import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storewatch.models import Variant  # noqa: E402
from storewatch.storage import Storage  # noqa: E402
from storewatch.weekly import _plural, build_weekly, focus_section, observations, to_html, to_markdown  # noqa: E402

NOW = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)


def v(i, price, cap=None, avail=True):
    return Variant(i, i, f"Kawa {i}", "", f"h{i}", "", "", "", price, cap, avail)


class TestWeekly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(os.path.join(self.tmp.name, "w.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def snap(self, url, variants, when):
        sid = self.db.save_snapshot(url, variants)
        self.db.db.execute("UPDATE snapshots SET taken_at=? WHERE id=?", (when.isoformat(), sid))
        self.db.db.commit()

    def seed(self):
        self.db.add_store("https://a.pl", "A")
        self.db.add_store("https://b.pl", "B")
        self.db.add_store("https://c.pl", "C")
        week_ago, mid = NOW - timedelta(days=7), NOW - timedelta(days=3)
        self.snap("https://a.pl", [v(1, 50), v(2, 60)], week_ago)
        self.snap("https://a.pl", [v(1, 40), v(2, 60)], mid)            # intermediate snapshot
        self.snap("https://a.pl", [v(1, 45), v(2, 60), v(3, 70)], NOW)  # net: 50 -> 45, one new
        self.snap("https://b.pl", [v(10, 80), v(11, 90)], week_ago)
        self.snap("https://b.pl", [v(10, 64, cap=80), v(11, 90, avail=False)], NOW)
        self.snap("https://c.pl", [v(20, 30)], NOW)                     # only one snapshot

    def test_net_weekly_change_vs_snapshot_a_week_ago(self):
        self.seed()
        r = build_weekly(self.db, now=NOW)
        a, b, c = r.stores
        self.assertEqual([(ch.kind, ch.old, ch.new) for ch in a.changes if ch.kind == "price_down"],
                         [("price_down", 50.0, 45.0)])
        self.assertEqual(a.count("product_new"), 1)
        self.assertEqual((b.count("discount_new"), b.count("out_of_stock")), (1, 1))
        self.assertEqual((c.changes, c.days), ([], 0))
        self.assertEqual(a.days, 7)

    def test_observations_and_focus(self):
        self.seed()
        r = build_weekly(self.db, now=NOW, focus="https://a.pl")
        obs = " ".join(observations(r))
        self.assertIn("w 2 z 3 sklepów", obs)
        self.assertIn("Najwięcej promocji: B", obs)
        fs = " ".join(focus_section(r))
        self.assertIn("Nowe braki u konkurencji: 1 pozycja", fs)
        md = to_markdown(r)
        self.assertIn("| A (Ty) |", md)
        self.assertNotIn("**A**", md)          # own changes are not listed as competitor moves
        html = to_html(r, sample=True)
        self.assertIn("Przykładowy raport", html)
        self.assertNotIn("<script", html)

    def test_errors_and_empty(self):
        self.db.add_store("https://x.pl", "X")
        r = build_weekly(self.db, now=NOW)
        self.assertEqual(r.stores[0].error, "brak danych")
        self.assertEqual(observations(r), [])
        self.assertIn("— | — |", to_markdown(r))

    def test_html_escapes_names(self):
        self.db.add_store("https://e.pl", "<b>Evil</b>")
        self.snap("https://e.pl", [v(1, 10)], NOW)
        self.assertNotIn("<b>Evil</b>", to_html(build_weekly(self.db, now=NOW)))

    def test_polish_plural(self):
        self.assertEqual([_plural(n, "pozycja", "pozycje", "pozycji") for n in (1, 2, 5, 12, 22, 25)],
                         ["pozycja", "pozycje", "pozycji", "pozycji", "pozycje", "pozycji"])


if __name__ == "__main__":
    unittest.main()
