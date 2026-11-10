"""Downloader, on-disk library, and loopback-only HTTP server for WebMirror.

Intentionally a best-effort *static* mirror. JavaScript-generated requests and
services that need an online backend cannot be inferred by a static crawler.
"""
from __future__ import annotations

import codecs
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tempfile
from threading import Event, Thread
from time import monotonic
from typing import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .ad_domains import AD_DOMAINS
from .net import (
    FETCH_ERRORS, USER_AGENT, AutoThrottle, RobotsRules, host_is_blocked,
    make_session, retry_after_seconds,
)


MAX_FILES = 5000
MAX_BYTES = 30 * 1024 * 1024
# Pages the browser opens in one mirror: the entry page and the linked pages under
# its path. Each one runs, so tools that load an engine only when opened get it.
MAX_PAGES = 40
# HTML pages the static crawl saves in one mirror. Pages are fetched one after
# another from a queue (not by recursion, which a long chain of "related" links
# would overflow) and don't count toward MAX_FILES, so a large asset count can't
# leave linked pages behind.
MAX_CRAWL_PAGES = 1000
# A link to these opens a page; other extensions are files the static crawl fetches.
PAGE_EXTENSIONS = ("", ".html", ".htm", ".php", ".asp", ".aspx")
# Windows disallows names beyond 255 characters and often rejects complete
# paths beyond 260 characters. Leave room for the user's mirror-directory path.
MAX_LOCAL_COMPONENT = 96
MAX_LOCAL_RELATIVE_PATH = 160
# A paused mirror keeps its files and this record of where the crawl stopped.
STATE_FILE = ".webmirror-state.json"
# ETag and Last-Modified of each saved binary file, so the next capture of the
# site can ask the server whether it changed instead of downloading it again.
VALIDATORS_FILE = ".webmirror-validators.json"
TEXT_KINDS = ("html", "css", "js", "manifest")


def parse_filters(text: str | list[str] | None) -> list[tuple[bool, re.Pattern]]:
    """HTTrack-style rules: "+*.glb -*/forum/*". `*` matches anything; the last
    rule that matches a URL decides, "+" to include it, "-" to leave it out."""
    if not text:
        return []
    words = text.split() if isinstance(text, str) else [w for item in text for w in item.split()]
    rules = []
    for word in words:
        if word[0] not in "+-" or len(word) < 2:
            raise ValueError(f'Filter "{word}" must start with + or -, like +*.glb or -*/forum/*')
        pattern = "^" + ".*".join(re.escape(part) for part in word[1:].split("*")) + "$"
        rules.append((word[0] == "+", re.compile(pattern, re.I)))
    return rules

CSS_URL = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<url>[^'")]+)(?P=q)\s*\)""", re.I)
CSS_IMPORT = re.compile(r"""(@import\s+)(?P<q>['"])(?P<url>[^'"]+)(?P=q)""", re.I)
JS_LITERAL = re.compile(
    r"""(?P<prefix>\b(?:import|export)\s+(?:[^\n;]{0,200}?\s+from\s*)?|\bimport\s*\(\s*|\bnew\s+URL\s*\(\s*|\bfetch\s*\(\s*|\bnew\s+(?:Worker|SharedWorker)\s*\(\s*)(?P<q>['"])(?P<url>[^'"]+)(?P=q)""",
    re.M,
)
# A webpack runtime names every chunk it can load later, either as whole paths
# ("static/chunks/7701.6846ef15632e87c4.js") or as a folder, a name and a map of
# chunk id to content hash ("static/chunks/" + id + "." + {7701: "6846..."}[id] + ".js").
WEBPACK_PUBLIC_PATH = re.compile(r"""\b\w+\.p\s*=\s*(['"])(?P<path>[^'"]*)\1""")
WEBPACK_CHUNK_PATH = re.compile(r"""(['"])(?P<path>static/(?:chunks|css)/[^'"+]+\.(?:js|css))\1""")
WEBPACK_CHUNK_MAP = re.compile(
    r"""(['"])(?P<folder>static/(?:chunks|css)/)\1\s*\+\s*(?P<name>\([^;]*?\)|\w+)\s*\+\s*"""
    r"""(['"])(?P<sep>[.-])\4\s*\+\s*\(?\s*(?P<map>\{[^{}]*\})\s*\)?\s*\[\w+\]\s*\+\s*"""
    r"""(['"])(?P<ext>\.(?:js|css))\7"""
)
SKIP_PREFIXES = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#", "//#")
MIME_EXTENSIONS = {
    "text/html": ".html", "text/css": ".css", "text/javascript": ".js",
    "application/javascript": ".js", "application/json": ".json",
    "application/wasm": ".wasm", "application/manifest+json": ".webmanifest",
    "image/svg+xml": ".svg", "image/png": ".png", "image/jpeg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif", "font/woff2": ".woff2",
}

# The charset a page names in <meta charset> or <meta http-equiv="Content-Type">.
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*(?P<name>[\w.:-]+)""", re.I)
HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?(?P<name>[\w.:-]+)""", re.I)


def text_encoding(content_type: str, data: bytes, kind: str) -> str:
    """The encoding to read a downloaded HTML, CSS or JS file with.

    Many servers send "text/html" with no charset, and requests then assumes
    ISO-8859-1, which garbles every non-ASCII character of a UTF-8 page ("›" turns
    into "â€º"). A browser reads the page's own <meta charset> instead and
    otherwise UTF-8 for modern files, so do the same.
    """
    candidates = []
    match = HEADER_CHARSET.search(content_type or "")
    if match:
        candidates.append(match.group("name"))
    if data.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    if kind == "html":
        match = META_CHARSET.search(bytes(data[:4096]))
        # As in a browser, a page that names UTF-16 in its own ASCII text is UTF-8.
        if match and not match.group("name").lower().startswith((b"utf-16", b"utf-32")):
            candidates.append(match.group("name").decode("ascii", "replace"))
    for name in candidates + ["utf-8"]:
        try:
            codecs.lookup(name)
        except LookupError:
            continue
        return name
    return "utf-8"


def declare_utf8(html: str) -> str:
    """Saved pages are always written as UTF-8, so their <meta> must say so."""
    return re.sub(r"""(<meta[^>]+charset\s*=\s*["']?\s*)[\w.:-]+""", r"\1utf-8", html, count=1, flags=re.I)


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Enter a website URL.")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Only valid HTTP and HTTPS website URLs are supported.")
    if parts.username or parts.password:
        raise ValueError("URLs containing passwords are not supported.")
    path = parts.path or "/"
    # Keep user-supplied path, because GitHub Pages commonly lives in a subpath.
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _slug(url: str) -> str:
    parsed = urlsplit(url)
    domain = re.sub(r"[^a-z0-9-]+", "-", (parsed.hostname or "site").lower()).strip("-")[:48]
    return f"{domain}-{sha256(url.encode()).hexdigest()[:12]}"


