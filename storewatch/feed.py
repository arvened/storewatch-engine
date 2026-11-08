"""Price & stock feed in Ceneo XML — the format Apilo, BaseLinker and Shoper import.

Use case: a shop sells goods of a supplier that publishes no XML file. The engine reads the
supplier's public website every day; this module turns the latest snapshot into an offers
feed the shop can connect to its integrator (matched by EAN / producer code).

Prices can be converted (fixed rate or NBP mid rate) and marked up:
    shop price = supplier price × rate × (1 + markup%) + add
"""

from __future__ import annotations

import json
import urllib.request
from xml.sax.saxutils import escape, quoteattr

from .models import Variant


def nbp_rate(currency: str, timeout: float = 30) -> float:
    """Current NBP mid rate (table A) for `currency` in PLN, e.g. nbp_rate('EUR') -> 4.27."""
    url = f"https://api.nbp.pl/api/exchangerates/rates/a/{currency.lower()}/?format=json"
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "ARVEN-StoreWatch/0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return float(json.load(r)["rates"][0]["mid"])


def product_url(store_url: str, platform: str | None, handle: str) -> str:
    if handle.startswith("http"):
        return handle
    base = store_url.rstrip("/")
    if platform == "shopify":
        return f"{base}/products/{handle}"
    if platform == "woocommerce":
        return f"{base}/product/{handle}/"
    return f"{base}/{handle.lstrip('/')}"


def shop_price(price: float, rate: float = 1.0, markup_pct: float = 0.0, add: float = 0.0) -> float:
    return round(price * rate * (1 + markup_pct / 100) + add, 2)


def _cdata(s: str) -> str:
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def ceneo_xml(items: list[tuple[str, str | None, Variant]], rate: float = 1.0,
              markup_pct: float = 0.0, add: float = 0.0) -> str:
    """items: (store_url, platform, variant). Returns the whole XML document."""
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<offers xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="1">',
           '<group name="other">']
    for store_url, platform, v in items:
        name = v.label
        attrs = []
        if v.vendor:
            attrs.append(f'<a name="Producent">{_cdata(v.vendor)}</a>')
        if v.gtin:
            attrs.append(f'<a name="EAN">{_cdata(v.gtin)}</a>')
        if v.sku:
            attrs.append(f'<a name="Kod_producenta">{_cdata(v.sku)}</a>')
        out.append(
            f'<o id={quoteattr(str(v.variant_id))} url={quoteattr(product_url(store_url, platform, v.handle))} '
            f'price="{shop_price(v.price, rate, markup_pct, add):.2f}" '
            f'avail="{1 if v.available else 99}" stock="{1 if v.available else 0}">'
            f'<cat>{_cdata(v.product_type or "Inne")}</cat><name>{_cdata(name)}</name>'
            + (f"<attrs>{''.join(attrs)}</attrs>" if attrs else "") + "</o>")
    out += ["</group>", "</offers>", ""]
    return "\n".join(out)


__all__ = ["ceneo_xml", "nbp_rate", "product_url", "shop_price", "escape"]
