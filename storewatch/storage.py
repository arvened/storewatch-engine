"""SQLite storage for tracked stores and catalogue snapshots."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .models import Variant

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    url        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    platform   TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_url  TEXT NOT NULL REFERENCES stores(url),
    taken_at   TEXT NOT NULL,
    status     TEXT NOT NULL,          -- ok | error
    error      TEXT,
    n_variants INTEGER NOT NULL DEFAULT 0,
    partial    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_snap_store ON snapshots(store_url, id);
CREATE TABLE IF NOT EXISTS page_memory (
    store_url  TEXT NOT NULL,
    url        TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- product | other
    seen_at    TEXT NOT NULL,
    PRIMARY KEY (store_url, url)
);
CREATE TABLE IF NOT EXISTS variants (
    snapshot_id      INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    product_id       INTEGER NOT NULL,
    variant_id       INTEGER NOT NULL,
    product_title    TEXT NOT NULL,
    variant_title    TEXT NOT NULL,
    handle           TEXT NOT NULL,
    sku              TEXT NOT NULL,
    vendor           TEXT NOT NULL,
    product_type     TEXT NOT NULL,
    price            REAL NOT NULL,
    compare_at_price REAL,
    available        INTEGER NOT NULL,
    tags             TEXT NOT NULL DEFAULT '',
    grams            INTEGER,
    created_at       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, variant_id)
);
"""

