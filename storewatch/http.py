"""Polite HTTP client shared by all platform adapters.

* honours robots.txt (stdlib ``urllib.robotparser``);
* identifies itself with an honest User-Agent;
* waits between requests to the same store;
* retries transient errors (429, 5xx, network) with exponential backoff.

The transport (``http_get``) is injectable so tests run without network.
"""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable

USER_AGENT = "ARVEN-StoreWatch/0.2 (+competitor price monitoring; contact: hello@arvend.io)"


@dataclass
class HttpResponse:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.headers.items():
            if k.lower() == name:
                return v
        return None


HttpGet = Callable[[str, dict[str, str], float], HttpResponse]


def urllib_get(url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
    """Default HTTP GET using only the standard library."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(20 * 1024 * 1024)
            if raw[:2] == b"\x1f\x8b":                  # gzipped file, e.g. sitemap.xml.gz
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError):
                    pass
            charset = resp.headers.get_content_charset() or "utf-8"
            return HttpResponse(resp.status, raw.decode(charset, errors="replace"), dict(resp.headers))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return HttpResponse(e.code, body, dict(e.headers or {}))


class FetchError(Exception):
    """A store could not be read."""


class NotThisPlatform(FetchError):
    """The store does not look like the platform an adapter handles."""


class HttpClient:
    def __init__(
        self,
        http_get: HttpGet = urllib_get,
        delay: float = 1.0,
        timeout: float = 20.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http_get = http_get
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.notes: dict[str, object] = {}       # per-fetch remarks from adapters, e.g. truncated
        self.requests = 0                        # HTTP requests made (for usage control)
        self._host_delay: dict[str, float] = {}  # Crawl-delay from robots.txt, per host
        self._last: dict[str, float] = {}

    def _headers(self, accept: str) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": accept}

    MAX_CRAWL_DELAY = 10.0      # a site asking for more is read at this pace, never faster than asked below it

    def _throttle(self, url: str) -> None:
        host = url.split("/")[2] if "://" in url else url
        last = self._last.get(host)
        delay = max(self.delay, self._host_delay.get(host, 0.0))
        if last is not None and delay > 0:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last[host] = time.monotonic()

    def get(self, url: str, accept: str = "application/json") -> HttpResponse:
        attempt = 0
        while True:
            self._throttle(url)
            self.requests += 1
            try:
                resp = self.http_get(url, self._headers(accept), self.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt >= self.retries:
                    raise FetchError(f"network error for {url}: {e}") from e
            else:
                if resp.status != 429 and resp.status < 500:
                    return resp
                if attempt >= self.retries:
                    return resp
                ra = resp.header("Retry-After")
                if ra and ra.isdigit():
                    self.sleep(min(int(ra), 60))
                    attempt += 1
                    continue
            self.sleep(2 ** attempt)
            attempt += 1

    def _robots_for(self, store: str) -> urllib.robotparser.RobotFileParser | None:
        if store not in self._robots:
            rp = None
            try:
                resp = self.get(store + "/robots.txt", accept="text/plain")
                if resp.status == 200:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(resp.body.splitlines())
                    wanted = 0.0
                    try:
                        cd = rp.crawl_delay(USER_AGENT)
                        if cd:
                            wanted = float(cd)
                        rr = rp.request_rate(USER_AGENT)
                        if rr and rr.requests:
                            wanted = max(wanted, rr.seconds / rr.requests)
                    except (TypeError, ValueError, AttributeError):
                        pass
                    if wanted > 0:
                        host = store.split("/")[2]
                        self._host_delay[host] = min(wanted, self.MAX_CRAWL_DELAY)
            except FetchError:
                rp = None
            self._robots[store] = rp
        return self._robots[store]

    def allowed(self, store: str, path_or_url: str) -> bool:
        """robots.txt check. Missing or unreadable robots.txt means allowed."""
        rp = self._robots_for(store)
        url = path_or_url if "://" in path_or_url else store + path_or_url
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def sitemaps(self, store: str) -> list[str]:
        rp = self._robots_for(store)
        return list(rp.site_maps() or []) if rp is not None else []
