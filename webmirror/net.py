"""How the static crawl talks to websites: browser-like requests, ad blocking,
robots.txt and polite pacing.

Every feature here is optional and off unless the engine turns it on, so the
default crawl behaves exactly as before.
"""
from __future__ import annotations

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Callable
from urllib import robotparser
from urllib.parse import urlsplit, urlunsplit

import requests

from .ad_domains import AD_DOMAINS

USER_AGENT = "WebMirror/0.1 (local static website archiver)"
# The browser the "look like Chrome" option copies, down to its TLS handshake.
IMPERSONATE = "chrome"

try:  # Optional: pip install "webmirror[browserlike]"
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import RequestException as _CurlRequestException
except ImportError:  # pragma: no cover - depends on the installed extras
    curl_requests = None
    _CurlRequestException = None

# Catch these around any request, whichever session made it.
FETCH_ERRORS: tuple[type[BaseException], ...] = (requests.RequestException,) + (
    (_CurlRequestException,) if _CurlRequestException else ()
)


def browserlike_available() -> bool:
    return curl_requests is not None


class _ClosingResponse:
    """Give a curl_cffi response the `with` support and attributes requests has."""

    def __init__(self, response):
        self._response = response

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def iter_content(self, chunk_size=None, **kwargs):
        # curl_cffi picks its own chunk sizes and warns when given one.
        return self._response.iter_content(**kwargs)

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:
            pass


class BrowserLikeSession:
    """A session whose requests look like Chrome's, TLS handshake and headers alike.

    Many sites (and CDNs in front of them) refuse Python's own TLS fingerprint
    with a 403 before any page is served; these requests get the page instead.
    """

    def __init__(self, impersonate: str = IMPERSONATE):
        if curl_requests is None:
            raise RuntimeError(
                "Browser-like requests need curl_cffi. Install with pip install 'webmirror[browserlike]'."
            )
        self._session = curl_requests.Session(impersonate=impersonate)
        self.headers = self._session.headers

    def get(self, url: str, **kwargs):
        if kwargs.get("stream"):
            return _ClosingResponse(self._session.get(url, **kwargs))
        return self._session.get(url, **kwargs)

    def close(self) -> None:
        self._session.close()


def make_session(browserlike: bool, progress: Callable[[str], None] | None = None):
    """A requests-compatible session: Chrome-like when asked and possible."""
    if browserlike:
        if browserlike_available():
            return BrowserLikeSession()
        if progress:
            progress("Browser-like requests need curl_cffi (pip install 'webmirror[browserlike]'); "
                     "using plain requests.")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def host_is_blocked(url: str, blocked: frozenset[str] | set[str] = AD_DOMAINS) -> bool:
    """True for an ad or tracker host, or any subdomain of one."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    while host:
        if host in blocked:
            return True
        _, dot, host = host.partition(".")
        if not dot:
            break
    return False


def retry_after_seconds(value: str | None) -> float | None:
    """A Retry-After header as seconds: either a number or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class AutoThrottle:
    """Pace requests to each website by how fast it answers.

    The wait before the next request to a host moves toward how long that host
    took to answer the last one, so a slow, busy server is asked less often and
    a fast one barely waits. When a site says it is overloaded (429 or 503) the
    wait doubles, or becomes what its Retry-After asks if that is longer; normal
    answers bring it back down. A robots.txt Crawl-delay is the floor.
    """

    RETRY_STATUSES = (429, 503)

    def __init__(self, start_delay: float = 0.25, min_delay: float = 0.0,
                 max_delay: float = 60.0, max_retries: int = 3,
                 clock: Callable[[], float] = monotonic,
                 sleeper: Callable[[float], None] = sleep):
        self.start_delay = start_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.clock = clock
        self.sleeper = sleeper
        self.delays: dict[str, float] = {}
        self.floors: dict[str, float] = {}
        self.last_request: dict[str, float] = {}

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).netloc or "").lower()

    def delay(self, url: str) -> float:
        host = self._host(url)
        return max(self.delays.get(host, self.start_delay), self.floors.get(host, 0.0))

    def set_floor(self, url: str, seconds: float) -> None:
        self.floors[self._host(url)] = min(self.max_delay, max(0.0, seconds))

    def wait(self, url: str) -> None:
        host = self._host(url)
        last = self.last_request.get(host)
        if last is not None:
            remaining = self.delay(url) - (self.clock() - last)
            if remaining > 0:
                self.sleeper(remaining)
        self.last_request[host] = self.clock()

    def record(self, url: str, latency: float, status: int | None = None,
               retry_after: float | None = None) -> None:
        host = self._host(url)
        current = self.delays.get(host, self.start_delay)
        if status in self.RETRY_STATUSES:
            new = max(current * 2, self.start_delay, retry_after or 0.0)
        else:
            new = (current + max(0.0, latency)) / 2
        self.delays[host] = min(self.max_delay, max(self.min_delay, new))


class RobotsRules:
    """Reads each site's robots.txt once and answers whether WebMirror may fetch a URL."""

    def __init__(self, session, user_agent: str = "WebMirror"):
        self.session = session
        self.user_agent = user_agent
        self.parsers: dict[str, robotparser.RobotFileParser | None] = {}

    def _parser(self, url: str) -> robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        key = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if key not in self.parsers:
            robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
            parser = robotparser.RobotFileParser(robots_url)
            try:
                response = self.session.get(robots_url, timeout=(5, 15))
                status = getattr(response, "status_code", 200)
                if status in (401, 403):
                    # As in urllib: a robots.txt behind a login means "stay out".
                    parser.disallow_all = True
                elif status >= 400:
                    parser.allow_all = True
                else:
                    parser.parse(response.text.splitlines())
            except FETCH_ERRORS:
                parser = None  # Unreachable robots.txt: don't block the crawl.
            self.parsers[key] = parser
        return self.parsers[key]

    def allowed(self, url: str) -> bool:
        parser = self._parser(url)
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        parser = self._parser(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        rate = parser.request_rate(self.user_agent)
        seconds = float(delay) if delay is not None else None
        if rate and rate.requests:
            per_request = rate.seconds / rate.requests
            seconds = max(seconds or 0.0, per_request)
        return seconds
