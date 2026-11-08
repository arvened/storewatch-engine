"""Client-ready price comparison from one market's export.

Usage: python niche/client_report.py EXPORT_CSV OUT_CSV OUT_MD

EXPORT_CSV is `python -m storewatch export --market M --gtin --csv ...`.
Rows are grouped by barcode (EAN/GTIN); products without a barcode are grouped by
brand + normalised title. Only products found in at least two shops are compared
(a single-shop product says nothing about competition) — they are listed in OUT_CSV
after the compared ones.

OUT_CSV opens directly in Google Sheets / Excel (UTF-8, comma separated).
Only counts are printed (the public log).
"""

from __future__ import annotations

import csv
import re
import sys
import unicodedata
from collections import defaultdict


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def key_of(r: dict) -> str:
    g = (r.get("gtin") or "").strip().lstrip("0")
    if g:
        return "ean:" + g
    title = " ".join(x for x in (r.get("product_title"), r.get("variant_title")) if x and x != "Default Title")
    return "t:" + _norm(r.get("vendor", "")) + "|" + _norm(title)


def build(rows: list[dict]) -> tuple[list[str], list[list], dict]:
    shops = sorted({r["store"] for r in rows})
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key_of(r)].append(r)

    out = []
    for k, items in groups.items():
        by_shop: dict[str, dict] = {}
        for r in items:                       # cheapest variant per shop
            try:
                p = float(r["price"])
            except (TypeError, ValueError):
                continue
            if r["store"] not in by_shop or p < float(by_shop[r["store"]]["price"]):
                by_shop[r["store"]] = r
        if not by_shop:
            continue
        prices = {s: float(r["price"]) for s, r in by_shop.items()}
        lo, hi = min(prices.values()), max(prices.values())
        first = next(iter(by_shop.values()))
        title = first["product_title"] + (f" — {first['variant_title']}"
                                          if first.get("variant_title") not in ("", None, "Default Title") else "")
        out.append({
            "n": len(by_shop),
            "row": [title, first.get("vendor", ""), k[4:] if k.startswith("ean:") else "",
                    len(by_shop), lo, hi, round((hi / lo - 1) * 100, 1) if lo > 0 else "",
                    min(prices, key=prices.get)]
            + [prices.get(s, "") for s in shops],
        })
    out.sort(key=lambda x: (-(x["n"] >= 2), -(x["row"][6] or 0) if x["n"] >= 2 else 0, x["row"][0]))
    header = ["produkt", "marka", "EAN", "sklepów", "min cena", "max cena", "różnica %", "najtaniej"] + shops
    stats = {"shops": len(shops), "products": len(out),
             "compared": sum(1 for x in out if x["n"] >= 2),
             "by_ean": sum(1 for x in out if x["n"] >= 2 and x["row"][2])}
    return header, [x["row"] for x in out], stats


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    header, table, st = build(rows)
    with open(argv[1], "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(table)
    top = [r for r in table if isinstance(r[3], int) and r[3] >= 2][:10]
    md = [f"# Porównanie cen — {st['shops']} sklepów", "",
          f"- produktów łącznie: {st['products']}",
          f"- produktów w ≥2 sklepach (porównanie): {st['compared']} (w tym po EAN: {st['by_ean']})", "",
          "## Największe różnice cen", ""]
    md += [f"- {r[0]} ({r[1]}): {r[4]}–{r[5]} (+{r[6]}%), najtaniej: {r[7]}" for r in top] or ["- brak wspólnych produktów"]
    with open(argv[2], "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"client report: shops={st['shops']} products={st['products']} "
          f"compared={st['compared']} by_ean={st['by_ean']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
