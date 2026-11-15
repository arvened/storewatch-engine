""""One product — different prices": the same barcode (EAN/GTIN) in several shops (and on eBay).

Prices are compared in EUR. A shop's currency is GBP when its host ends with .uk or its name in the
store list carries "(UK)"; otherwise EUR. GBP is converted at the latest ECB reference rate; without
the rate, non-EUR offers are left out. Only in-stock offers count. A pair where one price is more
than 3x the other is ignored (usually a multipack or a wrong currency).
"""

from __future__ import annotations

import csv
import statistics as st
from datetime import date

from . import ebay, gtinfill
from .context import ECB_URL, parse_ecb_xml
from .http import USER_AGENT, urllib_get
from .storage import Storage

MAX_RATIO = 3.0


def store_currency(name: str, url: str) -> str:
    host = url.split("://", 1)[-1].split("/", 1)[0].lower()
    return "GBP" if host.endswith(".uk") or "(UK)" in (name or "") else "EUR"


def eur_rates(http_get=urllib_get) -> dict[str, float]:
    """currency -> units per 1 EUR (ECB). {'EUR': 1.0} only, if the ECB is unreachable."""
    rates = {"EUR": 1.0}
    try:
        resp = http_get(ECB_URL, {"User-Agent": USER_AGENT}, 20.0)
        if resp.status == 200:
            rows = parse_ecb_xml(resp.body)
            if rows:
                rates.update(max(rows)[1])
    except Exception:
        pass
    return rates


def collect(db: Storage, market: str | None) -> dict[str, dict]:
    """gtin -> {'title': str, 'offers': {seller: (price, currency)}} from the latest snapshot of each shop."""
    out: dict[str, dict] = {}
    for s in db.stores(market):
        snaps = db.last_ok_snapshots(s["url"], 1)
        if not snaps:
            continue
        cache = gtinfill.cached(db, s["url"])
        cur = store_currency(s["name"], s["url"])
        for v in db.variants(snaps[0]["id"]).values():
            g = v.gtin or cache.get(v.variant_id, "")
            if not g or not v.available or v.price <= 0:
                continue
            e = out.setdefault(g, {"title": v.label, "offers": {}})
            old = e["offers"].get(s["name"])
            if old is None or v.price < old[0]:
                e["offers"][s["name"]] = (v.price, cur)
    return out


def add_ebay(items: dict[str, dict], found: dict[str, list]) -> None:
    for g, lst in found.items():
        if g in items:
            for m, price, cur in lst:
                items[g]["offers"][f"eBay {m.removeprefix('EBAY_')}"] = (price, cur)


def compare(items: dict[str, dict], rates: dict[str, float]) -> list[dict]:
    rows = []
    for g, e in items.items():
        eur = {k: round(p / rates[c], 2) for k, (p, c) in e["offers"].items() if c in rates and rates[c] > 0}
        if len(eur) < 2:
            continue
        lo_k = min(eur, key=eur.get); hi_k = max(eur, key=eur.get)
        lo, hi = eur[lo_k], eur[hi_k]
        if lo <= 0 or hi / lo > MAX_RATIO:
            continue
        rows.append({"gtin": g, "title": e["title"], "offers": eur, "min_seller": lo_k, "min_eur": lo,
                     "max_seller": hi_k, "max_eur": hi, "saving_eur": round(hi - lo, 2),
                     "saving_pct": round((hi - lo) / hi * 100, 1) if hi else 0.0})
    rows.sort(key=lambda r: (-r["saving_pct"], -r["saving_eur"]))
    return rows


def to_markdown(rows: list[dict], top: int = 40, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    diff = [r for r in rows if r["saving_eur"] > 0]
    lines = [f"# Один товар — разные цены ({today})", ""]
    if not rows:
        return "\n".join(lines + ["Нет товаров с одинаковым штрихкодом в двух местах.", ""])
    cheapest: dict[str, int] = {}
    for r in diff:
        cheapest[r["min_seller"]] = cheapest.get(r["min_seller"], 0) + 1
    hi_sum = sum(r["max_eur"] for r in rows); lo_sum = sum(r["min_eur"] for r in rows)
    lines += [
        f"Товаров со штрихкодом в 2+ местах: **{len(rows)}**, с разной ценой: **{len(diff)}**.",
        (f"Экономия, если брать там, где дешевле: медиана **{st.median([r['saving_pct'] for r in diff]):.1f}%** "
         f"({st.median([r['saving_eur'] for r in diff]):.2f} €), максимум {max(r['saving_pct'] for r in diff):.1f}%."
         if diff else "Цены везде одинаковые."),
        f"Корзина из всех {len(rows)} товаров: {hi_sum:,.0f} € по самой высокой цене, ".replace(",", " ")[:-2] + ", "
        + f"{lo_sum:,.0f} € по самой низкой ".replace(",", " ")
        + f"(−{(hi_sum - lo_sum) / hi_sum * 100 if hi_sum else 0:.1f}%).",
        "", "## Где дешевле всего (сколько раз)", "",
    ]
    for k, n in sorted(cheapest.items(), key=lambda x: -x[1]):
        lines.append(f"- {k}: {n}")
    lines += ["", f"## Самая большая разница (топ {min(top, len(diff))})", "",
              "| Товар | Дешевле | Дороже | Экономия |", "|---|---|---|---|"]
    for r in diff[:top]:
        t = r["title"].replace("|", "/")[:70]
        lines.append(f"| {t} | {r['min_seller']} {r['min_eur']:.2f} € | {r['max_seller']} {r['max_eur']:.2f} € "
                     f"| {r['saving_pct']:.0f}% ({r['saving_eur']:.2f} €) |")
    lines += ["", "Цены в евро (фунты по курсу ЕЦБ), только товары в наличии. "
              "Пары с разницей больше 3× не учтены (обычно набор или другая валюта).", ""]
    return "\n".join(lines)


def to_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gtin", "title", "min_seller", "min_eur", "max_seller", "max_eur", "saving_eur", "saving_pct", "offers"])
        for r in rows:
            w.writerow([r["gtin"], r["title"], r["min_seller"], r["min_eur"], r["max_seller"], r["max_eur"],
                        r["saving_eur"], r["saving_pct"], "; ".join(f"{k}={v}" for k, v in sorted(r["offers"].items()))])


def ebay_candidates(items: dict[str, dict]) -> list[str]:
    """Barcodes worth an eBay lookup, most shops first (they are the ones customers compare)."""
    return [g for g, _ in sorted(items.items(), key=lambda x: -len(x[1]["offers"]))]
