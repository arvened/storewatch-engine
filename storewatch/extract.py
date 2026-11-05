"""Extract products from an HTML page using the standards shops publish for Google.

Three sources, tried in order:
  1. JSON-LD  (<script type="application/ld+json"> with schema.org Product /
     ProductGroup, Offer / AggregateOffer, strikethrough price spec);
  2. Microdata (itemtype=".../Product" with itemprop name/price/availability);
  3. Open Graph product meta (product:price:amount, product:availability).

Pure functions over a string — no network.
"""

from __future__ import annotations

import html
import json
from html.parser import HTMLParser

from .models import Variant, parse_price, stable_id

OUT_OF_STOCK = ("outofstock", "soldout", "discontinued", "out of stock", "oos")


def _avail(value) -> bool:
    if value is None:
        return True
    v = str(value).strip().lower().replace("https://schema.org/", "").replace("http://schema.org/", "")
    v = v.replace("_", "").replace("-", "")
    return not any(v.startswith(x.replace(" ", "")) or v == x for x in OUT_OF_STOCK)


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    ts = t if isinstance(t, list) else [t]
    out = set()
    for x in ts:
        if isinstance(x, str):
            out.add(x.rsplit("/", 1)[-1].rsplit(":", 1)[-1])
    return out


def _text(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("@id") or ""
    if isinstance(v, list):
        v = v[0] if v else ""
    return html.unescape(str(v or "")).strip()


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld: list[str] = []
        self._in_ld = False
        self._buf: list[str] = []
        self.meta: dict[str, str] = {}
        self.micro: dict[str, str] = {}
        self._in_product = False
        self._pending: str | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._buf = True, []
            return
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and key not in self.meta and "content" in a:
                self.meta[key] = a["content"]
        if "product" in a.get("itemtype", "").lower().rsplit("/", 1)[-1]:
            self._in_product = True
        prop = a.get("itemprop", "").lower()
        if self._in_product and prop in ("name", "price", "pricecurrency", "availability", "sku", "brand") \
                and prop not in self.micro:
            val = a.get("content") or a.get("href") or a.get("value")
            if val:
                self.micro[prop] = val
            else:
                self._pending = prop

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ld.append("".join(self._buf))
            self._in_ld = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._pending and data.strip():
            self.micro.setdefault(self._pending, data.strip())
            self._pending = None


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "mainEntity", "itemListElement", "item"):
            if k in node:
                yield from _walk(node[k])


def _offer_rows(offers) -> list[dict]:
    rows = []
    for o in (offers if isinstance(offers, list) else [offers]):
        if not isinstance(o, dict):
            continue
        if "AggregateOffer" in _types(o) and o.get("offers"):
            rows.extend(_offer_rows(o["offers"]))
            continue
        price = parse_price(o.get("price"))
        compare = None
        specs = o.get("priceSpecification")
        for s in (specs if isinstance(specs, list) else [specs] if specs else []):
            if not isinstance(s, dict):
                continue
            ptype = str(s.get("priceType", "")).lower()
            p = parse_price(s.get("price"))
            if "strikethrough" in ptype or "listprice" in ptype:
                compare = p
            elif price is None:
                price = p
        if price is None:
            price = parse_price(o.get("lowPrice"))
        if price is None:
            continue
        rows.append({
            "price": price,
            "compare": compare if compare and compare > price else None,
            "available": _avail(o.get("availability")),
            "sku": _text(o.get("sku")),
            "name": _text(o.get("name")),
        })
    return rows


def _from_product(node: dict, url: str, group_name: str | None = None) -> list[Variant]:
    name = _text(node.get("name")) or group_name or url
    pid_key = group_name and f"{url}#group" or f"{url}#{_text(node.get('sku')) or _text(node.get('@id'))}"
    rows = _offer_rows(node.get("offers"))
    out = []
    for i, r in enumerate(rows):
        vt = ""
        if group_name:
            vt = name if name != group_name else ""
        elif len(rows) > 1:
            vt = r["name"] if r["name"] and r["name"] != name else (r["sku"] or f"#{i + 1}")
        sku = r["sku"] or _text(node.get("sku"))
        out.append(Variant(
            product_id=stable_id(pid_key),
            variant_id=stable_id(f"{url}#{sku or ''}#{vt}#{i if not sku else ''}"),
            product_title=group_name or name,
            variant_title=vt,
            handle=url,
            sku=sku,
            vendor=_text(node.get("brand")),
            product_type=_text(node.get("category")),
            price=r["price"],
            compare_at_price=r["compare"],
            available=r["available"],
        ))
    return out


def products_from_html(html: str, url: str) -> list[Variant]:
    c = _Collector()
    try:
        c.feed(html)
        c.close()
    except Exception:          # malformed HTML: use whatever was collected
        pass

    # 1. JSON-LD
    out: list[Variant] = []
    for raw in c.ld:
        raw = raw.strip().removeprefix("<!--").removesuffix("-->").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            types = _types(node)
            if "ProductGroup" in types:
                gname = _text(node.get("name")) or url
                for v in node.get("hasVariant") or []:
                    if isinstance(v, dict):
                        out.extend(_from_product(v, url, group_name=gname))
            elif "Product" in types:
                out.extend(_from_product(node, url))
    if out:
        seen, uniq = set(), []
        for v in out:
            if v.variant_id not in seen:
                seen.add(v.variant_id)
                uniq.append(v)
        return uniq

    # 2. Microdata
    m = c.micro
    price = parse_price(m.get("price"))
    if price is not None and m.get("name"):
        return [Variant(stable_id(url), stable_id(url + "#"), m["name"], "", url, m.get("sku", ""),
                        m.get("brand", ""), "", price, None, _avail(m.get("availability")))]

    # 3. Open Graph product meta
    meta = c.meta
    price = parse_price(meta.get("product:price:amount") or meta.get("og:price:amount"))
    title = meta.get("og:title") or meta.get("twitter:title")
    if price is not None and title:
        compare = parse_price(meta.get("product:original_price:amount"))
        return [Variant(stable_id(url), stable_id(url + "#"), title.strip(), "", url,
                        meta.get("product:retailer_item_id", ""), meta.get("product:brand", ""), "",
                        price, compare if compare and compare > price else None,
                        _avail(meta.get("product:availability") or meta.get("og:availability")))]
    return []
