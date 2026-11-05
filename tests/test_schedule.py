"""Tests for the pieces used by the scheduled run: store file sync and pruning."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storewatch import cli  # noqa: E402
from storewatch.models import Variant  # noqa: E402
from storewatch.storage import Storage  # noqa: E402


def v(i, price=10.0):
    return Variant(i, i, f"P{i}", "", "h", "", "", "", price, None, True)


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.list_path = os.path.join(self.tmp.name, "stores.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def write_list(self, text):
        with open(self.list_path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_read_store_file(self):
        self.write_list("# конкуренты\n\nlacava.pl | LaCava\nhttps://Kawa.pl/x  # опт\nlacava.pl\n")
        self.assertEqual(cli.read_store_file(self.list_path),
                         [("https://lacava.pl", "LaCava"), ("https://kawa.pl", None)])

    def test_sync_adds_removes_renames_and_reactivates(self):
        db = Storage(self.db_path)
        db.add_store("https://old.pl", "Old")
        db.add_store("https://keep.pl", "Keep")
        added, removed = db.sync_stores([("https://keep.pl", "Keep 2"), ("https://new.pl", None)])
        self.assertEqual((added, removed), (["https://new.pl"], ["https://old.pl"]))
        self.assertEqual([(s["url"], s["name"]) for s in db.stores()],
                         [("https://keep.pl", "Keep 2"), ("https://new.pl", "new.pl")])
        added, _ = db.sync_stores([("https://old.pl", None)])   # comes back: history kept
        self.assertEqual(added, [])
        self.assertEqual([s["url"] for s in db.stores()], ["https://old.pl"])
        db.close()

    def test_prune_keeps_newest_ok_snapshots(self):
        db = Storage(self.db_path)
        db.add_store("https://a.pl")
        for p in range(5):
            db.save_snapshot("https://a.pl", [v(1, 10 + p), v(2)])
        for _ in range(4):
            db.save_error("https://a.pl", "down")
        deleted = db.prune(3)
        self.assertEqual(deleted, 2 + 1)                         # 2 old ok + 1 old error
        ok = db.last_ok_snapshots("https://a.pl", 10)
        self.assertEqual(len(ok), 3)
        self.assertEqual([x[1] for x in db.price_history("https://a.pl", 1)], [12.0, 13.0, 14.0])
        left = db.db.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        self.assertEqual(left, 6)
        with self.assertRaises(ValueError):
            db.prune(1)
        db.close()

    def test_cli_sync(self):
        os.environ["STOREWATCH_DB"] = self.db_path
        try:
            self.write_list("a.pl | A\nb.pl | B\n")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["sync", self.list_path]), 0)
            self.assertIn("добавлено 2", out.getvalue())
            self.write_list("a.pl | A\nnot a url ::\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["sync", self.list_path]), 1)   # bad line: nothing changes
        finally:
            del os.environ["STOREWATCH_DB"]


if __name__ == "__main__":
    unittest.main()


class TestMarkets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Storage(os.path.join(self.tmp.name, "m.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_sync_is_scoped_to_market(self):
        self.db.add_store("https://legacy.pl", "Legacy")            # v0.3 row, market NULL
        self.db.sync_stores([("https://legacy.pl", "Legacy"), ("https://b.pl", "B")], "pl")
        self.db.sync_stores([("https://x.de", "X"), ("https://y.de", "Y")], "de")
        self.assertEqual([s["url"] for s in self.db.stores("pl")], ["https://legacy.pl", "https://b.pl"])
        self.assertEqual([s["url"] for s in self.db.stores("de")], ["https://x.de", "https://y.de"])
        _, removed = self.db.sync_stores([("https://x.de", "X")], "de")
        self.assertEqual(removed, ["https://y.de"])
        self.assertEqual(len(self.db.stores("pl")), 2)                 # other market untouched
        self.assertEqual(len(self.db.stores()), 3)

    def test_run_and_weekly_filter_by_market(self):
        from storewatch.engine import run_cycle
        from storewatch.weekly import build_weekly

        class F:
            def fetch(self, url, platform=None, **kw):
                return "shopify", [v(1, 10.0)]
        self.db.sync_stores([("https://a.pl", "A")], "pl")
        self.db.sync_stores([("https://b.de", "B")], "de")
        reps = run_cycle(self.db, F(), market="de")
        self.assertEqual([r.name for r in reps], ["B"])
        self.assertEqual([s.name for s in build_weekly(self.db, market="de").stores], ["B"])

    def test_migrates_v03_database(self):
        import sqlite3
        path = os.path.join(self.tmp.name, "old.db")
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE stores (url TEXT PRIMARY KEY, name TEXT NOT NULL, added_at TEXT NOT NULL, "
                          "active INTEGER NOT NULL DEFAULT 1, platform TEXT);"
                          "INSERT INTO stores VALUES ('https://o.pl','O','2026-11-01',1,'shopify');")
        con.commit(); con.close()
        db = Storage(path)
        self.assertEqual((db.stores()[0]["platform"], db.stores()[0]["market"]), ("shopify", None))
        db.close()
