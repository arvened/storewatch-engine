"""Check one niche's catalogue against EU Safety Gate alerts by barcode (EAN/GTIN).

Usage: python niche/recall_check.py CATALOG_CSV OUT_MD [--weeks N]

CATALOG_CSV is `python -m storewatch export --gtin --csv ...`.
Alerts come from the official public weekly-report XML (CC0):
  https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/list/xml/en

The XML layout is read defensively (any element whose tag mentions barcode/gtin/ean),
so a change in the export format degrades to "0 alerts read" instead of a crash.

Only counts go to stdout (the public log). Store names, products and matches go to OUT_MD,
which lives in the private data repository.
"""

from __future__ import annotations

import csv
import gzip
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

LIST_URL = "https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/list/xml/en"
UA = "ARVEN-StoreWatch/0.9 (+recall check; contact: hello@arvend.io)"
SOURCE_NOTE = ("Alerts from the Rapid Alert System for dangerous non-food products, published free of "
               "charge on the Safety Gate website (https://ec.europa.eu/safety-gate-alerts)")


def _get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def report_urls(list_xml: bytes, weeks: int) -> list[str]:
    """Links to the newest weekly-report XML files found in the list document."""
    root = ET.fromstring(list_xml)
    urls = []
    for el in root.iter():
        for val in [el.text or "", *el.attrib.values()]:
            val = val.strip()
            if val.startswith("http") and "xml" in val.lower() and val not in urls:
                urls.append(val)
    # the list is newest-first on the portal; keep that order but be safe if it is not
    urls.sort(key=lambda u: [int(x) for x in re.findall(r"\d+", u)], reverse=True)
    return urls[:weeks]


def _digits(s: str) -> list[str]:
    return [d for d in re.findall(r"\d{8,14}", re.sub(r"[\s-]", "", s or "")) if len(d) in (8, 12, 13, 14)]


def alerts_from_report(xml: bytes) -> list[dict]:
    """[{reference, product, brand, barcodes:set}] for every alert that states a barcode."""
    root = ET.fromstring(xml)
    parent = {c: p for p in root.iter() for c in p}
    found: dict[int, dict] = {}
    for el in root.iter():
        tag = _local(el.tag)
        if not any(k in tag for k in ("barcode", "gtin", "ean")):
            continue
        codes = _digits(el.text or "")
        if not codes:
            continue
        # climb to the element that represents one alert: the nearest ancestor with a reference
        node = el
        while node in parent:
            node = parent[node]
            if any("reference" in _local(c.tag) or "casenumber" in _local(c.tag) for c in node):
                break
        info = found.setdefault(id(node), {"reference": "", "product": "", "brand": "", "barcodes": set()})
        info["barcodes"].update(codes)
        for c in node.iter():
            t = _local(c.tag)
            txt = (c.text or "").strip()
            if not txt:
                continue
            if not info["reference"] and ("reference" in t or "casenumber" in t):
                info["reference"] = txt
            elif not info["brand"] and t == "brand":
                info["brand"] = txt
            elif not info["product"] and t in ("product", "name", "productname", "producttype", "category"):
                info["product"] = txt
    return list(found.values())


def catalogue(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if (r.get("gtin") or "").strip()]


def match(rows: list[dict], alerts: list[dict]) -> list[tuple[dict, dict]]:
    index: dict[str, dict] = {}
    for a in alerts:
        for code in a["barcodes"]:
            index[code.lstrip("0")] = a
    return [(r, index[r["gtin"].lstrip("0")]) for r in rows if r["gtin"].lstrip("0") in index]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cat_path, out_md = argv[0], argv[1]
    weeks = int(argv[argv.index("--weeks") + 1]) if "--weeks" in argv else 26

    rows = catalogue(cat_path)
    alerts, reports_ok = [], 0
    try:
        for url in report_urls(_get(LIST_URL), weeks):
            try:
                alerts.extend(alerts_from_report(_get(url)))
                reports_ok += 1
            except Exception as e:          # one bad report must not stop the check
                print(f"report skipped: {type(e).__name__}")
    except Exception as e:
        print(f"alert list unavailable: {type(e).__name__}")
    hits = match(rows, alerts)

    lines = [f"# Recall check: {len(hits)} match(es)", "",
             f"- weekly reports read: {reports_ok}",
             f"- alerts with a barcode: {len(alerts)}",
             f"- catalogue rows with a barcode: {len(rows)}", ""]
    for r, a in hits:
        lines.append(f"- **{r.get('store', '')}** — {r.get('product_title', '')} "
                     f"(EAN {r['gtin']}) → Safety Gate {a['reference']}: {a['brand']} {a['product']}"
                     f" — {r.get('handle', '')}")
    lines += ["", f"_{SOURCE_NOTE}_", ""]
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    # public log: counts only
    print(f"recall check: reports={reports_ok} alerts_with_barcode={len(alerts)} "
          f"rows_with_barcode={len(rows)} matches={len(hits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
