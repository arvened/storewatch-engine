"""Read a store's catalogue on any supported platform.

Platform is detected automatically (Shopify → WooCommerce → universal
structured-data reader) and remembered per store, so later runs skip
detection.
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Callable
from urllib.parse import urlparse

from .http import (USER_AGENT, FetchError, HttpClient, HttpGet, HttpResponse,  # noqa: F401
                   NotThisPlatform, urllib_get)
from .models import Variant  # noqa: F401
from .platforms import ADAPTERS, DETECT_ORDER
from .platforms.shopify import parse_products  # noqa: F401  (backwards compatibility)

PLATFORM_LABELS = {"shopify": "Shopify", "woocommerce": "WooCommerce", "structured": "универсальный режим"}


def normalize_store_url(url: str) -> str:
    """'mystore.com/collections/x' -> 'https://mystore.com'."""
    url = url.strip()
    if not url:
        raise ValueError("empty store URL")
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    host = p.hostname or ""
    if p.scheme not in ("http", "https") or not host or "." not in host \
            or any(c.isspace() for c in p.netloc) or not all(part for part in host.split(".")):
        raise ValueError(f"invalid store URL: {url}")
    try:
        p.port
    except ValueError as e:
        raise ValueError(f"invalid store URL: {url}") from e
    return f"{p.scheme}://{p.netloc.lower()}"


def _norm(text: str) -> str:
    """'Caffè Borbone' -> '-caffe-borbone-' (for whole-word matching in URLs and titles)."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return "-" + re.sub(r"[^a-z0-9]+", "-", t).strip("-") + "-"


def brand_matcher(words: list[str] | None):
    """Return text -> bool that is True when any brand occurs as whole words; None if no filter."""
    keys = [_norm(w) for w in (words or []) if _norm(w) != "--"]
    if not keys:
        return None
    return lambda text: any(k in _norm(text) for k in keys)


class CatalogFetcher:
    def __init__(
        self,
        http_get: HttpGet = urllib_get,
        delay: float = 1.0,
        timeout: float = 20.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        max_pages: int = 300,
    ):
        self._args = (http_get, delay, timeout, retries, sleep, max_pages)
        self.client = HttpClient(http_get, delay, timeout, retries, sleep)
        self.max_pages = max_pages
        self._memory: dict | None = None
        self._only: list[str] | None = None

    def clone(self) -> "CatalogFetcher":
        """A fresh fetcher with the same settings, for reading another store in parallel."""
        return CatalogFetcher(*self._args)

    @property
    def truncated(self) -> bool:
        """True if the last successful fetch read only part of the catalogue."""
        return bool(self.client.notes.get("truncated"))

    @property
    def learned(self) -> dict | None:
        """Pages classified by the universal reader during the last fetch."""
        return self.client.notes.get("learned")

    @property
    def requests(self) -> int:
        return self.client.requests

    def _run(self, name: str, store: str) -> list[Variant]:
        self.client.notes = {"memory": self._memory} if self._memory else {}
        match = brand_matcher(self._only)
        if match:
            self.client.notes["only"] = match          # the universal reader skips other pages
        variants = ADAPTERS[name].fetch(self.client, store, self.max_pages)
        if match:
            variants = [v for v in variants
                        if match(" ".join((v.product_title, v.vendor, v.handle, v.product_type)))]
        if not variants:
            # An empty catalogue would look like "everything was removed".
            raise NotThisPlatform("catalogue is empty")
        return variants

    def fetch(self, store_url: str, platform: str | None = None,
              memory: dict | None = None, only: list[str] | None = None) -> tuple[str, list[Variant]]:
        """Return (platform, variants). Raises FetchError if nothing works.
        `memory` — pages classified on earlier runs (see Storage.page_memory).
        `only` — brand words: keep only products whose page address or title names one."""
        self._memory = memory
        self._only = only
        store = normalize_store_url(store_url)
        if platform and platform not in ADAPTERS:
            raise ValueError(f"unknown platform: {platform}")
        reasons: list[str] = []
        if platform:
            try:
                return platform, self._run(platform, store)
            except NotThisPlatform as e:
                reasons.append(f"{platform}: {e}")      # store changed platform: re-detect
            except FetchError as e:
                raise FetchError(f"{store}: {e}") from e
        for name in DETECT_ORDER:
            if name == platform:
                continue
            try:
                return name, self._run(name, store)
            except NotThisPlatform as e:
                reasons.append(f"{name}: {e}")
            except FetchError as e:
                reasons.append(f"{name}: {e}")
                if not isinstance(e, NotThisPlatform) and name != "structured":
                    # Real platform found but failing (e.g. 403) — don't mask it.
                    raise FetchError(f"{store}: {e}") from e
        raise FetchError(f"{store}: каталог не найден ни одним способом ({'; '.join(reasons)})")

    def fetch_catalog(self, store_url: str) -> list[Variant]:
        return self.fetch(store_url)[1]


# Backwards-compatible name used by v0.1 code and tests.
ShopifyFetcher = CatalogFetcher
