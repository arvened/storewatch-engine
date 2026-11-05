"""Weekly market report in Polish: one dataset, sold to many readers.

Compares each store's latest snapshot with its snapshot from a week earlier
(net weekly change, not every intra-week flip), ranks the market and writes
rule-based observations. Optional `focus` store gets a personal section
comparing it with the market median.

No LLM: every sentence is computed from the data, so it can be checked.
"""

from __future__ import annotations

import html
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .diff import Change, DiffConfig, catalog_summary, diff_snapshots
from .storage import Storage

MONTHS_PL = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
             "sierpnia", "września", "października", "listopada", "grudnia"]


def _date_pl(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {MONTHS_PL[d.month - 1]} {d.year}"


def _zl(x: float | None) -> str:
    if x is None:
        return "—"
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} zł"


def _pct(x: float) -> str:
    return f"{x:.1f}%".replace(".", ",")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


@dataclass
class StoreWeek:
    url: str
    name: str
    summary: dict
    changes: list[Change] = field(default_factory=list)
    days: int = 0                   # how many days the comparison covers
    error: str | None = None

    def count(self, *kinds: str) -> int:
        return sum(1 for c in self.changes if c.kind in kinds)


@dataclass
class WeeklyReport:
    period_from: str
    period_to: str
    stores: list[StoreWeek]
    focus: str | None = None
    context: dict | None = None     # from context.build_context()
    currency: str = "PLN"           # local currency of the market

    @property
    def ok(self) -> list[StoreWeek]:
        return [s for s in self.stores if not s.error and s.summary.get("variants")]


def build_weekly(storage: Storage, days: int = 7, now: datetime | None = None,
                 focus: str | None = None, cfg: DiffConfig | None = None,
                 market: str | None = None) -> WeeklyReport:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    out: list[StoreWeek] = []
    first_dates = []
    for st in storage.stores(market):
        url, name = st["url"], st["name"]
        latest = storage.last_ok_snapshots(url, 1)
        if not latest:
            out.append(StoreWeek(url, name, {}, error="brak danych"))
            continue
        after_row = latest[0]
        before_row = storage.ok_snapshot_at_or_before(url, cutoff)
        after = storage.variants(after_row["id"])
        sw = StoreWeek(url, name, catalog_summary(after))
        if before_row and before_row["id"] != after_row["id"]:
            before = storage.variants(before_row["id"])
            full = not (before_row["partial"] or after_row["partial"])
            sw.changes = diff_snapshots(url, before, after, cfg, assortment=full).changes
            d0 = datetime.fromisoformat(before_row["taken_at"])
            d1 = datetime.fromisoformat(after_row["taken_at"])
            sw.days = max(1, round((d1 - d0).total_seconds() / 86400))
            first_dates.append(before_row["taken_at"])
        out.append(sw)
    period_from = min(first_dates) if first_dates else cutoff
    return WeeklyReport(period_from, now.replace(microsecond=0).isoformat(), out, focus)


# --- observations ------------------------------------------------------------

CUR_PL = {"PLN": "zł", "CZK": "Kč", "EUR": "€"}
MONTHS_NOM_PL = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień",
                 "wrzesień", "październik", "listopad", "grudzień"]


def _signed(x: float) -> str:
    return ("+" if x > 0 else "") + _pct(x)


def context_lines(r: WeeklyReport) -> list[str]:
    """'Kontekst rynkowy': raw-material price and the dollar — why roasters' costs move."""
    ctx = r.context or {}
    lines = []
    a = ctx.get("arabica")
    if a:
        y, m = (int(x) for x in a["month"].split("-"))
        yoy = f", {_signed(a['yoy_pct'])} r/r" if a.get("yoy_pct") is not None else ""
        lines.append(f"Arabika (indeks MFW, średnia za {MONTHS_NOM_PL[m - 1]} {y}): "
                     f"{a['usc_per_lb']:.0f} centów USD za funt; {_signed(a['mom_pct'])} m/m{yoy}.")
        if a.get("yoy_pct") is not None and abs(a["yoy_pct"]) >= 10:
            trend = "drożeje" if a["yoy_pct"] > 0 else "tanieje"
            lines.append(f"Surowiec wyraźnie {trend} rok do roku. Palarnie zwykle przenoszą to na ceny "
                         f"z opóźnieniem kilku miesięcy — warto obserwować, kto zareaguje pierwszy.")
    fx = ctx.get("fx")
    cur = r.currency
    if fx and cur in fx.get("per_usd", {}):
        rate, chg = fx["per_usd"][cur], fx["change_pct"][cur]
        sym = CUR_PL.get(cur, cur)
        if abs(chg) < 1:
            effect = "kurs stabilny, bez wpływu na koszt surowca"
        elif chg > 0:
            effect = "dolar podrożał, więc zielona kawa (kupowana w USD) jest droższa dla palarni"
        else:
            effect = "dolar potaniał, więc presja kosztowa na palarnie słabnie"
        rate_txt = f"{rate:.2f}".replace(".", ",")
        lines.append(f"Kurs: 1 USD = {rate_txt} {sym} "
                     f"({_signed(chg)} w 30 dni) — {effect}.")
    return lines


