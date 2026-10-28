"""Tests for manual human-verification handoff using a fake browser.

These tests do not access real websites or solve CAPTCHAs.
"""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

import pytest

from webmirror.browser import (
    VerificationIncomplete, capture_public_assets, is_verification_challenge,
)


def test_challenge_detection_distinguishes_403_from_interstitial():
    assert is_verification_challenge(status=403, title="Just a moment...", html="")
    assert is_verification_challenge(
        status=200, html="<html><div id='challenge-running'>Checking...</div></html>"
    )
    assert is_verification_challenge(
        status=403, html="<html><body>verify you are human</body></html>"
    )
    assert not is_verification_challenge(status=403, title="Not Found", html="<h1>Forbidden</h1>")
    assert not is_verification_challenge(
        status=200, title="Contact", html="<div class='g-recaptcha'></div>"
    )


class FakeResponse:
    def __init__(self, url, status, body, content_type="text/html"):
        self.url = url
        self.status = status
        self.request = SimpleNamespace(method="GET")
        self.headers = {"content-type": content_type, "content-length": str(len(body))}
        self._body = body

    def body(self):
        return self._body


class FakePage:
    def __init__(self, headless: bool, *, never_solve: bool = False):
        self.headless = headless
        self.never_solve = never_solve
        self.resolved = False
        self.closed = False
        self.callbacks = {}

    def on(self, event, fn):
        self.callbacks[event] = fn

    def goto(self, url, **kwargs):
        self.url = url
        response = FakeResponse(
            url, 403, b"<title>Just a moment...</title>cf-chl-opt"
        )
        self.callbacks["response"](response)
        return response

    def title(self):
        return "Web App" if self.resolved else "Just a moment..."

    def content(self):
        if self.resolved:
            return "<html><head><title>Web App</title></head><body>All set</body></html>"
        return "<title>Just a moment...</title>cf-chl-opt"

    def wait_for_timeout(self, duration):
        if not self.headless and duration == 750 and not self.never_solve:
            self.resolved = True
            self.callbacks["response"](
                FakeResponse(
                    self.url, 200,
                    b"<html><head><title>Web App</title></head><body><script src='/app.js'></script></body></html>",
                )
            )
            self.callbacks["response"](
                FakeResponse(
                    "https://example.org/app.js", 200,
                    b"window.ready = true;", "application/javascript",
                )
            )

    def evaluate(self, script):
        return None

    def is_closed(self):
        return self.closed


class FakeBrowser:
    def __init__(self, headless, *, never_solve=False):
        self.headless = headless
        self.page = FakePage(headless, never_solve=never_solve)

    def new_context(self, **kwargs):
        return FakeContext(self.page)

    def close(self):
        pass


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def close(self):
        pass


class FakePlaywright:
    def __init__(self, *, never_solve=False):
        self.launched = []
        self.never_solve = never_solve
        self.chromium = self

    def launch(self, *, headless, **kwargs):
        self.launched.append(headless)
        return FakeBrowser(headless, never_solve=self.never_solve)


class FakeManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, *args):
        pass


def fake_playwright(monkeypatch, *, never_solve=False):
    playwright = FakePlaywright(never_solve=never_solve)
    package = ModuleType("playwright")
    package.__path__ = []
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakeManager(playwright)
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return playwright


def test_manual_challenge_opens_headed_browser_and_collects_verified_resources(monkeypatch):
    pw = fake_playwright(monkeypatch)
    notifications = []
    logs = []
    assets = capture_public_assets(
        "https://example.org/app/", max_files=10, max_bytes=100000,
        progress=logs.append, allow_manual_verification=True,
        on_verification_required=notifications.append,
        verification_timeout=2,
    )
    assert pw.launched == [True, False]
    assert len(notifications) == 1
    assert "complete the human check" in notifications[0]
    assert [item[0] for item in assets] == [
        "https://example.org/app/",
        "https://example.org/app.js",
    ]
    assert b"Web App" in assets[0][2]
    assert any("Verification completed" in message for message in logs)