VARIANT_FIELDS = [
    "product_id", "variant_id", "product_title", "variant_title", "handle",
    "sku", "vendor", "product_type", "price", "compare_at_price", "available",
    "tags", "grams", "created_at",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, path: str = "storewatch.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(stores)")}
        if "platform" not in cols:                      # migrate v0.1 databases
            self.db.execute("ALTER TABLE stores ADD COLUMN platform TEXT")
        if "market" not in cols:                        # migrate v0.2/0.3 databases
            self.db.execute("ALTER TABLE stores ADD COLUMN market TEXT")
        if "url_filter" not in cols:                    # v0.7: brand filter, comma-separated words
            self.db.execute("ALTER TABLE stores ADD COLUMN url_filter TEXT")
        scols = {r["name"] for r in self.db.execute("PRAGMA table_info(snapshots)")}
        if "partial" not in scols:                      # migrate v0.4 databases
            self.db.execute("ALTER TABLE snapshots ADD COLUMN partial INTEGER NOT NULL DEFAULT 0")
        vcols = {r["name"] for r in self.db.execute("PRAGMA table_info(variants)")}
        if "tags" not in vcols:                         # v0.8: facts kept for the price history
            self.db.execute("ALTER TABLE variants ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            self.db.execute("ALTER TABLE variants ADD COLUMN grams INTEGER")
            self.db.execute("ALTER TABLE variants ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- stores -----------------------------------------------------------
    def add_store(self, url: str, name: str | None = None, platform: str | None = None,
                  market: str | None = None) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO stores(url, name, added_at, platform, market) VALUES (?,?,?,?,?)",
            (url, name or url.split("://", 1)[-1], now_iso(), platform, market),
        )
        if cur.rowcount == 0 and platform:
            self.db.execute("UPDATE stores SET platform=? WHERE url=?", (platform, url))
        if cur.rowcount == 0 and market:
            self.db.execute("UPDATE stores SET market=? WHERE url=?", (market, url))
        if cur.rowcount == 0:
            self.db.execute("UPDATE stores SET active=1 WHERE url=?", (url,))
        self.db.commit()
        return cur.rowcount == 1

    def set_platform(self, url: str, platform: str) -> None:
        self.db.execute("UPDATE stores SET platform=? WHERE url=?", (platform, url))
        self.db.commit()

    def remove_store(self, url: str) -> bool:
        cur = self.db.execute("UPDATE stores SET active=0 WHERE url=? AND active=1", (url,))
        self.db.commit()
        return cur.rowcount == 1

    def stores(self, market: str | None = None) -> list[sqlite3.Row]:
        """Active stores; with `market`, only that market's."""
        if market is None:
            return list(self.db.execute("SELECT * FROM stores WHERE active=1 ORDER BY added_at, rowid"))
        return list(self.db.execute(
            "SELECT * FROM stores WHERE active=1 AND market=? ORDER BY added_at, rowid", (market,)))

    def sync_stores(self, wanted: list[tuple],
                    market: str | None = None) -> tuple[list[str], list[str]]:
        """Make the active list equal to `wanted` [(url, name)] or [(url, name, brands)] —
        within `market` if given (other markets are untouched). Returns (added, removed)."""
        wanted_urls = [w[0] for w in wanted]
        added = []
        for w in wanted:
            url, name = w[0], w[1]
            if self.add_store(url, name, market=market):
                added.append(url)
            elif name:
                self.db.execute("UPDATE stores SET name=? WHERE url=?", (name, url))
            if len(w) > 2:
                self.db.execute("UPDATE stores SET url_filter=? WHERE url=?",
                                (",".join(w[2]) or None, url))
        removed = [r["url"] for r in self.stores(market) if r["url"] not in wanted_urls]
        for url in removed:
            self.remove_store(url)
        self.db.commit()
        return added, removed

    def prune(self, keep: int) -> int:
        """Keep only the newest `keep` snapshots per store. Returns snapshots deleted."""
        if keep < 2:
            raise ValueError("keep must be >= 2 (two snapshots are needed for a comparison)")
        old = [r["id"] for r in self.db.execute(
            # ranked per status, so a run of errors never pushes out the last good snapshot
            "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY store_url, status ORDER BY id DESC) AS rn "
            "FROM snapshots) WHERE rn > ?", (keep,))]
        for i in range(0, len(old), 500):
            chunk = old[i:i + 500]
            marks = ",".join("?" * len(chunk))
            self.db.execute(f"DELETE FROM variants WHERE snapshot_id IN ({marks})", chunk)
            self.db.execute(f"DELETE FROM snapshots WHERE id IN ({marks})", chunk)
        self.db.commit()
        if old:
            self.db.execute("VACUUM")
        return len(old)

    # --- snapshots --------------------------------------------------------
    def save_snapshot(self, store_url: str, variants: list[Variant], partial: bool = False) -> int:
        cur = self.db.execute(
            "INSERT INTO snapshots(store_url, taken_at, status, n_variants, partial) VALUES (?,?, 'ok', ?, ?)",
            (store_url, now_iso(), len(variants), int(partial)),
        )
        sid = cur.lastrowid
        rows = []
        for v in variants:
            d = asdict(v)
            rows.append((sid, *[int(d[f]) if f == "available" else d[f] for f in VARIANT_FIELDS]))
        self.db.executemany(
            f"INSERT OR REPLACE INTO variants(snapshot_id, {', '.join(VARIANT_FIELDS)}) "
            f"VALUES ({', '.join('?' * (len(VARIANT_FIELDS) + 1))})",
            rows,
        )
        self.db.commit()
        return sid

    def save_error(self, store_url: str, error: str) -> int:
        cur = self.db.execute(
            "INSERT INTO snapshots(store_url, taken_at, status, error) VALUES (?,?, 'error', ?)",
            (store_url, now_iso(), error),
        )
        self.db.commit()
        return cur.lastrowid

    def last_ok_snapshots(self, store_url: str, n: int = 2) -> list[sqlite3.Row]:
        """Most recent successful snapshots, newest first."""
        return list(self.db.execute(
            "SELECT * FROM snapshots WHERE store_url=? AND status='ok' ORDER BY id DESC LIMIT ?",
            (store_url, n),
        ))

    def ok_snapshot_at_or_before(self, store_url: str, iso: str) -> sqlite3.Row | None:
        """Newest successful snapshot taken at or before `iso`; else the oldest one available."""
        row = self.db.execute(
            "SELECT * FROM snapshots WHERE store_url=? AND status='ok' AND taken_at<=? ORDER BY id DESC LIMIT 1",
            (store_url, iso)).fetchone()
        if row is None:
            row = self.db.execute(
                "SELECT * FROM snapshots WHERE store_url=? AND status='ok' ORDER BY id LIMIT 1",
                (store_url,)).fetchone()
        return row

    # --- page memory (universal reader) ------------------------------------
    def page_memory(self, store_url: str, other_ttl_days: int = 30) -> dict[str, set[str]]:
        """Pages already classified for a store. Non-product pages are forgotten after
        `other_ttl_days`, so a page that later becomes a product is found again."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=other_ttl_days)).replace(microsecond=0).isoformat()
        mem: dict[str, set[str]] = {"product": set(), "other": set()}
        for r in self.db.execute(
                "SELECT url, kind, seen_at FROM page_memory WHERE store_url=?", (store_url,)):
            if r["kind"] == "product" or r["seen_at"] >= cutoff:
                mem[r["kind"]].add(r["url"])
        return mem

    def save_page_memory(self, store_url: str, learned: dict[str, set[str]]) -> None:
        ts = now_iso()
        rows = [(store_url, u, kind, ts) for kind in ("product", "other") for u in learned.get(kind, ())]
        self.db.executemany("INSERT OR REPLACE INTO page_memory(store_url, url, kind, seen_at) VALUES (?,?,?,?)", rows)
        self.db.commit()

    def variants(self, snapshot_id: int) -> dict[int, Variant]:
        out = {}
        for r in self.db.execute("SELECT * FROM variants WHERE snapshot_id=?", (snapshot_id,)):
            d = {f: r[f] for f in VARIANT_FIELDS}
            d["available"] = bool(d["available"])
            out[d["variant_id"]] = Variant(**d)
        return out

    def price_history(self, store_url: str, variant_id: int) -> list[tuple[str, float]]:
        return [(r["taken_at"], r["price"]) for r in self.db.execute(
            "SELECT s.taken_at, v.price FROM variants v JOIN snapshots s ON s.id=v.snapshot_id "
            "WHERE s.store_url=? AND v.variant_id=? ORDER BY s.id", (store_url, variant_id))]