def observations(r: WeeklyReport) -> list[str]:
    ok = r.ok
    obs: list[str] = []
    if len(ok) < 2:
        return obs
    n = len(ok)

    movers = [s for s in ok if s.count("price_down", "price_up", "discount_new", "discount_end")]
    if any(s.days for s in ok):
        obs.append(f"Ceny lub promocje zmieniły się w tym tygodniu w {len(movers)} z {n} "
                   f"{_plural(n, 'sklepu', 'sklepów', 'sklepów')}.")

    promo = max(ok, key=lambda s: s.summary["on_sale_pct"])
    med_promo = statistics.median(s.summary["on_sale_pct"] for s in ok)
    if promo.summary["on_sale_pct"] > 0:
        obs.append(f"Najwięcej promocji: {promo.name} — {_pct(promo.summary['on_sale_pct'])} oferty "
                   f"(średni rabat {_pct(promo.summary['avg_discount_pct'])}); mediana rynku "
                   f"{_pct(med_promo)}.")

    widest = max(ok, key=lambda s: s.summary["products"])
    obs.append(f"Najszerszy asortyment: {widest.name} — {widest.summary['products']} "
               f"{_plural(widest.summary['products'], 'produkt', 'produkty', 'produktów')}.")

    stockouts = sorted(ok, key=lambda s: -s.count("out_of_stock"))
    if stockouts[0].count("out_of_stock"):
        s = stockouts[0]
        k = s.count("out_of_stock")
        obs.append(f"Najwięcej nowych braków: {s.name} ({k} {_plural(k, 'pozycja', 'pozycje', 'pozycji')}). "
                   f"To okno sprzedażowe dla konkurencji.")

    new_total = sum(s.count("product_new") for s in ok)
    if new_total:
        leader = max(ok, key=lambda s: s.count("product_new"))
        obs.append(f"Nowości na rynku: {new_total}; najwięcej: {leader.name} ({leader.count('product_new')}).")

    low_stock = [s for s in ok if s.summary["in_stock_pct"] < 50]
    for s in low_stock:
        obs.append(f"Uwaga: {s.name} ma dostępne tylko {_pct(s.summary['in_stock_pct'])} oferty — "
                   f"albo braki, albo dużo archiwalnych pozycji w katalogu.")
    return obs


def focus_section(r: WeeklyReport) -> list[str]:
    me = next((s for s in r.ok if s.url == r.focus), None)
    others = [s for s in r.ok if s.url != r.focus]
    if not me or not others:
        return []
    lines = []
    med_promo = statistics.median(s.summary["on_sale_pct"] for s in others)
    mine = me.summary["on_sale_pct"]
    diff = mine - med_promo
    if diff >= 2:
        lines.append(f"Masz w promocji {_pct(mine)} oferty, konkurencja (mediana) {_pct(med_promo)}: "
                     f"rabatujesz częściej niż rynek. Warto sprawdzić, czy każda promocja jest potrzebna.")
    elif diff <= -2:
        lines.append(f"Masz w promocji {_pct(mine)} oferty, konkurencja (mediana) {_pct(med_promo)}: "
                     f"klienci porównujący ceny częściej trafią na rabat u innych.")
    else:
        lines.append(f"Udział promocji ({_pct(mine)}) jest zbliżony do rynku ({_pct(med_promo)}).")

    cut = [c for s in others for c in s.changes if c.kind == "price_down"]
    if cut:
        lines.append(f"Konkurencja obniżyła w tym tygodniu ceny {len(cut)} "
                     f"{_plural(len(cut), 'pozycji', 'pozycji', 'pozycji')} — lista poniżej.")
    gone = sum(s.count("out_of_stock") for s in others)
    if gone:
        lines.append(f"Nowe braki u konkurencji: {gone} {_plural(gone, 'pozycja', 'pozycje', 'pozycji')}. "
                     f"Jeśli masz podobne kawy, to dobry moment, żeby je pokazać.")
    return lines


def _top(r: WeeklyReport, kind: str, limit: int, key=None) -> list[tuple[str, Change]]:
    items = [(s.name, c) for s in r.ok if s.url != r.focus for c in s.changes if c.kind == kind]
    if key:
        items.sort(key=key)
    return items[:limit]


