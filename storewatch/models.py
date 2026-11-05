"""Shared data model: one sellable variant as seen in a snapshot."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass
class Variant:
    product_id: int
    variant_id: int
    product_title: str
    variant_title: str
    handle: str              # Shopify handle, Woo slug or page URL
    sku: str
    vendor: str
    product_type: str
    price: float
    compare_at_price: float | None
    available: bool
    # Extra facts kept for the price history (origin, process, certificates, weight, launch date).
    tags: str = ""                 # comma-separated: platform tags + attributes ("Herkunft: Peru")
    grams: int | None = None       # pack weight when the platform states it
    created_at: str = ""           # when the product first appeared in the shop, if published

    @property
    def weight_g(self) -> int | None:
        """Pack weight in grams: stated by the platform, else read from the titles."""
        if self.grams and self.grams > 0:
            return int(self.grams)
        return parse_grams(f"{self.variant_title} {self.product_title}")

    @property
    def price_per_kg(self) -> float | None:
        w = self.weight_g
        return round(self.price * 1000 / w, 2) if w and self.price > 0 else None

    @property
    def label(self) -> str:
        if self.variant_title and self.variant_title != "Default Title":
            return f"{self.product_title} — {self.variant_title}"
        return self.product_title

    @property
    def discount_pct(self) -> float | None:
        if self.compare_at_price and self.compare_at_price > self.price > 0:
            return round((1 - self.price / self.compare_at_price) * 100, 1)
        return None


def stable_id(key: str) -> int:
    """Deterministic 60-bit id for platforms without numeric ids (fits SQLite INTEGER)."""
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:15], 16)


_NUM = re.compile(r"-?\d[\d\s .,']*")


def parse_price(value) -> float | None:
    """'69.00', '69,00', '1 299,00 zł', 1299, '1,299.00' -> float. None if not a price."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2) if value >= 0 else None
    m = _NUM.search(str(value))
    if not m:
        return None
    s = re.sub(r"[\s ']", "", m.group(0)).rstrip(".,")
    if "," in s and "." in s:
        # the later separator is the decimal one
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) != 3 else s.replace(",", "")
    elif s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = f"{head.replace('.', '')}.{tail}" if len(tail) != 3 else s.replace(".", "")
    try:
        v = round(float(s), 2)
    except ValueError:
        return None
    return v if v >= 0 else None


_WEIGHT = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(kg|kilo|kilogram|g|gr|gram|gramm|grams|gramów|lb|lbs|oz)\b", re.I)
_MULT = {"kg": 1000, "kilo": 1000, "kilogram": 1000, "lb": 453.6, "lbs": 453.6, "oz": 28.35}


def parse_grams(text: str) -> int | None:
    """'1 kilo' -> 1000, '250g' -> 250, '2 x 250g' -> 500, '0,5 kg' -> 500; None if absent."""
    m = _WEIGHT.search(text or "")
    if not m:
        return None
    grams = float(m.group(1).replace(",", ".")) * _MULT.get(m.group(2).lower(), 1)
    k = re.search(r"(\d+)\s*[x×]\s*$", text[:m.start()])
    if k:
        grams *= int(k.group(1))
    return int(round(grams)) if 5 <= grams <= 100000 else None