class MirrorPaused(Exception):
    """The user paused a mirror; its progress is kept so it can be resumed."""


class SkippedResource(ValueError):
    """A URL WebMirror chose not to fetch (robots.txt said no)."""


def default_home() -> Path:
    return Path(os.environ.get("WEBMIRROR_HOME", "~/.webmirror")).expanduser()


@dataclass(frozen=True)
class MirrorRecord:
    key: str
    url: str
    entry: str
    saved_at: str
    files: int
    pages: int
    failures: int

    @classmethod
    def from_json(cls, data: dict) -> "MirrorRecord":
        return cls(**{field: data[field] for field in cls.__dataclass_fields__})

    @property
    def title(self) -> str:
        return f"{urlsplit(self.url).netloc}{urlsplit(self.url).path}"


class MirrorLibrary:
    def __init__(self, home: Path | str | None = None):
        self.home = Path(home).expanduser() if home is not None else default_home()
        self.directory = self.home / "mirrors"
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, record: MirrorRecord) -> Path:
        # Never take arbitrary filesystem paths from persisted JSON.
        if not re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{12}", record.key):
            raise ValueError("Invalid mirror key")
        return self.directory / record.key

    def list(self) -> list[MirrorRecord]:
        mirrors = []
        for manifest in self.directory.glob("*/manifest.json"):
            try:
                record = MirrorRecord.from_json(json.loads(manifest.read_text("utf-8")))
                if self.path(record) == manifest.parent and (manifest.parent / record.entry.lstrip("/")).is_file():
                    mirrors.append(record)
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return sorted(mirrors, key=lambda item: item.saved_at, reverse=True)

    def delete(self, record: MirrorRecord) -> None:
        folder = self.path(record)
        if folder.is_dir():
            shutil.rmtree(folder)

    def paused_path(self, url: str) -> Path:
        """Where a paused mirror of `url` keeps its files until it is resumed."""
        return self.directory / f".paused-{_slug(normalize_url(url))}"

    def is_paused(self, url: str) -> bool:
        return (self.paused_path(url) / STATE_FILE).is_file()

    def discard_paused(self, url: str) -> None:
        folder = self.paused_path(url)
        if folder.is_dir():
            shutil.rmtree(folder)


