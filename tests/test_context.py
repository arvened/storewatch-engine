import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storewatch.context import (arabica_summary, build_context, fx_summary,  # noqa: E402
                                parse_ecb_xml, parse_fred_csv)
from storewatch.http import HttpResponse  # noqa: E402
from storewatch.weekly import WeeklyReport, context_lines, to_markdown  # noqa: E402

FRED = """observation_date,PCOFFOTMUSDM
2025-09-01,380.5
2025-10-01,.
2025-10-01,400.0
2026-08-01,300.0
2026-09-01,320.0
2026-10-01,336.0
"""

ECB = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube>
<Cube time="2026-10-30"><Cube currency="USD" rate="1.10"/><Cube currency="PLN" rate="4.40"/><Cube currency="CZK" rate="25.30"/></Cube>
<Cube time="2026-09-30"><Cube currency="USD" rate="1.15"/><Cube currency="PLN" rate="4.37"/><Cube currency="CZK" rate="25.30"/></Cube>
<Cube time="2026-09-29"><Cube currency="USD" rate="1.16"/><Cube currency="PLN" rate="4.37"/></Cube>
</Cube></gesmes:Envelope>"""


class TestContext(unittest.TestCase):
    def test_arabica(self):
        a = arabica_summary(parse_fred_csv(FRED))
        self.assertEqual(a, {"month": "2026-10", "usc_per_lb": 336.0, "mom_pct": 5.0, "yoy_pct": -16.0})

    def test_fx(self):
        fx = fx_summary(parse_ecb_xml(ECB))
        self.assertEqual((fx["date"], fx["compared_to"]), ("2026-10-30", "2026-09-30"))
        self.assertEqual(fx["per_usd"]["PLN"], 4.0)                     # 4.40 / 1.10
        self.assertEqual(fx["change_pct"]["PLN"], 5.3)                  # 4.0 vs 4.37/1.15=3.8
        self.assertEqual(fx["per_usd"]["EUR"], round(1 / 1.10, 4))

    def test_bad_inputs(self):
        self.assertIsNone(arabica_summary(parse_fred_csv("garbage")))
        self.assertIsNone(fx_summary(parse_ecb_xml("<html>")))

    def test_build_context_never_raises(self):
        def get(url, headers, timeout):
            if "fred" in url:
                return HttpResponse(200, FRED)
            raise OSError("down")
        ctx = build_context(get)
        self.assertEqual(ctx["arabica"]["month"], "2026-10")
        self.assertIsNone(ctx["fx"])
        self.assertTrue(ctx["errors"][0].startswith("fx: OSError"))

    def test_weekly_lines(self):
        ctx = {"arabica": arabica_summary(parse_fred_csv(FRED)), "fx": fx_summary(parse_ecb_xml(ECB))}
        r = WeeklyReport("2026-10-24T00:00:00+00:00", "2026-10-31T00:00:00+00:00", [], context=ctx)
        lines = context_lines(r)
        self.assertIn("październik 2026", lines[0])
        self.assertIn("-16,0% r/r", lines[0])
        self.assertIn("tanieje", lines[1])
        self.assertIn("1 USD = 4,00 zł (+5,3% w 30 dni) — dolar podrożał", lines[2])
        md = to_markdown(r)
        self.assertIn("## Kontekst rynkowy", md)
        self.assertIn("MFW", md)
        self.assertEqual(context_lines(WeeklyReport("2026-10-24T00:00:00", "2026-10-31T00:00:00", [])), [])


if __name__ == "__main__":
    unittest.main()
