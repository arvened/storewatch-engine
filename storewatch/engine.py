"""One monitoring cycle: fetch every store, save, diff, report.

Stores are fetched in parallel threads (one polite HTTP client per store, so
the per-site delay is unchanged); everything touching the database happens
in the calling thread, in the stores' original order.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .diff import DiffConfig, catalog_summary, diff_snapshots
from .fetcher import CatalogFetcher, FetchError
from .report import StoreReport
from .storage import Storage, now_iso


def _fetch_one(fetcher, url: str, known: str | None, memory: dict | None,
               only: list[str] | None = None) -> dict:
    req0 = getattr(fetcher, "requests", 0)
    kw = {}
    if memory is not None:
        kw["memory"] = memory
    if only:
        kw["only"] = only
    try:
        platform, variants = fetcher.fetch(url, known, **kw)
    except (FetchError, ValueError) as e:
        return {"error": str(e), "requests": getattr(fetcher, "requests", 0) - req0}
    return {
        "platform": platform, "variants": variants,
        "learned": getattr(fetcher, "learned", None),
        "partial": bool(getattr(fetcher, "truncated", False)),
        "requests": getattr(fetcher, "requests", 0) - req0,
    }


def run_cycle(storage: Storage, fetcher: CatalogFetcher,
              cfg: DiffConfig | None = None, market: str | None = None,
              skip_platforms: tuple[str, ...] = (), workers: int = 1) -> list[StoreReport]:
    stores = storage.stores(market)
    jobs = []                                   # (store row, memory) in original order
    for st in stores:
        known = st["platform"]
        memory = None if known in skip_platforms else (
            storage.page_memory(st["url"]) if known in (None, "structured") else None)
        jobs.append((st, memory))

    parallel = workers > 1 and hasattr(fetcher, "clone") and len(jobs) > 1

    def work(job):
        st, memory = job
        if st["platform"] in skip_platforms:
            return {"skipped": True}
        f = fetcher.clone() if parallel else fetcher
        only = [w for w in (st["url_filter"] or "").split(",") if w] if "url_filter" in st.keys() else []
        return _fetch_one(f, st["url"], st["platform"], memory, only or None)

    if parallel:
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
            results = list(pool.map(work, jobs))
    else:
        results = [work(j) for j in jobs]

    reports: list[StoreReport] = []
    for (st, _), res in zip(jobs, results):
        url, name, known = st["url"], st["name"], st["platform"]
        if res.get("skipped"):
            reports.append(StoreReport(url, name, now_iso(), {}, None, platform=known,
                                       error="пропущен в режиме экономии минут"))
            continue
        if "error" in res:
            storage.save_error(url, res["error"])
            reports.append(StoreReport(url, name, now_iso(), {}, None, error=res["error"],
                                       platform=known or "", requests=res["requests"]))
            continue
        platform = res["platform"]
        if platform != known:
            storage.set_platform(url, platform)
        if res["learned"]:
            storage.save_page_memory(url, res["learned"])
        storage.save_snapshot(url, res["variants"], partial=res["partial"])
        snaps = storage.last_ok_snapshots(url, 2)
        after = storage.variants(snaps[0]["id"])
        diff = None
        if len(snaps) == 2:
            before = storage.variants(snaps[1]["id"])
            full = not (snaps[0]["partial"] or snaps[1]["partial"])
            diff = diff_snapshots(url, before, after, cfg, assortment=full)
        reports.append(StoreReport(url, name, snaps[0]["taken_at"], catalog_summary(after), diff,
                                   platform=platform, partial=res["partial"], requests=res["requests"]))
    return reports
