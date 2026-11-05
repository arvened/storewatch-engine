import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storewatch.usage import load, record, summarize, to_markdown  # noqa: E402


def rows(day_minutes, month="2026-11"):
    out = []
    for d, mins in day_minutes.items():
        out.append({"date": f"{month}-{d:02d}", "market": "pl", "seconds": str(mins * 60), "requests": "100",
                    "stores": "6"})
    return out


class TestUsage(unittest.TestCase):
    def test_one_off_setup_is_not_extrapolated(self):
        data = [{"date": "2026-11-01", "market": "setup", "seconds": "5571", "requests": "0", "stores": "0", "run": ""}]
        for run, mins in (("a", 16), ("b", 4)):
            data.append({"date": "2026-11-01", "market": "pl", "seconds": str(mins * 60), "requests": "1",
                         "stores": "6", "run": run})
        s = summarize(data, 1800, today=date(2026, 11, 1))
        self.assertEqual(s["runs"], 2)
        self.assertEqual(s["per_day"], 11.0)                          # (17 + 5) / 2, not 115
        self.assertAlmostEqual(s["projected"], 92.85 + 22 + 11 * 29, places=0)
        self.assertEqual(s["mode"], "normal")

    def test_projection_and_modes(self):
        s = summarize(rows({1: 9, 2: 9}), budget=1800, today=date(2026, 11, 2))
        self.assertEqual((s["minutes"], s["per_day"], s["projected"]), (20.0, 10.0, 300.0))
        self.assertEqual(s["mode"], "normal")
        s = summarize(rows({1: 59, 2: 59}), budget=1800, today=date(2026, 11, 2))
        self.assertEqual(s["mode"], "saver")                         # 60/day -> 1800 projected
        s = summarize(rows({d: 70 for d in range(1, 25)}), budget=1800, today=date(2026, 11, 24))
        self.assertEqual(s["mode"], "minimal")                       # 1704 spent > 90%

    def test_previous_month_ignored(self):
        s = summarize(rows({30: 500}, month="2026-10") + rows({1: 5}), budget=1800, today=date(2026, 11, 1))
        self.assertEqual(s["minutes"], 6.0)

    def test_old_log_is_upgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "usage.csv")
            with open(p, "w") as f:
                f.write("date,market,seconds,requests,stores\n2026-11-01,pl,60,10,6\n")
            record(p, "de", 30, 5, 10, run="42")
            data = load(p)
            self.assertEqual([(r["market"], r["run"]) for r in data], [("pl", ""), ("de", "42")])

    def test_record_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "u", "usage.csv")
            record(p, "pl", 120, 300, 6)
            record(p, "de", 60, 100, 10)
            data = load(p)
            self.assertEqual([r["market"] for r in data], ["pl", "de"])
            md = to_markdown(summarize(data, 1800))
            self.assertIn("Расход минут", md)
            self.assertIn("| pl | 2.0 | 300 | 1 |", md)


if __name__ == "__main__":
    unittest.main()
