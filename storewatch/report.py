"""Reports: Markdown (for people), JSON (for other systems), Telegram text."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .diff import Change, DiffResult

PLATFORM_LABELS = {"shopify": "Shopify", "woocommerce": "WooCommerce", "structured": "универсальный режим"}  # keep in sync with fetcher

KIND_RU = {
    "price_down": "Снижение цены",
    "price_up": "Рост цены",
    "discount_new": "Новая скидка",
    "discount_end": "Скидка снята",
    "discount_changed": "Скидка изменена",
    "out_of_stock": "Нет в наличии",
    "back_in_stock": "Снова в наличии",
    "product_new": "Новый товар",
    "product_removed": "Товар убран",
}
SEV_RU = {"high": "🔴 Важно", "medium": "🟡 Заметно", "low": "⚪ Фон"}


@dataclass
class StoreReport:
    store_url: str
    name: str
    taken_at: str
    summary: dict
    diff: DiffResult | None        # None on first snapshot (baseline)
    error: str | None = None
    platform: str = ""
    partial: bool = False          # only part of the catalogue was read
    requests: int = 0              # HTTP requests spent on this store


def _money(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f}".replace(",", " ")


def _change_line(c: Change) -> str:
    kind = KIND_RU[c.kind]
    if c.kind in ("price_down", "price_up"):
        return f"{kind}: **{c.label}** {_money(c.old)} → {_money(c.new)} ({c.pct:+.1f}%)"
    if c.kind == "discount_new":
        return f"{kind}: **{c.label}** {c.detail}, цена {_money(c.new)}"
    if c.kind in ("discount_end", "discount_changed"):
        return f"{kind}: **{c.label}** ({c.detail})"
    if c.kind in ("out_of_stock", "back_in_stock"):
        return f"{kind}: **{c.label}** (цена {_money(c.new)})"
    if c.kind == "product_new":
        extra = f", {c.detail}" if c.detail else ""
        return f"{kind}: **{c.label}** — {_money(c.new)}{extra}"
    return f"{kind}: **{c.label}**"


def to_markdown(reports: list[StoreReport], max_per_severity: int = 25) -> str:
    lines = ["# Мониторинг конкурентов", ""]
    total = {"high": 0, "medium": 0, "low": 0}
    for r in reports:
        if r.diff:
            for k, v in r.diff.counts.items():
                total[k] += v
    lines.append(
        f"Магазинов: {len(reports)} · "
        f"🔴 {total['high']} · 🟡 {total['medium']} · ⚪ {total['low']}"
    )
    lines.append("")

    for r in reports:
        lines.append(f"## {r.name}")
        plat = f" · {PLATFORM_LABELS.get(r.platform, r.platform)}" if r.platform else ""
        if r.partial:
            plat += " · каталог прочитан частично (новинки и пропажи не считаются)"
        lines.append(f"{r.store_url}{plat} · снимок {r.taken_at}")
        lines.append("")
        if r.error:
            lines.append(f"⚠️ Не удалось прочитать магазин: {r.error}")
            lines.append("")
            continue
        s = r.summary
        if s.get("variants"):
            lines.append(
                f"Товаров {s['products']}, вариантов {s['variants']} · "
                f"в наличии {s['in_stock_pct']}% · на распродаже {s['on_sale_pct']}% "
                f"(средняя скидка {s['avg_discount_pct']}%) · "
                f"медианная цена {_money(s['median_price'])}"
            )
            lines.append("")
        if r.diff is None:
            lines.append("Первый снимок сохранён — изменения появятся со следующего запуска.")
            lines.append("")
            continue
        if not r.diff.changes:
            lines.append("Изменений нет.")
            lines.append("")
            continue
        for sev in ("high", "medium", "low"):
            items = r.diff.by_severity(sev)
            if not items:
                continue
            lines.append(f"### {SEV_RU[sev]} ({len(items)})")
            for c in items[:max_per_severity]:
                lines.append(f"- {_change_line(c)}")
            if len(items) > max_per_severity:
                lines.append(f"- … и ещё {len(items) - max_per_severity}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json(reports: list[StoreReport]) -> str:
    return json.dumps([{
        "store_url": r.store_url,
        "name": r.name,
        "taken_at": r.taken_at,
        "error": r.error,
        "platform": r.platform,
        "partial": r.partial,
        "requests": r.requests,
        "summary": r.summary,
        "baseline": r.diff is None and not r.error,
        "counts": r.diff.counts if r.diff else None,
        "changes": [c.to_dict() for c in r.diff.changes] if r.diff else [],
    } for r in reports], ensure_ascii=False, indent=2)


def to_telegram(reports: list[StoreReport], max_items: int = 10) -> str | None:
    """Short alert with only high-severity changes. None if nothing to say."""
    blocks = []
    for r in reports:
        if r.error:
            blocks.append(f"⚠️ {r.name}: {r.error}")
            continue
        if not r.diff:
            continue
        high = r.diff.by_severity("high")
        if not high:
            continue
        rows = [f"🔴 {r.name} — {len(high)} важн."]
        for c in high[:max_items]:
            rows.append("• " + _change_line(c).replace("**", ""))
        if len(high) > max_items:
            rows.append(f"• … и ещё {len(high) - max_items}")
        blocks.append("\n".join(rows))
    if not blocks:
        return None
    text = "\n\n".join(blocks)
    return text[:4000]   # Telegram limit is 4096 chars
