"""Usage control: how many GitHub Actions minutes the daily runs cost.

Each `run --usage FILE` appends one row per market: date, market, seconds,
requests, stores. `usage` sums the current month, projects it to the month
end and picks a mode:

  normal  — projection within the budget
  saver   — projection above SAVER_SHARE of the budget: fewer pages per store
  minimal — month-to-date above MINIMAL_SHARE of the budget: only stores read
            through fast APIs (Shopify, WooCommerce); universal-mode stores wait

GitHub bills a job by the minute, rounded up, plus setup time; RUN_OVERHEAD_MIN
covers checkout, Python setup and cache per run.
"""

from __future__ import annotations

import calendar
import csv
import os
from datetime import date, datetime, timezone

FIELDS = ["date", "market", "seconds", "requests", "stores", "run"]
RUN_OVERHEAD_MIN = 1.0
SAVER_SHARE = 0.8
MINIMAL_SHARE = 0.9


def record(path: str, market: str, seconds: float, requests: int, stores: int,
           run: str | None = None) -> None:
    """Append one row. `run` groups rows of one workflow run (GITHUB_RUN_ID by default)."""
    run = run if run is not None else os.environ.get("GITHUB_RUN_ID", "")
    if os.path.exists(path):                       # upgrade a log written before the "run" column
        old = load(path)
        if old and "run" not in old[0]:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, restval="", extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([datetime.now(timezone.utc).date().isoformat(), market or "-",
                    round(seconds, 1), requests, stores, run])


def load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("date")]


def summarize(rows: list[dict], budget: float, today: date | None = None, recent_runs: int = 7) -> dict:
    """Spent this month, and a projection: spent + typical daily run × days left.
    Rows with stores=0 are one-off costs (setup, manual runs): counted as spent,
    never extrapolated. The typical run is the average of the last `recent_runs`."""
    today = today or datetime.now(timezone.utc).date()
    month = today.strftime("%Y-%m")
    rows = [r for r in rows if r["date"].startswith(month)]
    runs: dict[str, float] = {}
    for r in rows:
        if int(r.get("stores") or 0) == 0:
            continue
        key = r.get("run") or r["date"]          # old rows without run id: one run per day
        runs[key] = runs.get(key, 0.0) + float(r["seconds"])
    run_minutes = [sec / 60 + RUN_OVERHEAD_MIN for sec in runs.values()]
    one_off = sum(float(r["seconds"]) for r in rows if int(r.get("stores") or 0) == 0) / 60
    minutes = one_off + sum(run_minutes)
    per_day = (sum(run_minutes[-recent_runs:]) / len(run_minutes[-recent_runs:])) if run_minutes else 0.0
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    remaining_days = days_in_month - today.day
    projected = minutes + per_day * remaining_days
    days_run = sorted({r["date"] for r in rows})
    per_market: dict[str, dict] = {}
    for r in rows:
        m = per_market.setdefault(r["market"], {"seconds": 0.0, "requests": 0, "runs": 0})
        m["seconds"] += float(r["seconds"])
        m["requests"] += int(r["requests"])
        m["runs"] += 1
    if minutes > budget * MINIMAL_SHARE:
        mode = "minimal"
    elif projected > budget * SAVER_SHARE:
        mode = "saver"
    else:
        mode = "normal"
    return {"month": month, "budget": budget, "minutes": round(minutes, 1), "per_day": round(per_day, 1),
            "projected": round(projected, 1), "days_run": len(days_run), "runs": len(runs), "mode": mode,
            "per_market": per_market}


MODE_RU = {"normal": "обычный", "saver": "экономия (меньше страниц на магазин)",
           "minimal": "минимальный (только быстрые магазины на Shopify/WooCommerce)"}


def to_markdown(s: dict) -> str:
    pct = s["projected"] / s["budget"] * 100 if s["budget"] else 0
    lines = [
        "## Расход минут GitHub Actions",
        f"{s['month']}: потрачено **{s['minutes']:.0f}** мин ({s['runs']} прогонов), обычный прогон "
        f"≈{s['per_day']:.0f} мин, прогноз на месяц **{s['projected']:.0f}** из {s['budget']:.0f} "
        f"({pct:.0f}%). Режим: **{MODE_RU[s['mode']]}**.",
        "",
        "| Рынок | Минут | Запросов | Запусков |",
        "|---|---:|---:|---:|",
    ]
    for m, v in sorted(s["per_market"].items(), key=lambda kv: -kv[1]["seconds"]):
        lines.append(f"| {m} | {v['seconds'] / 60:.1f} | {v['requests']} | {v['runs']} |")
    lines.append("")
    lines.append("_Учитываются ежедневные прогоны; ручные проверки (Check stores) — сверх этого._")
    return "\n".join(lines) + "\n"