class MirrorEngine:
    """Download linked resources into a folder and rewrite URLs for local hosting."""

    def __init__(self, library: MirrorLibrary, progress: Callable[[str], None] | None = None,
                 max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES,
                 include_pages_files: bool = False, browser_capture: bool = False,
                 manual_verification: bool = False,
                 on_verification_required: Callable[[str], None] | None = None,
                 max_pages: int = MAX_PAGES, max_crawl_pages: int = MAX_CRAWL_PAGES,
                 browserlike: bool = False, block_ads: bool = False,
                 polite: bool = False, obey_robots: bool = False,
                 auto_solve_challenges: bool = False,
                 pause_event: Event | None = None,
                 throttle: AutoThrottle | None = None,
                 update_unchanged: bool = False,
                 filters: str | list[str] | None = None):
        """Options beyond the original static crawl, all off by default:

        browserlike: send requests that look like Chrome's (needs curl_cffi).
        block_ads: skip ad and tracker hosts, in the crawl and in the browser.
        polite: pace requests by how fast each site answers, and back off and
            retry when it answers 429 or 503 (see net.AutoThrottle).
        obey_robots: don't fetch what robots.txt disallows; honour Crawl-delay.
        auto_solve_challenges: let Scrapling's stealth browser solve Cloudflare
            checks during browser capture (needs the "stealth" extra).
        pause_event: set it to pause after the current page; resume with
            mirror(url, resume=True).
        update_unchanged: when the site was mirrored before, ask the server
            whether each saved image, font, model or other binary file changed
            (If-None-Match / If-Modified-Since) and reuse the saved copy if not.
        filters: HTTrack-style rules such as "+*.glb -*/forum/*" (see
            parse_filters). "-" leaves matching URLs out; "+" also lets the crawl
            follow same-site pages outside the starting folder.
        """
        self.library = library
        self.browserlike = browserlike
        self.block_ads = block_ads
        self.polite = polite
        self.obey_robots = obey_robots
        self.auto_solve_challenges = auto_solve_challenges
        self.pause_event = pause_event
        self.throttle = throttle
        self.update_unchanged = update_unchanged
        self.filters = parse_filters(filters)
        self.max_crawl_pages = max_crawl_pages
        self.max_pages = max_pages
        self.progress = progress or (lambda message: None)
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.include_pages_files = include_pages_files
        self.browser_capture = browser_capture
        self.manual_verification = manual_verification
        self.on_verification_required = on_verification_required
        self.import_map: dict[str, str] = {}

    def mirror(self, supplied_url: str, resume: bool = False) -> MirrorRecord:
        """Mirror a website. With `resume`, continue a paused mirror of it if there is one."""
        url = normalize_url(supplied_url)
        parsed = urlsplit(url)
        self.origin = (parsed.scheme.lower(), parsed.netloc.lower())
        path = parsed.path or "/"
        # "/designer" is a folder on most sites (it redirects to "/designer/"), so
        # scope the crawl to it rather than to the whole site; "/app/page.html"
        # keeps its folder "/app/".
        if path.endswith("/"):
            self.scope = path
        elif PurePosixPath(path).suffix:
            self.scope = posixpath.dirname(path) + "/"
        else:
            self.scope = path + "/"
        self.entry_path = path
        self.visited: dict[str, str | None] = {}
        self._browser_assets: dict[str, tuple[str, bytes]] = {}
        self.in_progress: set[str] = set()
        self.failures: list[str] = []
        self.pages = 0
        self.html_paths: set[str] = set()
        # In-scope page links the static crawl could not fetch: the browser tries
        # them, and any still missing get a page that sends visitors to the live site.
        self.unfetched_pages: dict[str, str] = {}
        # Linked pages waiting to be saved, and the local path each link already uses.
        self.page_queue: deque[str] = deque()
        self.queued_pages: dict[str, str] = {}
        # Bare module names from the pages' import maps, and the URL each maps to.
        self.import_map: dict[str, str] = {}
        self.extra_files = 0
        self.blocked = 0
        self.filtered = 0
        self.reused = 0
        self.validators: dict[str, dict] = {}
        self.browser_done = False
        entry: str | None = None
        key = _slug(url)
        dest = self.library.directory / key
        backup = self.library.directory / f".{key}-backup"
        paused = self.library.paused_path(url)
        self.previous_dir = dest
        self.previous: dict[str, dict] = {}
        if self.update_unchanged:
            try:
                self.previous = json.loads((dest / VALIDATORS_FILE).read_text("utf-8"))
            except (OSError, ValueError):
                self.previous = {}
        resuming = resume and (paused / STATE_FILE).is_file()
        if resuming:
            self.root = paused
            entry = self._load_state()
            self.progress(f"Resuming the paused mirror ({len(self.page_queue)} pages left in the queue).")
        else:
            self.root = Path(tempfile.mkdtemp(prefix=".webmirror-", dir=self.library.directory))
        self.session = make_session(self.browserlike, self.progress)
        self.robots = RobotsRules(self.session) if self.obey_robots else None
        if self.throttle is None and (self.polite or self.obey_robots):
            # Without "polite", only a robots.txt Crawl-delay paces the crawl.
            self.throttle = AutoThrottle() if self.polite else AutoThrottle(start_delay=0.0)
        try:
            if not resuming:
                entry = self._download(url, hint="html")
            self._crawl_pages()
            if self.browser_capture:
                if not self.browser_done:
                    self._check_pause()
                    self._capture_browser(url)
                    self.browser_done = True
                # Pages the browser's copies link to that the crawl hasn't seen yet.
                self._crawl_pages()
                entry = self._visited_variant(url) or entry
            if not entry:
                detail = self.failures[0] if self.failures else "Unknown error"
                raise RuntimeError(
                    "The website entry page could not be downloaded. " + detail[:350]
                    + ". Sites requiring login or bot challenges cannot be bypassed."
                )
            if self.include_pages_files:
                self._download_github_pages_assets(url)
            self._write_live_links()
            self._write_routes()
            (self.root / VALIDATORS_FILE).write_text(json.dumps(self.validators), encoding="utf-8")
            (self.root / STATE_FILE).unlink(missing_ok=True)
            record = MirrorRecord(
                key=key, url=url, entry=entry,
                saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                files=len({item for item in self.visited.values() if item}) + self.extra_files,
                pages=self.pages, failures=len(self.failures),
            )
            (self.root / "manifest.json").write_text(
                json.dumps(record.__dict__, indent=2) + "\n", encoding="utf-8"
            )
            # Only replace an existing snapshot after a complete entry-page download.
            if backup.exists():
                shutil.rmtree(backup)
            if dest.exists():
                dest.rename(backup)
            try:
                self.root.rename(dest)
            except Exception:
                if backup.exists() and not dest.exists():
                    backup.rename(dest)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            # A fresh mirror supersedes any paused one of the same site.
            if paused.exists():
                shutil.rmtree(paused, ignore_errors=True)
            blocked = f", {self.blocked} ad/tracker requests blocked" if self.blocked else ""
            if self.filtered:
                blocked += f", {self.filtered} left out by filters"
            if self.reused:
                blocked += f", {self.reused} unchanged files reused"
            self.progress(f"Saved {record.files} resources ({record.pages} HTML pages, "
                          f"{record.failures} skipped/failed{blocked}).")
            return record
        except MirrorPaused:
            self._save_state(url, entry)
            if self.root != paused:
                if paused.exists():
                    shutil.rmtree(paused)
                self.root.rename(paused)
                self.root = paused
            self.progress(f"Paused with {self.pages} pages saved and {len(self.page_queue)} "
                          "waiting. Mirror the same address again to resume.")
            raise
        finally:
            self.session.close()
            # Keep a paused mirror's files (and one being resumed, if it fails).
            if self.root.exists() and self.root != paused:
                shutil.rmtree(self.root, ignore_errors=True)

    def _check_pause(self) -> None:
        if self.pause_event is not None and self.pause_event.is_set():
            raise MirrorPaused("Mirror paused.")

    def _save_state(self, url: str, entry: str | None) -> None:
        state = {
            "url": url, "entry": entry, "browser_done": self.browser_done,
            "visited": self.visited, "failures": self.failures, "pages": self.pages,
            "html_paths": sorted(self.html_paths),
            "unfetched_pages": self.unfetched_pages,
            "page_queue": list(self.page_queue), "queued_pages": self.queued_pages,
            "import_map": self.import_map, "extra_files": self.extra_files,
            "blocked": self.blocked, "filtered": self.filtered, "reused": self.reused,
            "validators": self.validators,
        }
        (self.root / STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    def _load_state(self) -> str | None:
        state = json.loads((self.root / STATE_FILE).read_text("utf-8"))
        self.visited = dict(state["visited"])
        self.failures = list(state["failures"])
        self.pages = int(state["pages"])
        self.html_paths = set(state["html_paths"])
        self.unfetched_pages = dict(state["unfetched_pages"])
        self.page_queue = deque(state["page_queue"])
        self.queued_pages = dict(state["queued_pages"])
        self.import_map = dict(state["import_map"])
        self.extra_files = int(state["extra_files"])
        self.blocked = int(state.get("blocked", 0))
        self.filtered = int(state.get("filtered", 0))
        self.reused = int(state.get("reused", 0))
        self.validators = dict(state.get("validators", {}))
        self.browser_done = bool(state["browser_done"])
        return state["entry"]

    def _crawl_pages(self) -> None:
        """Save the queued page links, breadth first, until none are left."""
        while self.page_queue:
            self._check_pause()
            target = self.page_queue.popleft()
            reserved = self.queued_pages[target]
            saved = self._download(target, hint="html")
            if not saved:
                # Opened by the browser capture, or else a page pointing at the live one.
                self.unfetched_pages[target] = reserved
            elif saved != reserved:
                # Saved under another name (the browser saw a different type):
                # the links already written point here, so forward them.
                output = self.root / reserved.lstrip("/")
                if not output.exists():
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(
                        "<!doctype html><meta charset=\"utf-8\">"
                        f"<meta http-equiv=\"refresh\" content=\"0;url={saved}\">\n",
                        encoding="utf-8",
                    )

    def _capture_browser(self, url: str) -> None:
        """Copy additional GET resources requested during a real page load."""
        try:
            from .browser import BrowserUnavailable, capture_public_assets
            self.progress("Opening a clean Chromium browser to discover dynamic resources...")
            assets = capture_public_assets(
                url, max_files=self.max_files, max_bytes=self.max_bytes,
                progress=self.progress,
                allow_manual_verification=self.manual_verification,
                on_verification_required=self.on_verification_required,
                pages=self._known_pages(url), follow=self._is_page_link,
                max_pages=self.max_pages,
                blocked_domains=AD_DOMAINS if self.block_ads else None,
                auto_solve=self.auto_solve_challenges,
            )
        except (BrowserUnavailable, OSError, RuntimeError) as exc:
            self.failures.append("Browser capture unavailable: " + str(exc)[:300])
            self.progress("Browser capture unavailable: " + str(exc)[:300])
            return
        # A successful browser verification may make resources available that
        # Requests could not fetch. Process them from the browser's actual
        # responses, without replaying challenge cookies into Requests.
        self._browser_assets = {
            target: (mime, data) for target, mime, data in assets
        }
        for target in self._browser_assets:
            if self.visited.get(target) is None:
                self.visited.pop(target, None)
        added = 0
        for target, mime, data in assets:
            if target not in self.visited and self._store_browser_asset(target, mime, data):
                added += 1
        self._browser_assets.clear()
        self.progress(f"Captured {added} additional dynamic resources.")

    def _visited_variant(self, url: str) -> str | None:
        """The saved copy of `url`, or of its trailing-slash twin it redirected to."""
        parts = urlsplit(url)
        path = parts.path or "/"
        twin = path[:-1] if path.endswith("/") and path != "/" else path + "/"
        return (self.visited.get(url)
                or self.visited.get(urlunsplit((parts.scheme, parts.netloc, twin, parts.query, ""))))

    def _known_pages(self, entry: str) -> list[str]:
        """In-scope pages found so far, for the browser to open after the entry."""
        found = [target for target, local in self.visited.items()
                 if local in self.html_paths and target != entry]
        return found + [target for target in self.unfetched_pages if target not in found]

    def _filter_verdict(self, target: str) -> bool | None:
        """True if a "+" rule is the last to match, False for "-", None if none match."""
        verdict = None
        for include, pattern in getattr(self, "filters", ()):
            if pattern.match(target):
                verdict = include
        return verdict

    def _in_crawl_scope(self, target: str) -> bool:
        """Under the starting folder, or a same-site page a "+" filter asks for."""
        if self._within_scope(target):
            return True
        parts = urlsplit(target)
        return ((parts.scheme.lower(), parts.netloc.lower()) == self.origin
                and self._filter_verdict(target) is True)

    def _page_candidate(self, target: str) -> bool:
        """A page the crawl would save, before robots.txt and "-" filters have a say."""
        return (self._in_crawl_scope(target)
                and PurePosixPath(urlsplit(target).path).suffix.lower() in PAGE_EXTENSIONS)

    def _is_page_link(self, target: str) -> bool:
        # A page robots.txt or a "-" filter keeps out stays a link to the live site.
        return (self._page_candidate(target) and self._filter_verdict(target) is not False
                and self._robots_allow(target))

    def _robots_allow(self, target: str) -> bool:
        robots = getattr(self, "robots", None)
        if robots is None:
            return True
        allowed = robots.allowed(target)
        delay = robots.crawl_delay(target)
        if delay and self.throttle is not None and self.throttle.floors.get(
                urlsplit(target).netloc.lower()) is None:
            self.throttle.set_floor(target, delay)
            self.progress(f"robots.txt asks for {delay:g}s between requests to {urlsplit(target).netloc}.")
        return allowed

    def _fetch(self, target: str, headers: dict | None = None):
        """GET `target` as a stream, paced, and retried when the site says it is busy."""
        if not self._robots_allow(target):
            raise SkippedResource("disallowed by robots.txt")
        retries = 0
        while True:
            if self.throttle is not None:
                self.throttle.wait(target)
            started = monotonic()
            extra = {"headers": headers} if headers else {}
            response = self.session.get(target, timeout=(5, 20), stream=True, **extra)
            if not self.polite or self.throttle is None:
                return response
            status = getattr(response, "status_code", None)
            answer = getattr(response, "headers", None) or {}
            retry_after = (retry_after_seconds(answer.get("Retry-After"))
                           if status in AutoThrottle.RETRY_STATUSES else None)
            self.throttle.record(target, monotonic() - started, status, retry_after)
            if status not in AutoThrottle.RETRY_STATUSES or retries >= self.throttle.max_retries:
                return response
            retries += 1
            close = getattr(response, "close", None)
            if close:
                close()
            self.progress(f"{urlsplit(target).netloc} answered {status}; slowing down to "
                          f"{self.throttle.delay(target):.1f}s between requests and retrying.")

    def _write_live_links(self) -> None:
        """Give each page link that never downloaded a page pointing at the live one.

        Its link was already rewritten to the local path, so without this it would
        be a dead end offline; with it, it opens the site when there is a connection.
        """
        for target, local in self.unfetched_pages.items():
            if self.visited.get(target):
                continue
            output = self.root / local.lstrip("/")
            if output.exists():
                continue
            escaped = target.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "<!doctype html><meta charset=\"utf-8\">"
                f"<meta http-equiv=\"refresh\" content=\"0;url={escaped}\">"
                f"<title>Not saved offline</title><p>This page was not saved offline. "
                f"<a href=\"{escaped}\">Open it online</a>.</p>\n",
                encoding="utf-8",
            )

    def _store_browser_asset(self, target: str, mime: str, data: bytes) -> bool:
        """Store a browser-observed resource, without refetching or losing its URL."""
        parts = urlsplit(target)
        if (parts.scheme.lower(), parts.netloc.lower()) != self.origin:
            return False
        if len(data) > self.max_bytes or len(self.visited) >= self.max_files:
            return False
        if mime in ("text/html", "application/xhtml+xml"):
            kind = "html"
        elif mime == "text/css":
            kind = "css"
        elif mime in ("text/javascript", "application/javascript", "application/x-javascript"):
            kind = "js"
        elif mime == "application/manifest+json":
            kind = "manifest"
        else:
            kind = mime
        relative = self._destination(target, kind)
        local = "/" + relative.as_posix()
        output = self.root / relative
        if output.is_file():
            self.visited[target] = local
            return False
        self.visited[target] = local
        if kind in ("html", "css", "js", "manifest"):
            text = data.decode(text_encoding("", data, kind), errors="replace")
            if kind == "html":
                if (re.search(r"<title>\s*Just a moment[.\s]*</title>", text, re.I)
                        or "cf-chl-opt" in text):
                    self.visited.pop(target, None)
                    return False
                if local not in self.html_paths:
                    self.html_paths.add(local)
                    self.pages += 1
                text = self._rewrite_html(text, target)
            elif kind == "css":
                text = self._rewrite_css(text, target)
            elif kind == "js":
                text = self._rewrite_js(text, target)
            elif kind == "manifest":
                text = self._rewrite_manifest(text, target)
            data = text.encode("utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        return True

    def _write_routes(self) -> None:
        """Map original path+query URLs to their archived filenames.

        SPAs request routes like /app, not our implementation detail
        /app/index.html; generated URLs may also contain query strings.
        """
        routes: dict[str, str] = {}
        for original, local in self.visited.items():
            parts = urlsplit(original)
            if local and (parts.scheme.lower(), parts.netloc.lower()) == self.origin:
                key = parts.path or "/"
                if parts.query:
                    key += "?" + parts.query
                routes[key] = local
        (self.root / ".webmirror-routes.json").write_text(
            json.dumps(routes, indent=2), encoding="utf-8"
        )

    def _download_github_pages_assets(self, url: str) -> None:
        """Optionally include unreferenced published files from a public GitHub Pages repo.

        Useful for sites with computed tile names (for instance Terrain Press).
        This is opt-in because repositories can contain hundreds of megabytes of data.
        """
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if not hostname.endswith(".github.io"):
            self.progress("Full-site asset mode only applies to github.io sites.")
            return
        owner = hostname.removesuffix(".github.io")
        components = [part for part in parsed.path.split("/") if part]
        if components:
            repo = components[0]
            if len(components) > 1 and components[1] != "index.html":
                self.progress("Full-site asset mode requires a GitHub Pages project homepage.")
                return
        else:
            repo = owner + ".github.io"
        self.progress(f"Looking up published files in GitHub repo {owner}/{repo}...")
        api = f"https://api.github.com/repos/{owner}/{repo}"
        try:
            metadata = self.session.get(api, timeout=(5, 25),
                                        headers={"Accept": "application/vnd.github+json"})
            metadata.raise_for_status()
            branch = metadata.json()["default_branch"]
            tree_result = self.session.get(
                f"{api}/git/trees/{quote(branch, safe='')}?recursive=1",
                timeout=(5, 45), headers={"Accept": "application/vnd.github+json"}
            )
            tree_result.raise_for_status()
            tree_data = tree_result.json()
            tree = tree_data["tree"]
            paths = {item["path"] for item in tree if item.get("type") == "blob"}
            # Common GitHub Pages publication layouts. Source-build outputs may
            # not be committed to the repo, in which case regular crawling wins.
            prefix = next((candidate for candidate in ("site/", "docs/", "")
                           if candidate + "index.html" in paths), None)
            if prefix is None:
                self.progress("No published index.html found in the GitHub repo.")
                return
            files = [item for item in tree if item.get("type") == "blob"
                     and item["path"].startswith(prefix)]
            if tree_data.get("truncated"):
                self.progress("GitHub returned a truncated tree; some assets may be missing.")
            self.progress(f"Found {len(files)} published files in {owner}/{repo}/{prefix}.")
        except (*FETCH_ERRORS, KeyError, ValueError, TypeError) as exc:
            self.failures.append(f"GitHub Pages file listing unavailable: {exc}")
            self.progress(f"Could not list GitHub Pages files: {exc}")
            return

        for index, item in enumerate(files, start=1):
            if self.extra_files + len(self.visited) >= self.max_files:
                self.failures.append("GitHub Pages file limit reached")
                self.progress("File limit reached while copying GitHub Pages assets.")
                break
            relative = item["path"][len(prefix):]
            if not relative:
                continue
            source_url = urlunsplit((parsed.scheme, parsed.netloc,
                                   self.scope + relative, "", ""))
            kind = "html" if relative.endswith((".html", ".htm")) else ""
            dest = self.root / self._destination(source_url, kind)
            if dest.is_file():
                continue
            # Defensive check against unexpected upstream repository paths.
            if not dest.resolve().is_relative_to(self.root.resolve()):
                continue
            if item.get("size", 0) > self.max_bytes:
                self.failures.append(f"{relative}: exceeds per-file size limit")
                continue
            raw = (f"https://raw.githubusercontent.com/{owner}/{repo}/"
                   f"{quote(branch, safe='/')}/{quote(item['path'], safe='/')}")
            try:
                with self.session.get(raw, timeout=(5, 35), stream=True) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("resource exceeds per-file size limit")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                self.extra_files += 1
                if index % 10 == 0:
                    self.progress(f"GitHub Pages assets: {index}/{len(files)} checked...")
            except (*FETCH_ERRORS, ValueError, OSError) as exc:
                self.failures.append(f"{relative}: {exc}")
                self.progress(f"Skipped GitHub Pages file {relative}: {exc}")

    @staticmethod
    def _supported(url: str) -> bool:
        return urlsplit(url).scheme.lower() in ("http", "https")

    def _resolve(self, base: str, link: str | None) -> str | None:
        if not link or link.strip().lower().startswith(SKIP_PREFIXES):
            return None
        try:
            full = urljoin(base, link.strip())
            parts = urlsplit(full)
            if not self._supported(full) or not parts.hostname or parts.username or parts.password:
                return None
            return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        except ValueError:
            return None

    def _within_scope(self, target: str) -> bool:
        p = urlsplit(target)
        path = p.path or "/"
        return ((p.scheme.lower(), p.netloc.lower()) == self.origin
                and (path.startswith(self.scope) or path == getattr(self, "entry_path", None)))

    def _destination(self, target: str, kind: str) -> Path:
        parts = urlsplit(target)
        same_origin = (parts.scheme.lower(), parts.netloc.lower()) == self.origin
        # Keep same-origin absolute paths so runtime root-relative URLs still work.
        prefix = [] if same_origin else ["_external", re.sub(r"[^A-Za-z0-9._-]", "_", parts.netloc)]
        segments = []
        for segment in (parts.path or "/").split("/"):
            if segment:
                decoded = unquote(segment)
                # Guard against filesystem traversal and platform-specific separators.
                safe = re.sub(r"[^A-Za-z0-9._~-]", "_", decoded).strip(".")
                segments.append(safe or "_")
        ext = PurePosixPath(parts.path).suffix.lower()
        if parts.path.endswith("/") or not segments:
            segments.append("index.html" if kind == "html" else "index")
        elif not ext:
            if kind == "html":
                segments.append("index.html")
            else:
                segments[-1] += MIME_EXTENSIONS.get(kind, "") if kind.startswith("text/") else {
                    "css": ".css", "js": ".js", "manifest": ".webmanifest"
                }.get(kind, "")
        elif ext in (".php", ".asp", ".aspx") and kind == "html":
            # HTML content with a server extension stays on its original URL.
            pass
        if parts.query:
            stem, dot, suffix = segments[-1].rpartition(".")
            fingerprint = sha256(parts.query.encode()).hexdigest()[:10]
            segments[-1] = (f"{stem}-{fingerprint}.{suffix}" if dot
                            else f"{segments[-1]}-{fingerprint}")
        # Bound every component, including long opaque tokens generated by
        # ad/analytics and script endpoints. Keep extensions for correct MIME.
        bounded = [self._shorten_component(piece) for piece in prefix + segments]
        relative = Path(*bounded)
        if len(relative.as_posix()) > MAX_LOCAL_RELATIVE_PATH:
            # Deep directory trees also trip Windows MAX_PATH, even when no
            # individual filename is too long. Original URLs remain in the
            # route map and are served from this stable hashed location.
            extension = Path(bounded[-1]).suffix[:16]
            relative = Path("_webmirror_longpaths") / (
                sha256(target.encode("utf-8")).hexdigest()[:36] + extension
            )
        return relative

    @staticmethod
    def _shorten_component(name: str) -> str:
        """Keep local path components short, unique and Windows-compatible."""
        stem, ext = os.path.splitext(name)
        # Invalid or immense extensions should not crowd out the hash.
        ext = ext[:16] if re.fullmatch(r"\.[A-Za-z0-9]{1,15}", ext) else ""
        if len(name) > MAX_LOCAL_COMPONENT:
            token = sha256(name.encode("utf-8")).hexdigest()[:16]
            available = MAX_LOCAL_COMPONENT - len(ext) - len(token) - 1
            name = stem[:available] + "-" + token + ext
        # Reserved DOS device names are invalid on Windows in any directory.
        base = name.split(".", 1)[0].upper()
        if base in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", base):
            name = "_" + name
        return name

    def _download(self, target: str, hint: str = "") -> str | None:
        if target in self.visited:
            return self.visited[target]
        if target in self.in_progress:
            return None
        if target in getattr(self, "_browser_assets", {}):
            mime, data = self._browser_assets[target]
            self.in_progress.add(target)
            try:
                self._store_browser_asset(target, mime, data)
                return self.visited.get(target)
            finally:
                self.in_progress.discard(target)
        if self.block_ads and host_is_blocked(target):
            self.visited[target] = None
            self.blocked += 1
            return None
        if self._filter_verdict(target) is False:
            self.visited[target] = None
            self.filtered += 1
            return None
        if len(self.visited) >= self.max_files and target not in self.queued_pages:
            if "resource limit reached" not in self.failures:
                self.failures.append("resource limit reached")
                self.progress("Resource limit reached; skipping remaining assets.")
            return None
        self.in_progress.add(target)
        try:
            self.progress(f"Downloading {target}")
            previous = self.previous.get(target) if self.update_unchanged else None
            conditional = {}
            if previous and (self.previous_dir / previous["local"].lstrip("/")).is_file():
                if previous.get("etag"):
                    conditional["If-None-Match"] = previous["etag"]
                if previous.get("last_modified"):
                    conditional["If-Modified-Since"] = previous["last_modified"]
            with self._fetch(target, headers=conditional or None) as response:
                unchanged = conditional and getattr(response, "status_code", None) == 304
                if not unchanged:
                    response.raise_for_status()
                headers = getattr(response, "headers", None) or {}
                mime = (previous["mime"] if unchanged else
                        headers.get("Content-Type", "").split(";", 1)[0].strip().lower())
                if hint == "html" or mime in ("text/html", "application/xhtml+xml"):
                    kind = "html"
                elif hint == "css" or mime == "text/css":
                    kind = "css"
                elif hint == "js" or mime in ("text/javascript", "application/javascript", "application/x-javascript"):
                    kind = "js"
                elif hint == "manifest" or mime == "application/manifest+json":
                    kind = "manifest"
                else:
                    kind = mime
                if unchanged:
                    data = bytearray((self.previous_dir / previous["local"].lstrip("/")).read_bytes())
                    self.reused += 1
                else:
                    length = headers.get("Content-Length")
                    if length and int(length) > self.max_bytes:
                        raise ValueError("resource exceeds size limit")
                    data = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise ValueError("resource exceeds size limit")
                rel = self._destination(target, kind)
                local = "/" + rel.as_posix()
                # Mark as cached before recursive processing to break asset cycles.
                self.visited[target] = local
                # After a redirect ("/designer" to "/designer/") relative links
                # resolve against where the page really is, as in a browser.
                final = (getattr(response, "url", None) or target).split("#", 1)[0]
                if final != target:
                    final_parts = urlsplit(final)
                    if (final_parts.scheme.lower(), final_parts.netloc.lower()) == self.origin:
                        self.visited.setdefault(final, local)
                base = final
                if kind in ("html", "css", "js", "manifest"):
                    charset = text_encoding(response.headers.get("Content-Type", ""), bytes(data), kind)
                    text = bytes(data).decode(charset, errors="replace")
                    if kind == "html":
                        # Don't archive anti-bot / login challenge pages as a valid site.
                        if (re.search(r"<title>\s*Just a moment[.\s]*</title>", text, re.I)
                                or "cf-chl-opt" in text):
                            raise ValueError("Cloudflare verification page returned instead of the website")
                        if local not in self.html_paths:
                            self.html_paths.add(local)
                            self.pages += 1
                        text = self._rewrite_html(text, base)
                    elif kind == "css":
                        text = self._rewrite_css(text, base)
                    elif kind == "js":
                        text = self._rewrite_js(text, base)
                    elif kind == "manifest":
                        text = self._rewrite_manifest(text, base)
                    data = bytearray(text.encode("utf-8"))
                filename = self.root / rel
                filename.parent.mkdir(parents=True, exist_ok=True)
                filename.write_bytes(data)
                if kind not in TEXT_KINDS:
                    source = previous if unchanged else headers
                    etag = source.get("etag") if unchanged else headers.get("ETag")
                    modified = source.get("last_modified") if unchanged else headers.get("Last-Modified")
                    if etag or modified:
                        self.validators[target] = {"etag": etag, "last_modified": modified,
                                                   "mime": mime, "local": local}
                return local
        except (*FETCH_ERRORS, ValueError, OSError, LookupError) as exc:
            self.visited[target] = None
            self.failures.append(f"{target}: {exc}")
            self.progress(f"Skipped {target}: {exc}")
            return None
        finally:
            self.in_progress.discard(target)

    def _replace_link(self, base: str, link: str, hint: str = "", navigation: bool = False) -> str:
        target = self._resolve(base, link)
        if target is None or (navigation and not self._in_crawl_scope(target)):
            return link
        fragment = urlsplit(link).fragment
        if (navigation and target not in self.visited and self._page_candidate(target)
                and not self._is_page_link(target)):
            # robots.txt or a filter keeps WebMirror out of this page: link to it online.
            return target + ("#" + fragment if fragment else "")
        if navigation and self._is_page_link(target) and target not in self.visited:
            # Save linked pages later, from the queue, at the path linked to now.
            local = self.queued_pages.get(target)
            if local is None and target not in self.in_progress:
                if len(self.queued_pages) < self.max_crawl_pages:
                    local = "/" + self._destination(target, "html").as_posix()
                    self.queued_pages[target] = local
                    self.page_queue.append(target)
                else:
                    if "page limit reached" not in self.failures:
                        self.failures.append("page limit reached")
                        self.progress(f"Page limit reached; pages after {self.max_crawl_pages} link to the live site.")
                    local = "/" + self._destination(target, "html").as_posix()
                    self.unfetched_pages.setdefault(target, local)
            if local:
                return local + ("#" + fragment if fragment else "")
        saved = self._download(target, hint=hint)
        if not saved and navigation and self._is_page_link(target):
            # A page still being saved (a link back to it) will be at its usual
            # path. One the static crawl could not fetch, often because the site
            # refuses non-browser requests, is opened by the browser capture.
            saved = "/" + self._destination(target, "html").as_posix()
            if target not in self.in_progress:
                self.unfetched_pages[target] = saved
        if not saved:
            return link
        return saved + ("#" + fragment if fragment else "")

    def _rewrite_css(self, text: str, base: str) -> str:
        def import_replace(match: re.Match) -> str:
            url = self._replace_link(base, match["url"], hint="css")
            return match[1] + match["q"] + url + match["q"]

        text = CSS_IMPORT.sub(import_replace, text)

        def url_replace(match: re.Match) -> str:
            value = self._replace_link(base, match["url"])
            return f"url({match['q']}{value}{match['q']})"

        return CSS_URL.sub(url_replace, text)

    def _webpack_chunks(self, text: str, base: str) -> list[str]:
        """Every chunk a webpack runtime can load later, as absolute URLs.

        Chunks load only when the page needs them, often when a control is first
        used, so a browser visit misses some; offline, a missing one breaks the app.
        """
        public = WEBPACK_PUBLIC_PATH.search(text)
        if public:
            root = urljoin(base, public["path"])
        elif "/_next/static/" in base:
            root = base.split("/_next/static/")[0] + "/_next/"
        else:
            return []
        paths = {match["path"] for match in WEBPACK_CHUNK_PATH.finditer(text)}
        for match in WEBPACK_CHUNK_MAP.finditer(text):
            hashes = dict(re.findall(r"""['"]?([\w-]+)['"]?\s*:\s*['"]([0-9a-f]{6,})['"]""", match["map"]))
            # Names that differ from the chunk id: (5099===e?"892990c8":e) or ({5099:"name"})[e]||e.
            names = dict(re.findall(r"""(\d+)\s*===\s*\w+\s*\?\s*['"]([^'"]+)['"]""", match["name"]))
            names.update(re.findall(r"""['"]?(\d+)['"]?\s*:\s*['"]([^'"]+)['"]""", match["name"]))
            for chunk, digest in hashes.items():
                paths.add(f"{match['folder']}{names.get(chunk, chunk)}{match['sep']}{digest}{match['ext']}")
        return sorted(urljoin(root, path) for path in paths)

    def _rewrite_js(self, text: str, base: str) -> str:
        for chunk in self._webpack_chunks(text, base):
            self._download(chunk, hint="css" if chunk.endswith(".css") else "js")
        def replace(match: re.Match) -> str:
            link = match["url"]
            # Bare package names aren't URLs: leave import "react" untouched.
            is_import = re.match(r"^\s*(import|export)\b", match["prefix"]) is not None
            if is_import and not (link.startswith((".", "/", "http://", "https://"))):
                # The page's import map resolves it, to a copy saved here.
                mapped = self._import_target(link)
                if mapped:
                    self._download(mapped, hint="js")
                return match[0]
            # fetch("/tools/" + name): the literal is only the start of a URL built
            # at run time. Same-site paths are kept in the mirror, so leave it as is.
            if text[match.end():match.end() + 40].lstrip().startswith("+"):
                return match[0]
            hint = ("js" if (is_import or match["prefix"].lstrip().startswith("import(")
                             or "Worker" in match["prefix"]) else "")
            return match["prefix"] + match["q"] + self._replace_link(base, link, hint=hint) + match["q"]
        text = JS_LITERAL.sub(replace, text)
        # Worker importScripts() can contain multiple comma-separated file names.
        def scripts(match: re.Match) -> str:
            args = re.sub(
                r"""(?P<q>['"])(?P<url>[^'"]+)(?P=q)""",
                lambda item: item["q"] + self._replace_link(base, item["url"], hint="js") + item["q"],
                match["args"],
            )
            return "importScripts(" + args + ")"
        return re.sub(r"\bimportScripts\s*\((?P<args>[^)]*)\)", scripts, text)

    def _rewrite_manifest(self, text: str, base: str) -> str:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for item in data.get("icons", []) + data.get("screenshots", []):
                    if isinstance(item, dict) and isinstance(item.get("src"), str):
                        item["src"] = self._replace_link(base, item["src"])
                if isinstance(data.get("start_url"), str):
                    data["start_url"] = self._replace_link(base, data["start_url"], hint="html", navigation=True)
                return json.dumps(data, ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        return text

    def _rewrite_html(self, text: str, base: str) -> str:
        soup = BeautifulSoup(text, "html.parser")
        effective_base = base
        base_tag = soup.find("base", href=True)
        if base_tag:
            effective_base = urljoin(base, base_tag["href"])
            # Browser must never keep an original absolute remote <base>.
            local_base = urlsplit(effective_base).path or "/"
            base_tag["href"] = local_base

        assets = {"script": "src", "img": "src", "source": "src",
                  "video": "src", "audio": "src", "track": "src",
                  "iframe": "src", "embed": "src", "object": "data",
                  "input": "src"}
        for tag in soup.find_all(True):
            if tag.name in assets and tag.has_attr(assets[tag.name]):
                attr = assets[tag.name]
                hint = "js" if tag.name == "script" else "html" if tag.name == "iframe" else ""
                tag[attr] = self._replace_link(effective_base, tag[attr], hint=hint)
            if tag.name == "video" and tag.has_attr("poster"):
                tag["poster"] = self._replace_link(effective_base, tag["poster"])
            if tag.name == "link" and tag.has_attr("href"):
                relations = set(tag.get("rel", []))
                if relations.intersection({"stylesheet", "icon", "shortcut", "apple-touch-icon",
                                           "manifest", "preload", "modulepreload"}):
                    hint = ("css" if "stylesheet" in relations else
                            "manifest" if "manifest" in relations else
                            "js" if "modulepreload" in relations else "")
                    tag["href"] = self._replace_link(effective_base, tag["href"], hint=hint)
            if tag.name == "a" and tag.has_attr("href"):
                tag["href"] = self._replace_link(effective_base, tag["href"], hint="html", navigation=True)
            if tag.has_attr("srcset"):
                # Preserve descriptors such as 2x and 480w.
                parts = []
                for item in tag["srcset"].split(","):
                    fields = item.strip().split(None, 1)
                    if fields:
                        fields[0] = self._replace_link(effective_base, fields[0])
                        parts.append(" ".join(fields))
                tag["srcset"] = ", ".join(parts)
            if tag.has_attr("style"):
                tag["style"] = self._rewrite_css(tag["style"], effective_base)
            if tag.name == "style" and tag.string:
                tag.string.replace_with(self._rewrite_css(str(tag.string), effective_base))
            if tag.name == "script" and not tag.has_attr("src") and tag.string:
                kind = (tag.get("type") or "").lower()
                if kind in ("", "module", "text/javascript", "application/javascript"):
                    tag.string.replace_with(self._rewrite_js(str(tag.string), effective_base))
                elif kind == "importmap":
                    tag.string.replace_with(self._rewrite_import_map(str(tag.string), effective_base))
        return declare_utf8(str(soup))

    def _rewrite_import_map(self, text: str, base: str) -> str:
        """Point an import map's modules, and its folder prefixes, at the saved copies.

        `"three": "https://cdn/.../three.js"` is downloaded like a script tag;
        `"three/addons/": "https://cdn/.../jsm/"` becomes the local folder its files
        are saved in, and they are downloaded as modules import them.
        """
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if not isinstance(data, dict):
            return text
        mappings = [data.get("imports")]
        if isinstance(data.get("scopes"), dict):
            mappings += list(data["scopes"].values())
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for name, value in mapping.items():
                target = self._resolve(base, value) if isinstance(value, str) else None
                if target is None:
                    continue
                self.import_map.setdefault(name, target)
                if value.endswith("/"):
                    folder = self._destination(target + "index.js", "js").parent
                    if folder.parts[:1] != ("_webmirror_longpaths",):
                        mapping[name] = "/" if folder == Path(".") else f"/{folder.as_posix()}/"
                else:
                    mapping[name] = self._replace_link(base, value, hint="js")
        return json.dumps(data, indent=2)

    def _import_target(self, specifier: str) -> str | None:
        """The URL a bare import such as "three/addons/x.js" maps to, if any."""
        if specifier in self.import_map:
            return self.import_map[specifier]
        prefixes = [name for name in self.import_map
                    if name.endswith("/") and specifier.startswith(name)]
        if not prefixes:
            return None
        name = max(prefixes, key=len)
        return self.import_map[name] + specifier[len(name):]


class _QuietHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript", ".mjs": "text/javascript",
        ".wasm": "application/wasm", ".webmanifest": "application/manifest+json",
        ".json": "application/json", ".svg": "image/svg+xml",
        ".woff2": "font/woff2", ".glb": "model/gltf-binary",
    }

    def __init__(self, *args, routes: dict[str, str] | None = None,
                 entry: str = "/index.html", route_base: str = "/", **kwargs):
        self.routes = routes or {}
        self.entry = entry
        self.route_base = route_base
        super().__init__(*args, **kwargs)

    def translate_path(self, path: str) -> str:
        # Match the original browser URL, including query-specific API/asset
        # responses, to the file saved on disk.
        parsed = urlsplit(path)
        lookup = parsed.path or "/"
        if parsed.query:
            lookup += "?" + parsed.query
        saved = self.routes.get(lookup)
        if saved:
            return super().translate_path(saved)
        actual = super().translate_path(path)
        if os.path.isfile(actual):
            return actual
        # SPA history navigation: reload /app/settings through the saved entry.
        # Never serve HTML in response to a missing .js/.css/image/API asset.
        wants_html = "text/html" in self.headers.get("Accept", "")
        target_path = parsed.path or "/"
        if wants_html and not PurePosixPath(target_path).suffix:
            return super().translate_path(self.entry)
        return actual

    def guess_type(self, path: str | os.PathLike[str]) -> str:
        # Saved HTML, CSS and JS are UTF-8; say so, or browsers may read them as Latin-1.
        kind = super().guess_type(path)
        if kind.startswith("text/") or kind in ("application/json", "application/manifest+json"):
            return kind + "; charset=utf-8"
        return kind

    def log_message(self, format: str, *args: object) -> None:
        pass


class MirrorServer:
    """Host one mirror at a time on loopback; safe to stop and restart from GUI."""

    def __init__(self, library: MirrorLibrary):
        self.library = library
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None
        self.url: str | None = None

    def start(self, record: MirrorRecord, port: int = 8000) -> str:
        if not 1 <= port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        folder = self.library.path(record)
        if not (folder / record.entry.lstrip("/")).is_file():
            raise FileNotFoundError("This mirror's entry page is missing.")
        self.stop()
        routes_path = folder / ".webmirror-routes.json"
        try:
            routes = json.loads(routes_path.read_text("utf-8")) if routes_path.is_file() else {}
        except (ValueError, OSError):
            routes = {}
        original = urlsplit(record.url)
        base = original.path if original.path.endswith("/") else posixpath.dirname(original.path) + "/"
        handler = partial(_QuietHandler, directory=str(folder), routes=routes,
                          entry=record.entry, route_base=base)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True, name="webmirror-http")
        self.thread.start()
        original = urlsplit(record.url)
        path = quote(original.path or "/", safe="/%:@")
        self.url = f"http://127.0.0.1:{port}" + path
        if original.query:
            self.url += "?" + original.query
        return self.url

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread:
            self.thread.join(timeout=3)
            self.thread = None
        self.url = None
