"""Command line: python -m storewatch <command>

  check <url> [--brands a,b]  detect the platform and show what can be read
                              (--brands: only pages of these brands)
                              (nothing is saved)
  add <url> [--name NAME] [--platform P]
                              start tracking a store (platform is detected
                              automatically on the first run)
  remove <url>                stop tracking a store
  sync <file>                 make the tracked list equal to a file
                              (one store per line: "url | name", # comments;
                              "url | name | brands: kimbo, borbone" reads
                              only pages of those brands)
  list                        show tracked stores
  weekly [--md FILE] [--html FILE] [--for URL] [--days N] [--sample]
                              weekly market report in Polish (comparison with
                              N days ago); --for adds a personal section for
                              one tracked store
  export --csv FILE [--market M] [--gtin]
                              latest prices of every tracked store as CSV
                              (one row per variant), for analysis
  run [--md FILE] [--json FILE] [--telegram] [--keep N]
                              fetch all stores, compare with the previous
                              snapshot, print a Markdown report;
                              --keep N deletes all but the newest N snapshots
                              per store afterwards

Platforms: shopify, woocommerce, structured (any other shop that publishes
standard product markup: Shoper, IdoSell, PrestaShop, Magento, ...).

Settings come from environment variables:
  STOREWATCH_DB         database path                     (default storewatch.db)
  STOREWATCH_DELAY      seconds between requests          (default 1.0)
  STOREWATCH_MAX_PAGES  max pages per store, universal    (default 300)
  STOREWATCH_MIN_PCT    ignore price moves below          (default 1.0)
  STOREWATCH_BIG_PCT    "high" price drop from            (default 10.0)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   for --telegram
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .diff import DiffConfig, catalog_summary
from .engine import run_cycle
from .fetcher import PLATFORM_LABELS, CatalogFetcher, FetchError, normalize_store_url
from .notify import send_telegram
from .platforms import ADAPTERS
from .report import to_json, to_markdown, to_telegram
from .storage import Storage


def parse_brands(text: str | None) -> list[str]:
    """'kimbo, caffe borbone' -> ['kimbo', 'caffe borbone']."""
    return [w.strip() for w in (text or "").split(",") if w.strip()]


def read_store_file(path: str, with_brands: bool = False) -> list[tuple]:
    """Parse a store list: 'url | name' per line, optionally '| brands: a, b' (only pages
    of those brands are read); blank lines and # comments ignored.
    Returns [(url, name)], or [(url, name, brands)] with `with_brands`."""
    out: list[tuple] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            url_part, _, rest = line.partition("|")
            name, _, extra = rest.partition("|")
            extra = extra.strip()
            if extra and not extra.lower().startswith("brands:"):
                raise ValueError(f"line {n}: третье поле должно начинаться с 'brands:'")
            brands = parse_brands(extra[len("brands:"):]) if extra else []
            try:
                url = normalize_store_url(url_part)
            except ValueError as e:
                raise ValueError(f"line {n}: {e}") from e
            if url not in seen:
                seen.add(url)
                out.append((url, name.strip() or None, brands) if with_brands
                           else (url, name.strip() or None))
    return out


def _verbose(http_get):
    """Wrap the transport to print each request: status, key headers, start of body."""
    def wrapped(url, headers, timeout):
        try:
            resp = http_get(url, headers, timeout)
        except Exception as e:
            print(f"  GET {url} -> ERROR {type(e).__name__}: {e}")
            raise
        h = {k.lower(): v for k, v in resp.headers.items()}
        info = ", ".join(f"{k}={h[k]}" for k in ("content-type", "server", "x-powered-by", "location") if k in h)
        hints = [n for n in ("shoptet", "shopify", "woocommerce", "prestashop", "shoper", "idosell", "wix",
                             "squarespace", "magento", "shopware", "webflow", "eshop-rychle", "upgates")
                 if n in resp.body[:200000].lower()]
        print(f"  GET {url} -> {resp.status} ({len(resp.body)} B) {info}" + (f" hints={hints}" if hints else ""))
        if resp.status != 200 or not resp.body.lstrip().startswith(("{", "[", "<?xml", "<urlset", "<sitemapindex")):
            snippet = " ".join(resp.body[:160].split())
            if snippet:
                print(f"      body: {snippet}")
        return resp
    return wrapped


def _check_one(fetcher: CatalogFetcher, url: str, platform: str | None,
               brands: list[str] | None = None) -> int:
    try:
        platform, variants = fetcher.fetch(url, platform, only=brands or None)
    except (FetchError, ValueError) as e:
        print(f"Не подходит: {e}")
        return 4
    s = catalog_summary({v.variant_id: v for v in variants})
    print(f"Подходит: {normalize_store_url(url)}")
    print(f"Платформа: {PLATFORM_LABELS.get(platform, platform)}")
    print(f"Товаров {s['products']}, вариантов {s['variants']}, в наличии {s['in_stock_pct']}%, "
          f"на распродаже {s['on_sale_pct']}%")
    cand = fetcher.client.notes.get("candidates")
    if cand:
        print(f"Страниц-кандидатов в карте сайта: {cand}" + (" (прочитана часть)" if fetcher.truncated else ""))
    for v in variants[:5]:
        sale = f" (было {v.compare_at_price:.2f})" if v.discount_pct else ""
        stock = "" if v.available else " — нет в наличии"
        print(f"  • {v.label}: {v.price:.2f}{sale}{stock}")
    return 0


def _make_fetcher() -> CatalogFetcher:
    return CatalogFetcher(
        delay=float(os.environ.get("STOREWATCH_DELAY", "1.0")),
        max_pages=int(os.environ.get("STOREWATCH_MAX_PAGES", "300")),
    )


def main(argv: list[str] | None = None, fetcher: CatalogFetcher | None = None) -> int:
    p = argparse.ArgumentParser(prog="storewatch", description="Competitor monitoring for online stores")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("url", nargs="+")
    c.add_argument("--platform", choices=sorted(ADAPTERS))
    c.add_argument("--brands", help="read only pages of these brands, comma-separated")
    c.add_argument("-v", "--verbose", action="store_true", help="print every HTTP request (diagnostics)")
    a = sub.add_parser("add"); a.add_argument("url"); a.add_argument("--name")
    a.add_argument("--market", help="market code, e.g. pl, de")
    a.add_argument("--platform", choices=sorted(ADAPTERS))
    r = sub.add_parser("remove"); r.add_argument("url")
    sy = sub.add_parser("sync"); sy.add_argument("file")
    sy.add_argument("--market", help="sync only this market (other markets untouched)")
    ls = sub.add_parser("list"); ls.add_argument("--market")
    wk = sub.add_parser("weekly")
    wk.add_argument("--md"); wk.add_argument("--html")
    wk.add_argument("--for", dest="focus", help="tracked store to personalise the report for")
    wk.add_argument("--days", type=int, default=7)
    wk.add_argument("--sample", action="store_true", help="mark the report as an example")
    wk.add_argument("--market")
    wk.add_argument("--context", help="JSON from the 'context' command (arabica, FX)")
    wk.add_argument("--currency", default="PLN", help="local currency for the FX line (PLN, CZK, EUR)")
    cx = sub.add_parser("context"); cx.add_argument("--out", help="write JSON here")
    ex = sub.add_parser("export"); ex.add_argument("--csv", required=True); ex.add_argument("--market")
    ex.add_argument("--gtin", action="store_true", help="add a last column with the barcode (EAN/GTIN)")
    ex.add_argument("--all", action="store_true", help="every stored snapshot, not only the latest")
    run = sub.add_parser("run")
    run.add_argument("--md", help="write Markdown report to file")
    run.add_argument("--json", help="write JSON report to file")
    run.add_argument("--telegram", action="store_true", help="send high-severity alerts")
    run.add_argument("--keep", type=int, help="keep only the newest N snapshots per store")
    run.add_argument("--market", help="only this market's stores")
    run.add_argument("--usage", help="append time and request counts to this CSV")
    run.add_argument("--workers", type=int, default=int(os.environ.get("STOREWATCH_WORKERS", "4")),
                     help="stores read in parallel (each site still gets its own polite delay)")
    run.add_argument("--only-fast", action="store_true",
                     help="skip stores read in the slow universal mode (budget saving)")
    us = sub.add_parser("usage")
    us.add_argument("--file", required=True)
    us.add_argument("--budget", type=float, default=1800, help="monthly minutes budget")
    us.add_argument("--md", help="write a Markdown summary here")
    us.add_argument("--mode-only", action="store_true", help="print only the mode")
    args = p.parse_args(argv)

    if args.cmd == "usage":
        from .usage import load, summarize, to_markdown as usage_md
        s = summarize(load(args.file), args.budget)
        if args.md:
            with open(args.md, "w", encoding="utf-8") as f:
                f.write(usage_md(s))
        print(s["mode"] if args.mode_only else json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "context":
        from .context import build_context
        ctx = build_context()
        text = json.dumps(ctx, ensure_ascii=False, indent=2)
        print(text)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
        return 0 if not ctx["errors"] else 4

    if args.cmd == "check":
        fetcher = fetcher or _make_fetcher()
        if args.verbose:
            fetcher.client.http_get = _verbose(fetcher.client.http_get)
        worst = 0
        for url in args.url:
            if len(args.url) > 1:
                print(f"\n=== {url}")
            worst = max(worst, _check_one(fetcher, url, args.platform, parse_brands(args.brands)))
        return worst

    db = Storage(os.environ.get("STOREWATCH_DB", "storewatch.db"))
    try:
        if args.cmd == "add":
            url = normalize_store_url(args.url)
            added = db.add_store(url, args.name, args.platform, args.market)
            print(("Добавлен: " if added else "Уже отслеживается: ") + url)
        elif args.cmd == "remove":
            url = normalize_store_url(args.url)
            print(("Удалён: " if db.remove_store(url) else "Не найден: ") + url)
        elif args.cmd == "sync":
            try:
                wanted = read_store_file(args.file, with_brands=True)
            except (OSError, ValueError) as e:
                print(f"Ошибка в файле магазинов: {e}", file=sys.stderr)
                return 1
            added, removed = db.sync_stores(wanted, args.market)
            print(f"Магазинов в списке: {len(wanted)}; добавлено {len(added)}, убрано {len(removed)}")
            for u in added:
                print(f"  + {u}")
            for u in removed:
                print(f"  - {u}")
        elif args.cmd == "weekly":
            from .weekly import build_weekly, to_html as weekly_html, to_markdown as weekly_md
            focus = normalize_store_url(args.focus) if args.focus else None
            if focus and focus not in {s["url"] for s in db.stores(args.market)}:
                print(f"Магазин {focus} не отслеживается", file=sys.stderr)
                return 1
            rep = build_weekly(db, days=args.days, focus=focus, market=args.market)
            rep.currency = args.currency.upper()
            if args.context:
                try:
                    with open(args.context, encoding="utf-8") as f:
                        rep.context = json.load(f)
                except (OSError, ValueError) as e:
                    print(f"Контекст не прочитан: {e}", file=sys.stderr)
            md = weekly_md(rep, sample=args.sample)
            print(md)
            if args.md:
                with open(args.md, "w", encoding="utf-8") as f:
                    f.write(md)
            if args.html:
                with open(args.html, "w", encoding="utf-8") as f:
                    f.write(weekly_html(rep, sample=args.sample))
        elif args.cmd == "export":
            import csv
            cols = ["market", "store_url", "store", "taken_at", "product_title", "variant_title", "handle",
                    "vendor", "product_type", "price", "compare_at_price", "available",
                    "weight_g", "price_per_kg", "tags", "created_at"] + (["gtin"] if args.gtin else [])
            import gzip
            n = 0
            # '.gz' -> compressed file (the permanent daily price history in the data branch).
            opener = (lambda p: gzip.open(p, "wt", encoding="utf-8", newline="")) if args.csv.endswith(".gz") \
                else (lambda p: open(p, "w", encoding="utf-8", newline=""))
            with opener(args.csv) as f:
                w = csv.writer(f)
                w.writerow(cols)
                for s in db.stores(args.market):
                    # --all: every stored snapshot, oldest first (one-time backfill of history).
                    snaps = db.last_ok_snapshots(s["url"], 10 ** 6 if args.all else 1)
                    for snap in reversed(snaps):
                        for v in db.variants(snap["id"]).values():
                            w.writerow([s["market"] or "", s["url"], s["name"], snap["taken_at"],
                                        v.product_title, v.variant_title, v.handle, v.vendor, v.product_type,
                                        v.price, "" if v.compare_at_price is None else v.compare_at_price,
                                        int(v.available), v.weight_g or "",
                                        "" if v.price_per_kg is None else v.price_per_kg,
                                        v.tags, v.created_at, *([v.gtin] if args.gtin else [])])
                            n += 1
            print(f"Выгружено строк: {n}")
        elif args.cmd == "list":
            stores = db.stores(args.market)
            if not stores:
                print("Нет магазинов. Добавьте: python -m storewatch add <url>")
            for s in stores:
                plat = PLATFORM_LABELS.get(s["platform"], s["platform"]) if s["platform"] else "не определена"
                print(f"{s['url']}\t{s['name']}\t{plat}\t{s['market'] or '-'}")
        elif args.cmd == "run":
            if not db.stores(args.market):
                print("Нет магазинов. Добавьте: python -m storewatch add <url>", file=sys.stderr)
                return 1
            fetcher = fetcher or _make_fetcher()
            cfg = DiffConfig(
                min_move_pct=float(os.environ.get("STOREWATCH_MIN_PCT", "1.0")),
                big_move_pct=float(os.environ.get("STOREWATCH_BIG_PCT", "10.0")),
            )
            t0 = time.monotonic()
            reports = run_cycle(db, fetcher, cfg, args.market,
                                skip_platforms=("structured",) if args.only_fast else (),
                                workers=args.workers)
            if args.usage:
                from .usage import record
                record(args.usage, args.market or "", time.monotonic() - t0,
                       sum(r.requests for r in reports), len(reports))
            md = to_markdown(reports)
            print(md)
            if args.md:
                with open(args.md, "w", encoding="utf-8") as f:
                    f.write(md)
            if args.json:
                with open(args.json, "w", encoding="utf-8") as f:
                    f.write(to_json(reports))
            if args.keep:
                db.prune(args.keep)
            if args.telegram:
                token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
                if not (token and chat):
                    print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы", file=sys.stderr)
                    return 2
                text = to_telegram(reports)
                if text and not send_telegram(token, chat, text):
                    print("Не удалось отправить в Telegram", file=sys.stderr)
                    return 3
            return 0 if all(not r.error for r in reports) else 4
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
