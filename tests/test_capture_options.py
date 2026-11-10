"""Tests for the optional capture features: browser-like requests, ad blocking,
polite crawling (auto-throttle, robots.txt, pause and resume) and automatic
challenge solving. Local servers and fakes only; no real websites."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from webmirror.browser import capture_public_assets
from webmirror.core import MirrorEngine, MirrorLibrary, MirrorPaused
from webmirror.net import AutoThrottle, browserlike_available, host_is_blocked, retry_after_seconds
from tests.test_browser import FakePage, FakeResponse, fake_playwright


class Handler(SimpleHTTPRequestHandler):
    """Serves a folder; answers 429 for paths listed in `busy` until they run out."""

    busy: dict[str, int] = {}
    requests_seen: list[str] = []

    def do_GET(self):
        type(self).requests_seen.append(self.path)
        remaining = self.busy.get(self.path, 0)
        if remaining:
            self.busy[self.path] = remaining - 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture
def site(tmp_path):
    """Start a server for a dict of files; returns (base_url, handler_class)."""
    servers = []

    def start(files: dict[str, str], busy: dict[str, int] | None = None):
        root = tmp_path / f"site{len(servers)}"
        for rel, content in files.items():
            output = root / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8")
        handler = type("SiteHandler", (Handler,), {"busy": dict(busy or {}), "requests_seen": []})
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(root)))
        Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", handler

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


DEMO = {
    "demo/index.html": '<html><head><link rel="stylesheet" href="main.css"></head>'
                       '<body><a href="about.html">About</a><a href="team.html">Team</a></body></html>',
    "demo/about.html": '<html><body><img src="logo.svg"></body></html>',
    "demo/team.html": "<html><body>Team</body></html>",
    "demo/main.css": "body{color:red}",
    "demo/logo.svg": "<svg xmlns='http://www.w3.org/2000/svg'></svg>",
}


@pytest.mark.skipif(not browserlike_available(), reason="curl_cffi is not installed")
def test_browserlike_requests_mirror_the_site(site, tmp_path):
    base, handler = site(DEMO)
    library = MirrorLibrary(tmp_path / "data")
    record = MirrorEngine(library, browserlike=True).mirror(base + "/demo/")
    folder = library.path(record)
    assert record.pages == 3 and record.failures == 0
    assert 'href="/demo/main.css"' in (folder / "demo/index.html").read_text()
    assert (folder / "demo/logo.svg").is_file()


def test_ad_hosts_and_their_subdomains_are_blocked():
    assert host_is_blocked("https://doubleclick.net/x.js")
    assert host_is_blocked("https://stats.g.doubleclick.net/x.js")
    assert not host_is_blocked("https://example.org/doubleclick.net.js")
    assert not host_is_blocked("https://cdn.jsdelivr.net/npm/three/build/three.module.js")


def test_block_ads_skips_trackers_without_counting_failures(site, tmp_path):
    base, _ = site({"index.html": '<html><head><script src="https://ad.doubleclick.net/tag.js"></script>'
                                  '<script src="app.js"></script></head></html>',
                    "app.js": "run();"})
    library = MirrorLibrary(tmp_path / "data")
    logs = []
    record = MirrorEngine(library, block_ads=True, progress=logs.append).mirror(base + "/")
    html = (library.path(record) / "index.html").read_text()
    assert 'src="https://ad.doubleclick.net/tag.js"' in html  # left pointing online
    assert 'src="/app.js"' in html
    assert record.failures == 0
    assert not any("doubleclick" in message and "Downloading" in message for message in logs)
    assert "1 ad/tracker requests blocked" in logs[-1]


def test_auto_throttle_follows_latency_and_backs_off():
    slept = []
    now = [0.0]
    throttle = AutoThrottle(start_delay=0.5, clock=lambda: now[0], sleeper=slept.append)
    url = "https://example.org/a"
    throttle.wait(url)               # first request: no wait
    throttle.record(url, latency=1.5)
    assert throttle.delay(url) == pytest.approx(1.0)  # (0.5 + 1.5) / 2
    throttle.wait(url)
    assert slept == [pytest.approx(1.0)]
    throttle.record(url, latency=0.1, status=429)
    assert throttle.delay(url) == pytest.approx(2.0)  # doubled
    throttle.record(url, latency=0.1, status=503, retry_after=30)
    assert throttle.delay(url) == pytest.approx(30.0)  # Retry-After wins when longer
    throttle.set_floor(url, 45)
    assert throttle.delay(url) == 45
    assert throttle.delay("https://other.example/") == 0.5  # per host


def test_retry_after_accepts_seconds_and_dates():
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0  # in the past
    assert retry_after_seconds("soon") is None
    assert retry_after_seconds(None) is None


def test_polite_crawl_retries_a_busy_page(site, tmp_path):
    base, handler = site(DEMO, busy={"/demo/about.html": 2})
    library = MirrorLibrary(tmp_path / "data")
    logs = []
    throttle = AutoThrottle(sleeper=lambda seconds: None)
    record = MirrorEngine(library, polite=True, throttle=throttle, progress=logs.append).mirror(base + "/demo/")
    assert record.failures == 0 and record.pages == 3
    assert handler.requests_seen.count("/demo/about.html") == 3
    assert any("answered 429" in message for message in logs)


def test_polite_crawl_gives_up_after_the_retry_limit(site, tmp_path):
    base, handler = site(DEMO, busy={"/demo/team.html": 10})
    library = MirrorLibrary(tmp_path / "data")
    throttle = AutoThrottle(sleeper=lambda seconds: None, max_retries=2)
    record = MirrorEngine(library, polite=True, throttle=throttle).mirror(base + "/demo/")
    assert handler.requests_seen.count("/demo/team.html") == 3
    assert record.failures == 1


def test_robots_txt_is_obeyed_when_asked(site, tmp_path):
    files = dict(DEMO)
    files["robots.txt"] = "User-agent: *\nDisallow: /demo/team.html\nDisallow: /demo/logo.svg\nCrawl-delay: 2\n"
    base, handler = site(files)
    library = MirrorLibrary(tmp_path / "data")
    slept = []
    throttle = AutoThrottle(start_delay=0.0, sleeper=slept.append)
    record = MirrorEngine(library, obey_robots=True, throttle=throttle).mirror(base + "/demo/")
    folder = library.path(record)
    assert "/demo/team.html" not in handler.requests_seen
    assert "/demo/logo.svg" not in handler.requests_seen
    # The disallowed page stays a link to the live site.
    assert f'href="{base}/demo/team.html"' in (folder / "demo/index.html").read_text()
    assert record.pages == 2
    assert throttle.delay(base + "/") == 2
    assert slept  # Crawl-delay paced the crawl

    # Without the option, robots.txt is not consulted.
    base2, handler2 = site(files)
    MirrorEngine(MirrorLibrary(tmp_path / "data2")).mirror(base2 + "/demo/")
    assert "/robots.txt" not in handler2.requests_seen
    assert "/demo/team.html" in handler2.requests_seen


def test_pause_keeps_progress_and_resume_finishes(site, tmp_path):
    base, handler = site(DEMO)
    library = MirrorLibrary(tmp_path / "data")
    url = base + "/demo/"
    pause = Event()

    def progress(message):
        if message == f"Downloading {base}/demo/main.css":
            pause.set()  # pause while the entry page is being saved

    with pytest.raises(MirrorPaused):
        MirrorEngine(library, progress=progress, pause_event=pause).mirror(url)
    assert library.is_paused(url)
    assert library.list() == []
    assert "/demo/about.html" not in handler.requests_seen
    paused_files = library.paused_path(url)
    assert (paused_files / "demo/index.html").is_file()

    handler.requests_seen.clear()
    record = MirrorEngine(library).mirror(url, resume=True)
    assert record.pages == 3 and record.failures == 0
    assert "/demo/index.html" not in handler.requests_seen  # not fetched again
    assert "/demo/" not in handler.requests_seen
    assert "/demo/about.html" in handler.requests_seen
    assert not library.is_paused(url) and not paused_files.exists()
    folder = library.path(record)
    assert (folder / "demo/team.html").is_file()
    assert not (folder / ".webmirror-state.json").exists()
    assert library.list() == [record]


def test_fresh_mirror_discards_a_paused_one(site, tmp_path):
    base, _ = site(DEMO)
    library = MirrorLibrary(tmp_path / "data")
    url = base + "/demo/"
    pause = Event()
    pause.set()
    with pytest.raises(MirrorPaused):
        MirrorEngine(library, pause_event=pause).mirror(url)
    assert library.is_paused(url)
    MirrorEngine(library).mirror(url)  # resume not requested: starts over
    assert not library.is_paused(url)


class SolvedPage(FakePage):
    """A stealth-browser page whose challenge was already solved."""

    def __init__(self):
        super().__init__(headless=True)
        self.resolved = True

    def goto(self, url, **kwargs):
        self.url = url
        response = FakeResponse(url, 200, b"<html><head><title>Web App</title></head><body>ok</body></html>")
        self.callbacks["response"](response)
        self.callbacks["response"](FakeResponse("https://example.org/app.js", 200,
                                                b"window.ready = true;", "application/javascript"))
        return response

    def wait_for_timeout(self, duration):
        pass

    def eval_on_selector_all(self, selector, script):
        return []


class FakeStealthContext:
    def __init__(self, page):
        self.pages = [page]
        self.closed = False

    def route(self, pattern, handler):
        pass

    def close(self):
        self.closed = True


def stealth_browser(monkeypatch, page):
    """Replace the stealth browser with one fake page; record how it was used."""
    used = {"contexts": [], "solved": []}

    def stealth_context(playwright):
        context = FakeStealthContext(page)
        used["contexts"].append(context)
        return context, None

    monkeypatch.setattr("webmirror.browser._stealth_context", stealth_context)
    monkeypatch.setattr("webmirror.browser.solve", lambda pg, log: used["solved"].append(pg) or True)
    return used


def test_auto_solve_captures_through_the_stealth_browser(monkeypatch):
    pw = fake_playwright(monkeypatch)
    used = stealth_browser(monkeypatch, SolvedPage())
    logs = []
    assets = capture_public_assets(
        "https://example.org/app/", max_files=10, max_bytes=100000,
        progress=logs.append, allow_manual_verification=True,
        on_verification_required=lambda message: pytest.fail("manual check was not needed"),
        auto_solve=True,
    )
    assert pw.launched == [True]  # no visible browser
    assert used["solved"] and used["contexts"][0].closed
    assert [item[0] for item in assets] == ["https://example.org/app/", "https://example.org/app.js"]
    assert any("passed automatically" in message for message in logs)


def test_auto_solve_failure_falls_back_to_manual_verification(monkeypatch):
    pw = fake_playwright(monkeypatch)

    class StillChallenged(FakePage):
        def __init__(self):
            super().__init__(headless=True, never_solve=True)

        @property
        def pages(self):
            return [self]

    stealth_browser(monkeypatch, StillChallenged())
    notices = []
    logs = []
    assets = capture_public_assets(
        "https://example.org/app/", max_files=10, max_bytes=100000,
        progress=logs.append, allow_manual_verification=True,
        on_verification_required=notices.append, verification_timeout=2,
        auto_solve=True,
    )
    assert pw.launched == [True, False]
    assert len(notices) == 1
    assert any("could not be passed automatically" in message for message in logs)
    assert [item[0] for item in assets][0] == "https://example.org/app/"


def test_auto_solve_without_a_browser_says_so(monkeypatch):
    fake_playwright(monkeypatch)
    from webmirror.browser import BrowserUnavailable

    def no_browser(playwright):
        raise BrowserUnavailable("No Chrome, Edge or Chromium browser could start for automatic solving.")

    monkeypatch.setattr("webmirror.browser._stealth_context", no_browser)
    logs = []
    assets = capture_public_assets(
        "https://example.org/app/", max_files=10, max_bytes=100000,
        progress=logs.append, auto_solve=True,
    )
    assert assets == []
    assert any("could start for automatic solving" in message for message in logs)


def test_cloudflare_challenge_kinds_are_detected():
    from webmirror.cloudflare import challenge_cleared, detect_challenge
    assert detect_challenge("window._cf_chl_opt={cType: 'managed', cRay: 'x'}") == "managed"
    assert detect_challenge("cType: 'non-interactive'") == "non-interactive"
    assert detect_challenge('<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>') == "embedded"
    assert detect_challenge("<html><body>Hello</body></html>") is None
    assert challenge_cleared("<html>ok</html>", "managed")
    assert not challenge_cleared("cType: 'managed'", "managed")
    assert challenge_cleared("cType: 'managed'", "embedded")


def test_cloudflare_scripts_are_not_saved_as_site_files(monkeypatch):
    pw = fake_playwright(monkeypatch)

    class Page(SolvedPage):
        def goto(self, url, **kwargs):
            response = super().goto(url, **kwargs)
            self.callbacks["response"](FakeResponse(
                "https://example.org/cdn-cgi/challenge-platform/h/b/orchestrate/x.js", 200,
                b"cf();", "application/javascript"))
            return response

    page = Page()

    def launch(*, headless, **kwargs):
        from tests.test_browser import FakeBrowser
        browser = FakeBrowser(headless)
        browser.page = page
        return browser

    pw.launch = launch
    assets = capture_public_assets("https://example.org/app/", max_files=10, max_bytes=100000,
                                   progress=lambda message: None)
    assert [item[0] for item in assets] == ["https://example.org/app/", "https://example.org/app.js"]


def test_engine_passes_options_to_the_browser_capture(tmp_path, monkeypatch):
    from unittest.mock import patch

    seen = {}

    def fake_capture(url, **kwargs):
        seen.update(kwargs)
        return [(url, "text/html", b"<html><body>hi</body></html>")]

    class Forbidden:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            import requests
            raise requests.HTTPError("403")

    with patch("requests.Session.get", return_value=Forbidden()), patch(
        "webmirror.browser.capture_public_assets", side_effect=fake_capture
    ):
        MirrorEngine(MirrorLibrary(tmp_path / "data"), browser_capture=True,
                     block_ads=True, auto_solve_challenges=True).mirror("https://example.org/")
    assert seen["auto_solve"] is True
    assert "doubleclick.net" in seen["blocked_domains"]


def test_update_mode_reuses_unchanged_files_and_refetches_changed_ones(site, tmp_path):
    import os
    base, handler = site(DEMO | {"demo/about.html": '<html><body><img src="logo.svg"><img src="photo.png"></body></html>',
                                 "demo/photo.png": "png-v1"})
    library = MirrorLibrary(tmp_path / "data")
    url = base + "/demo/"
    first = MirrorEngine(library, update_unchanged=True).mirror(url)
    folder = library.path(first)
    assert (folder / ".webmirror-validators.json").is_file()

    # Change one image (a newer Last-Modified); leave the other alone.
    photo = tmp_path / "site0/demo/photo.png"
    photo.write_text("png-v2", encoding="utf-8")
    later = photo.stat().st_mtime + 30
    os.utime(photo, (later, later))
    logs = []
    second = MirrorEngine(library, update_unchanged=True, progress=logs.append).mirror(url)
    folder = library.path(second)
    assert (folder / "demo/logo.svg").read_text().startswith("<svg")
    assert (folder / "demo/photo.png").read_text() == "png-v2"
    assert second.failures == 0
    assert "1 unchanged files reused" in logs[-1]


def test_filters_leave_out_and_add_urls(site, tmp_path):
    files = DEMO | {"extra/page.html": "<html><body>Extra</body></html>",
                    "other/page.html": "<html><body>Other</body></html>"}
    files["demo/index.html"] = files["demo/index.html"].replace(
        "</body>", '<a href="/extra/page.html">Extra</a><a href="/other/page.html">Other</a></body>')
    base, handler = site(files)
    library = MirrorLibrary(tmp_path / "data")
    logs = []
    record = MirrorEngine(library, filters="-*.svg +*/extra/* -*/team.html", progress=logs.append).mirror(base + "/demo/")
    folder = library.path(record)
    index = (folder / "demo/index.html").read_text()
    assert "/demo/logo.svg" not in handler.requests_seen
    assert (folder / "extra/page.html").is_file()           # "+" widened the crawl
    assert 'href="/other/page.html"' in index and not (folder / "other/page.html").exists()
    assert f'href="{base}/demo/team.html"' in index          # "-" page links online
    assert record.failures == 0
    assert "left out by filters" in logs[-1]


def test_filter_rules_are_validated():
    from webmirror.core import parse_filters
    assert parse_filters("") == []
    rules = parse_filters("+*.glb -*/forum/*")
    assert [include for include, _ in rules] == [True, False]
    assert rules[1][1].match("https://x.org/forum/a")
    with pytest.raises(ValueError, match="must start with"):
        parse_filters("*.glb")