def test_challenge_without_manual_option_does_not_prompt_or_launch_headed(monkeypatch):
    pw = fake_playwright(monkeypatch)
    notices = []
    assets = capture_public_assets(
        "https://example.org/app/", max_files=10, max_bytes=100000,
        progress=lambda msg: None,
        allow_manual_verification=False,
        on_verification_required=notices.append,
    )
    assert pw.launched == [True]
    assert notices == []
    assert assets == []


def test_manual_verification_times_out_without_persisting_challenge(monkeypatch):
    pw = fake_playwright(monkeypatch, never_solve=True)
    with pytest.raises(VerificationIncomplete, match="within the allowed time"):
        capture_public_assets(
            "https://example.org/app/", max_files=10, max_bytes=100000,
            progress=lambda msg: None,
            allow_manual_verification=True,
            verification_timeout=0,
        )
    assert pw.launched == [True, False]


class WalkingPage(FakePage):
    """A site whose pages link to each other, some links added by script."""

    LINKS = {
        "https://example.org/tools/": ["https://example.org/tools/a/", "https://example.org/blog/"],
        "https://example.org/tools/a/": ["https://example.org/tools/b/#top", "https://example.org/tools/"],
        "https://example.org/tools/b/": ["https://example.org/tools/c/"],
        "https://example.org/tools/c/": [],
    }

    def __init__(self):
        super().__init__(True)
        self.opened = []

    def goto(self, url, **kwargs):
        self.url = url
        self.opened.append(url)
        response = FakeResponse(url, 200, f"<title>{url}</title>".encode())
        self.callbacks["response"](response)
        self.callbacks["response"](FakeResponse(url + "engine.wasm", 200, b"\0asm", "application/wasm"))
        return response

    def title(self):
        return "Tool"

    def content(self):
        return "<html><body>Tool</body></html>"

    def wait_for_timeout(self, duration):
        pass

    def eval_on_selector_all(self, selector, script):
        assert selector == "a[href]"
        return self.LINKS[self.url]


def walking_playwright(monkeypatch):
    playwright = fake_playwright(monkeypatch)
    page = WalkingPage()

    def launch(*, headless, **kwargs):
        browser = FakeBrowser(headless)
        browser.page = page
        return browser

    playwright.launch = launch
    return page


def test_capture_opens_linked_pages_under_the_follow_rule(monkeypatch):
    page = walking_playwright(monkeypatch)
    assets = capture_public_assets(
        "https://example.org/tools/", max_files=50, max_bytes=1000, progress=lambda m: None,
        follow=lambda link: link.startswith("https://example.org/tools/"), max_pages=10,
    )
    assert page.opened == [
        "https://example.org/tools/", "https://example.org/tools/a/",
        "https://example.org/tools/b/", "https://example.org/tools/c/",
    ]
    urls = {url for url, _, _ in assets}
    assert "https://example.org/tools/b/engine.wasm" in urls
    assert "https://example.org/blog/" not in page.opened


def test_capture_stops_at_the_page_limit(monkeypatch):
    page = walking_playwright(monkeypatch)
    messages = []
    capture_public_assets(
        "https://example.org/tools/", max_files=50, max_bytes=1000, progress=messages.append,
        pages=["https://example.org/tools/c/"],
        follow=lambda link: link.startswith("https://example.org/tools/"), max_pages=2,
    )
    assert page.opened == ["https://example.org/tools/", "https://example.org/tools/c/"]
    assert any("Page limit reached" in m for m in messages)


def test_capture_without_follow_opens_only_the_entry_page(monkeypatch):
    page = walking_playwright(monkeypatch)
    capture_public_assets(
        "https://example.org/tools/", max_files=50, max_bytes=1000, progress=lambda m: None,
    )
    assert page.opened == ["https://example.org/tools/"]
