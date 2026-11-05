"""Market context: what moves roasters' costs, from free official sources.

* Arabica price — IMF "Global price of Coffee, Other Mild Arabica", monthly
  average, US cents per pound (FRED series PCOFFOTMUSDM).
* Exchange rates — ECB euro reference rates (last 90 days). Green coffee is
  bought in US dollars, so the local currency against the dollar matters.

Both series are public with full history, so nothing needs to be accumulated
in advance. Failures are reported in the result, never raised: the weekly
report simply omits what is missing.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from .http import USER_AGENT, HttpGet, urllib_get

ARABICA_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=PCOFFOTMUSDM"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
CURRENCIES = ("PLN", "CZK", "EUR", "GBP")  # GBP: UK green-coffee sellers


def parse_fred_csv(text: str) -> list[tuple[str, float]]:
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        return rows
    for r in reader:
        if len(r) < 2:
            continue
        try:
            rows.append((r[0][:10], float(r[1])))
        except ValueError:               # '.' marks a missing value
            continue
    return rows


def parse_ecb_xml(text: str) -> list[tuple[str, dict[str, float]]]:
    """[(date, {currency: units per 1 EUR})], oldest first."""
    try:
        root = ET.fromstring(text.strip().encode("utf-8"))
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Cube" and "time" in el.attrib:
            rates = {}
            for c in el:
                try:
                    rates[c.attrib["currency"]] = float(c.attrib["rate"])
                except (KeyError, ValueError):
                    continue
            out.append((el.attrib["time"], rates))
    return sorted(out)


def _pct(new: float, old: float) -> float:
    return round((new / old - 1) * 100, 1)


def arabica_summary(rows: list[tuple[str, float]]) -> dict | None:
    if len(rows) < 2:
        return None
    month, value = rows[-1]
    prev = rows[-2][1]
    by_month = dict(rows)
    y = date.fromisoformat(month)
    year_ago = by_month.get(f"{y.year - 1}-{y.month:02d}-01")
    return {
        "month": month[:7],
        "usc_per_lb": round(value, 2),
        "mom_pct": _pct(value, prev),
        "yoy_pct": _pct(value, year_ago) if year_ago else None,
    }


def fx_summary(rows: list[tuple[str, dict[str, float]]], days: int = 30) -> dict | None:
    """Local currency units per 1 USD, now and about `days` days ago."""
    rows = [(d, r) for d, r in rows if "USD" in r]
    if len(rows) < 2:
        return None
    last_d, last = rows[-1]
    target = date.fromordinal(date.fromisoformat(last_d).toordinal() - days).isoformat()
    then_d, then = next(((d, r) for d, r in reversed(rows) if d <= target), rows[0])
    out = {"date": last_d, "compared_to": then_d, "per_usd": {}, "change_pct": {}}
    for cur in CURRENCIES:
        def per_usd(r):
            if cur == "EUR":
                return 1 / r["USD"]
            return r[cur] / r["USD"] if cur in r else None
        now_v, then_v = per_usd(last), per_usd(then)
        if now_v and then_v:
            out["per_usd"][cur] = round(now_v, 4)
            out["change_pct"][cur] = _pct(now_v, then_v)
    return out


def build_context(http_get: HttpGet = urllib_get, timeout: float = 20.0) -> dict:
    ctx: dict = {"generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                 "arabica": None, "fx": None, "errors": [],
                 "sources": {"arabica": ARABICA_URL, "fx": ECB_URL}}
    headers = {"User-Agent": USER_AGENT}
    for key, url, parse, summarise in (("arabica", ARABICA_URL, parse_fred_csv, arabica_summary),
                                       ("fx", ECB_URL, parse_ecb_xml, fx_summary)):
        try:
            resp = http_get(url, headers, timeout)
            if resp.status != 200:
                ctx["errors"].append(f"{key}: HTTP {resp.status}")
                continue
            ctx[key] = summarise(parse(resp.body))
            if ctx[key] is None:
                ctx["errors"].append(f"{key}: unexpected format")
        except Exception as e:           # network, timeout, anything: context is optional
            ctx["errors"].append(f"{key}: {type(e).__name__}: {e}")
    return ctx
