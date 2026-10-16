"""Capture public assets requested by JavaScript, with optional human verification.

A clean temporary Chromium profile is used for each capture. If a website
challenges the browser, the user may complete the verification in a *visible*
Chrome or Edge window. WebMirror does not solve or bypass anti-bot challenges,
use stored account cookies, or archive private browser profiles.
"""
from __future__ import annotations

from time import monotonic
from typing import Callable
from urllib.parse import urlsplit

CAPTURE_LIMIT = 160 * 1024 * 1024
VERIFICATION_TIMEOUT_SECONDS = 180
ALLOWED_CONTENT_TYPES = (
    "text/html", "text/css", "text/javascript", "application/javascript",
    "application/x-javascript", "application/json", "text/plain",
    "application/wasm", "application/manifest+json", "image/",
    "font/", "application/font-", "application/octet-stream",
    "text/xml", "application/xml",
)


class BrowserUnavailable(RuntimeError):
    """No usable Chromium browser or Playwright runtime was found."""


class VerificationIncomplete(RuntimeError):
    """The user declined, closed, or did not complete the verification."""


def is_verification_challenge(
    *, status: int | None = None, title: str = "", html: str = "",
) -> bool:
    """Identify common human-verification interstitials, not generic 403s.

    HTML is examined only for markers, never used to solve a challenge.
    """
    title = title.strip().lower()
    sample = html[:80000].lower()
    if "just a moment" in title or "verify you are human" in title:
        return True
    if "attention required" in title and "cloudflare" in sample:
        return True
    if any(marker in sample for marker in (
        "cf-chl-opt", "cf-challenge-running", "id=\"challenge-running\"",
        "id='challenge-running'", "challenges.cloudflare.com/turnstile",
    )):
        return True
    if status in (403, 429, 503) and any(marker in sample for marker in (
        "captcha", "robot check", "human verification", "verify you are human",
        "cloudflare", "access to this page has been denied",
    )):
        return True
    return False


def _page_is_challenged(page, response) -> bool:
    status = response.status if response is not None else None
    try:
        title = page.title()
        html = page.content()
    except Exception:
        return False
    return is_verification_challenge(status=status, title=title, html=html)


def _launch_chromium(playwright, *, headless: bool):
    """Use a local installed browser; never install one behind the user's back."""
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            options = {"headless": headless}
            if channel:
                options["channel"] = channel
            return playwright.chromium.launch(**options)
        except Exception as exc:
            errors.append(str(exc).splitlines()[0])
    raise BrowserUnavailable(
        "No compatible Chrome/Edge/Chromium browser available. Install Google "
        "Chrome or Microsoft Edge. " + "; ".join(errors[:2])
    )


def capture_public_assets(
    url: str, *, max_files: int, max_bytes: int,
    progress: Callable[[str], None],
    allow_manual_verification: bool = False,
    on_verification_required: Callable[[str], None] | None = None,
    verification_timeout: int = VERIFICATION_TIMEOUT_SECONDS,
) -> list[tuple[str, str, bytes]]:
    """Observe anonymous GET responses and optionally invite the user to verify.

    When an interstitial is detected, a visible temporary browser opens and
    the user may solve the check manually. This capture never reuses the user's
    signed-in browser profile; closing the window cancels verification.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Browser capture needs Playwright. Install with pip install 'webmirror[dynamic]'."
        ) from exc

    origin = (urlsplit(url).scheme.lower(), urlsplit(url).netloc.lower())
    captured: dict[str, tuple[str, str, bytes]] = {}
    total_bytes = 0
    failed: list[str] = []

    def on_response(response) -> None:
        nonlocal total_bytes
        target = response.url.split("#", 1)[0]
        parsed = urlsplit(target)
        if (parsed.scheme.lower(), parsed.netloc.lower()) != origin:
            return
        if target in captured or len(captured) >= max_files:
            return
        if response.request.method != "GET" or not 200 <= response.status < 300:
            return
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not any(mime.startswith(allowed) for allowed in ALLOWED_CONTENT_TYPES):
            return
        size = response.headers.get("content-length", "")
        try:
            if size and int(size) > max_bytes:
                return
            body = response.body()
        except Exception:
            return
        if len(body) > max_bytes or total_bytes + len(body) > CAPTURE_LIMIT:
            return
        if mime == "text/html" and is_verification_challenge(html=body.decode("utf-8", errors="replace")):
            return
        total_bytes += len(body)
        captured[target] = (target, mime, body)

    def create_page(browser):
        context = browser.new_context(service_workers="block")
        page = context.new_page()
        page.on("response", on_response)
        page.on("requestfailed", lambda req: failed.append(req.url[:180]))
        return context, page

    def navigate(page):
        response = None
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1300)
        except Exception as exc:
            progress(f"Browser navigation incomplete: {str(exc)[:150]}")
        return response

    def settle(page):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1100)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(600)
        except Exception as exc:
            progress(f"Browser preview incomplete: {str(exc)[:150]}")

    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright, headless=True)
        try:
            context, page = create_page(browser)
            try:
                response = navigate(page)
                challenged = _page_is_challenged(page, response)
                if not challenged:
                    settle(page)
                    challenged = _page_is_challenged(page, response)
            finally:
                context.close()
        finally:
            browser.close()

        if challenged and allow_manual_verification:
            # Do not save response bodies belonging to the challenge itself.
            captured.clear()
            total_bytes = 0
            failed.clear()
            progress("Human verification detected; opening a browser window.")
            browser = _launch_chromium(playwright, headless=False)
            try:
                context, page = create_page(browser)
                try:
                    response = navigate(page)
                    if _page_is_challenged(page, response):
                        message = (
                            "Website verification required. In the opened browser, "
                            "complete the human check. Do not sign in or enter private "
                            "information. WebMirror resumes automatically when the "
                            "website loads, or cancels if you close the window."
                        )
                        progress(message)
                        if on_verification_required:
                            on_verification_required(message)
                        deadline = monotonic() + max(1, verification_timeout)
                        while _page_is_challenged(page, None):
                            if page.is_closed():
                                raise VerificationIncomplete("The verification browser was closed.")
                            if monotonic() >= deadline:
                                raise VerificationIncomplete(
                                    "Verification was not completed within the allowed time."
                                )
                            page.wait_for_timeout(750)
                        progress("Verification completed; capturing visible website resources.")
                    settle(page)
                    if _page_is_challenged(page, None):
                        raise VerificationIncomplete(
                            "The website presented another verification check; capture was stopped."
                        )
                finally:
                    context.close()
            finally:
                browser.close()
        elif challenged:
            progress(
                "Site verification detected. Enable manual verification in the "
                "GUI to complete the check in a visible browser."
            )

    if failed:
        progress(f"Browser had {len(failed)} failed requests; some features may need the internet.")
    progress(f"Browser observed {len(captured)} same-origin assets ({total_bytes // 1024} KB).")
    return list(captured.values())
