"""Compare two catalogue snapshots and rank what changed.

Rules only — no LLM. Every change gets a severity so the owner reads the
three things that matter first instead of three hundred rows.

Severity rules (thresholds are configurable):
  high   price drop >= big_move_pct; a new discount (compare-at price appears);
         a competitor product goes out of stock (your selling window)
  medium any other price change >= min_move_pct; new product;
         discount depth changed
  low    product removed; back in stock; sub-threshold price noise is ignored
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Variant

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class DiffConfig:
    min_move_pct: float = 1.0     # ignore price moves smaller than this
    big_move_pct: float = 10.0    # a drop this large is "high"


@dataclass
class Change:
    kind: str                     # price_down | price_up | discount_new | discount_end |
                                  # discount_changed | out_of_stock | back_in_stock |
                                  # product_new | product_removed
    severity: str
    label: str
    handle: str
    variant_id: int
    old: float | None = None
    new: float | None = None
    pct: float | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class DiffResult:
    store_url: str
    changes: list[Change] = field(default_factory=list)
    n_before: int = 0
    n_after: int = 0

    def by_severity(self, severity: str) -> list[Change]:
        return [c for c in self.changes if c.severity == severity]

    @property
    def counts(self) -> dict[str, int]:
        return {s: len(self.by_severity(s)) for s in SEVERITY_ORDER}


def _pct(old: float, new: float) -> float:
    return round((new - old) / old * 100, 1) if old else 0.0


def diff_snapshots(
    store_url: str,
    before: dict[int, Variant],
    after: dict[int, Variant],
    cfg: DiffConfig | None = None,
    assortment: bool = True,
) -> DiffResult:
    """assortment=False skips new/removed products — used when either snapshot read
    only part of the catalogue, so missing pages are not mistaken for removals."""
    cfg = cfg or DiffConfig()
    res = DiffResult(store_url, n_before=len(before), n_after=len(after))
    add = res.changes.append

    # Assortment changes are reported per product, not per variant.
    before_products = {v.product_id for v in before.values()}
    after_products = {v.product_id for v in after.values()}
    if not assortment:
        after_products = before_products = before_products & after_products
    new_products = after_products - before_products
    gone_products = before_products - after_products

    reported_new: set[int] = set()
    for v in after.values():
        if v.product_id in new_products and v.product_id not in reported_new:
            reported_new.add(v.product_id)
            add(Change("product_new", "medium", v.product_title, v.handle, v.variant_id,
                       new=v.price, detail=v.product_type or v.vendor))
    reported_gone: set[int] = set()
    for v in before.values():
        if v.product_id in gone_products and v.product_id not in reported_gone:
            reported_gone.add(v.product_id)
            add(Change("product_removed", "low", v.product_title, v.handle, v.variant_id,
                       old=v.price))

    for vid, new in after.items():
        old = before.get(vid)
        if old is None:
            continue

        od, nd = old.discount_pct, new.discount_pct
        sale_started = od is None and nd is not None

        # Price (a drop caused by a newly started sale is reported once, as the sale)
        if old.price != new.price and not (sale_started and new.price < old.price):
            pct = _pct(old.price, new.price)
            if abs(pct) >= cfg.min_move_pct:
                if pct < 0:
                    sev = "high" if -pct >= cfg.big_move_pct else "medium"
                    add(Change("price_down", sev, new.label, new.handle, vid, old.price, new.price, pct))
                else:
                    add(Change("price_up", "medium", new.label, new.handle, vid, old.price, new.price, pct))

        # Discount (compare-at price)
        if sale_started:
            add(Change("discount_new", "high", new.label, new.handle, vid,
                       new.compare_at_price, new.price, -nd, detail=f"-{nd}% от {new.compare_at_price:.2f}"))
        elif od is not None and nd is None:
            add(Change("discount_end", "medium", new.label, new.handle, vid,
                       old.price, new.price, detail=f"скидка {od}% снята"))
        elif od is not None and nd is not None and abs(od - nd) >= cfg.min_move_pct:
            add(Change("discount_changed", "medium", new.label, new.handle, vid,
                       detail=f"скидка {od}% → {nd}%"))

        # Stock
        if old.available and not new.available:
            add(Change("out_of_stock", "high", new.label, new.handle, vid, new=new.price))
        elif not old.available and new.available:
            add(Change("back_in_stock", "low", new.label, new.handle, vid, new=new.price))

    res.changes.sort(key=lambda c: (SEVERITY_ORDER[c.severity], -(abs(c.pct or 0)), c.label))
    return res


def catalog_summary(variants: dict[int, Variant]) -> dict:
    """Headline numbers for one snapshot."""
    vs = list(variants.values())
    if not vs:
        return {"products": 0, "variants": 0}
    prices = sorted(v.price for v in vs)
    on_sale = [v for v in vs if v.discount_pct]
    return {
        "products": len({v.product_id for v in vs}),
        "variants": len(vs),
        "in_stock_pct": round(sum(v.available for v in vs) / len(vs) * 100, 1),
        "on_sale_pct": round(len(on_sale) / len(vs) * 100, 1),
        "avg_discount_pct": round(sum(v.discount_pct for v in on_sale) / len(on_sale), 1) if on_sale else 0.0,
        "median_price": prices[len(prices) // 2],
        "min_price": prices[0],
        "max_price": prices[-1],
    }