# --- rendering ---------------------------------------------------------------

def to_markdown(r: WeeklyReport, sample: bool = False) -> str:
    L = []
    title = "Raport tygodniowy: rynek kawy specialty"
    L.append(f"# {title}")
    L.append(f"{_date_pl(r.period_from)} – {_date_pl(r.period_to)} · sklepów: {len(r.stores)}")
    if sample:
        L.append("")
        L.append("> PRZYKŁADOWY RAPORT — nazwy sklepów i dane są ilustracyjne.")
    L.append("")
    obs = observations(r)
    if obs:
        L.append("## Najważniejsze w tym tygodniu")
        L += [f"- {o}" for o in obs]
        L.append("")
    fs = focus_section(r)
    if fs:
        L.append("## Co to oznacza dla Ciebie")
        L += [f"- {x}" for x in fs]
        L.append("")
    ctx = context_lines(r)
    if ctx:
        L.append("## Kontekst rynkowy")
        L += [f"- {x}" for x in ctx]
        L.append("")
    L.append("## Rynek w liczbach")
    L.append("| Sklep | Produkty | Dostępne | W promocji | Średni rabat | Mediana ceny | Zmiany cen | Nowości |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for s in r.stores:
        if s.error or not s.summary.get("variants"):
            L.append(f"| {s.name} | — | — | — | — | — | — | — |")
            continue
        m = s.summary
        mark = " (Ty)" if s.url == r.focus else ""
        L.append(f"| {s.name}{mark} | {m['products']} | {_pct(m['in_stock_pct'])} | {_pct(m['on_sale_pct'])} | "
                 f"{_pct(m['avg_discount_pct'])} | {_zl(m['median_price'])} | "
                 f"{s.count('price_down', 'price_up')} | {s.count('product_new')} |")
    L.append("")
    sections = [
        ("Największe obniżki", _top(r, "price_down", 8, key=lambda x: x[1].pct or 0),
         lambda c: f"{_zl(c.old)} → {_zl(c.new)} ({_pct(c.pct)})"),
        ("Nowe promocje", _top(r, "discount_new", 8, key=lambda x: x[1].pct or 0),
         lambda c: f"{_zl(c.new)}, wcześniej {_zl(c.old)} ({_pct(-(c.pct or 0))} taniej)"),
        ("Podwyżki", _top(r, "price_up", 8, key=lambda x: -(x[1].pct or 0)),
         lambda c: f"{_zl(c.old)} → {_zl(c.new)} (+{_pct(c.pct)})"),
        ("Nowości", _top(r, "product_new", 10), lambda c: _zl(c.new)),
        ("Zniknęły z dostępności", _top(r, "out_of_stock", 10), lambda c: _zl(c.new)),
    ]
    for head, items, fmt in sections:
        if not items:
            continue
        L.append(f"## {head}")
        L += [f"- **{name}**: {c.label}: {fmt(c)}" for name, c in items]
        L.append("")
    failed = [s.name for s in r.stores if s.error]
    if failed:
        L.append(f"_Nie udało się odczytać: {', '.join(failed)}._")
        L.append("")
    L.append("---")
    L.append("Dane zebrane automatycznie z publicznych katalogów sklepów. Ceny „od” dotyczą najmniejszego "
             "wariantu (np. opakowania). Zmiany liczone między stanem sprzed tygodnia a dzisiejszym.")
    return "\n".join(L).rstrip() + "\n"


def to_html(r: WeeklyReport, sample: bool = False) -> str:
    """Email-friendly HTML: inline styles, single column, no scripts."""
    e = html.escape
    css_td = "padding:6px 8px;border-bottom:1px solid #e6e2dc;font-size:13px;"
    parts = [
        '<!doctype html><html lang="pl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Raport tygodniowy: rynek kawy specialty</title></head>"
        '<body style="margin:0;background:#f6f3ee;font-family:Georgia,\'Times New Roman\',serif;color:#2b2420;">'
        '<div style="max-width:680px;margin:0 auto;padding:24px 16px;">'
        '<div style="background:#ffffff;border-radius:10px;padding:24px 20px;">',
        '<p style="margin:0 0 4px;font:600 11px/1.4 Arial,sans-serif;letter-spacing:.12em;'
        'text-transform:uppercase;color:#8a6a4f;">Raport tygodniowy</p>',
        '<h1 style="margin:0 0 6px;font-size:26px;line-height:1.2;">Rynek kawy specialty</h1>',
        f'<p style="margin:0 0 18px;font:13px/1.5 Arial,sans-serif;color:#6b625b;">'
        f"{e(_date_pl(r.period_from))} – {e(_date_pl(r.period_to))} · sklepów: {len(r.stores)}</p>",
    ]
    if sample:
        parts.append('<p style="margin:0 0 18px;padding:10px 12px;background:#fff4dc;border-radius:6px;'
                     'font:13px/1.5 Arial,sans-serif;color:#7a5600;">Przykładowy raport — nazwy sklepów '
                     "i dane są ilustracyjne.</p>")

    def section(title, items):
        if not items:
            return
        parts.append(f'<h2 style="margin:22px 0 8px;font-size:18px;">{e(title)}</h2>'
                     '<ul style="margin:0;padding-left:18px;font:14px/1.6 Arial,sans-serif;">')
        parts.extend(f'<li style="margin:0 0 6px;">{i}</li>' for i in items)
        parts.append("</ul>")

    section("Najważniejsze w tym tygodniu", [e(o) for o in observations(r)])
    fs = focus_section(r)
    if fs:
        parts.append('<div style="margin:20px 0 0;padding:14px 16px;background:#f1ece4;border-radius:8px;">'
                     '<h2 style="margin:0 0 8px;font-size:18px;">Co to oznacza dla Ciebie</h2>'
                     '<ul style="margin:0;padding-left:18px;font:14px/1.6 Arial,sans-serif;">'
                     + "".join(f'<li style="margin:0 0 6px;">{e(x)}</li>' for x in fs) + "</ul></div>")

    section("Kontekst rynkowy", [e(x) for x in context_lines(r)])
    parts.append('<h2 style="margin:22px 0 8px;font-size:18px;">Rynek w liczbach</h2>'
                 '<div style="overflow-x:auto;"><table style="border-collapse:collapse;width:100%;'
                 'font-family:Arial,sans-serif;min-width:520px;">'
                 '<tr style="text-align:left;color:#6b625b;font-size:12px;">'
                 + "".join(f'<th style="{css_td}font-weight:600;">{h}</th>' for h in
                           ["Sklep", "Produkty", "Dostępne", "Promocje", "Śr. rabat", "Mediana", "Zm. cen", "Nowe"])
                 + "</tr>")
    for s in r.stores:
        bold = "font-weight:700;background:#faf6ef;" if s.url == r.focus else ""
        if s.error or not s.summary.get("variants"):
            cells = [e(s.name)] + ["—"] * 7
        else:
            m = s.summary
            cells = [e(s.name + (" (Ty)" if s.url == r.focus else "")), str(m["products"]),
                     _pct(m["in_stock_pct"]), _pct(m["on_sale_pct"]), _pct(m["avg_discount_pct"]),
                     e(_zl(m["median_price"])), str(s.count("price_down", "price_up")), str(s.count("product_new"))]
        parts.append(f'<tr style="{bold}">' + "".join(f'<td style="{css_td}">{c}</td>' for c in cells) + "</tr>")
    parts.append("</table></div>")

    def items(kind, limit, fmt, key=None):
        return [f"<b>{e(n)}</b>: {e(c.label)}: {e(fmt(c))}" for n, c in _top(r, kind, limit, key)]

    section("Największe obniżki", items("price_down", 8, lambda c: f"{_zl(c.old)} → {_zl(c.new)} ({_pct(c.pct)})",
                                        key=lambda x: x[1].pct or 0))
    section("Nowe promocje", items("discount_new", 8,
                                   lambda c: f"{_zl(c.new)}, wcześniej {_zl(c.old)} ({_pct(-(c.pct or 0))} taniej)",
                                   key=lambda x: x[1].pct or 0))
    section("Podwyżki", items("price_up", 8, lambda c: f"{_zl(c.old)} → {_zl(c.new)} (+{_pct(c.pct)})",
                              key=lambda x: -(x[1].pct or 0)))
    section("Nowości", items("product_new", 10, lambda c: _zl(c.new)))
    section("Zniknęły z dostępności", items("out_of_stock", 10, lambda c: _zl(c.new)))

    failed = [s.name for s in r.stores if s.error]
    if failed:
        parts.append(f'<p style="font:italic 13px Arial,sans-serif;color:#6b625b;">Nie udało się odczytać: '
                     f"{e(', '.join(failed))}.</p>")
    parts.append('<p style="margin:24px 0 0;padding-top:12px;border-top:1px solid #e6e2dc;'
                 'font:12px/1.5 Arial,sans-serif;color:#8a817a;">Dane zebrane automatycznie z publicznych '
                 "katalogów sklepów. Ceny „od” dotyczą najmniejszego wariantu (np. opakowania). Zmiany liczone "
                 "między stanem sprzed tygodnia a dzisiejszym.</p>")
    parts.append("</div></div></body></html>")
    return "".join(parts)
