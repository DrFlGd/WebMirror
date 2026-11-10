"""Capture public assets requested by JavaScript, with optional human verification.

A clean temporary Chromium profile is used for each capture. If a website
challenges the browser, the user may complete the verification in a *visible*
Chrome or Edge window. Only when the user turns on automatic solving does
WebMirror hand a Cloudflare check to Scrapling's stealth browser (the optional
"stealth" extra) to solve. It never uses stored account cookies or archives
private browser profiles.
"""
from __future__ import annotations

from time import monotonic
from typing import Callable, Iterable
from urllib.parse import urlsplit

# One capture may now walk many pages of a site, so the byte budget covers them all.
CAPTURE_LIMIT = 320 * 1024 * 1024
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


def _host_is_blocked(url: str, blocked) -> bool:
    from .net import host_is_blocked
    return host_is_blocked(url, blocked)


def _stealth_session(**options):
    """Scrapling's stealth browser session, on the installed Chrome if there is one."""
    try:
        from scrapling.fetchers import StealthySession
    except ImportError as exc:
        raise BrowserUnavailable(
            "Automatic Cloudflare solving needs Scrapling. Install with pip install 'webmirror[stealth]'."
        ) from exc
    errors = []
    for real_chrome in (True, False):
        session = StealthySession(real_chrome=real_chrome, **options)
        try:
            session.start()
            return session
        except Exception as exc:
            errors.append(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
            try:
                session.close()
            except Exception:
                pass
    raise BrowserUnavailable(
        "The stealth browser could not start. Install Google Chrome, or run "
        "'scrapling install' once to download its browser. " + "; ".join(errors[:2])
    )


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
        "id='challenge-running'",
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
    pages: Iterable[str] = (),
    follow: Callable[[str], bool] | None = None,
    max_pages: int = 1,
    blocked_domains: Iterable[str] | None = None,
    auto_solve: bool = False,
) -> list[tuple[str, str, bytes]]:
    """Observe anonymous GET responses and optionally invite the user to verify.

    When an interstitial is detected, a visible temporary browser opens and
    the user may solve the check manually. This capture never reuses the user's
    signed-in browser profile; closing the window cancels verification.

    After `url`, the same browser opens each of `pages`, then every link a
    loaded page shows that `follow` accepts, up to `max_pages` pages in all.
    Links a script adds after load are found this way too, and each page's own
    on-demand engines, models and fonts are observed as it runs.

    Requests to `blocked_domains` (and their subdomains) are refused, which keeps
    ads and trackers from loading. With `auto_solve`, a challenged capture is
    first retried in Scrapling's stealth browser, which solves Cloudflare checks
    by itself; manual verification, if allowed, is the fallback.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "Browser capture needs Playwright. Install with pip install 'webmirror[dynamic]'."
        ) from exc

    origin = (urlsplit(url).scheme.lower(), urlsplit(url).netloc.lower())
    blocked = frozenset(blocked_domains or ())
    captured: dict[str, tuple[str, str, bytes]] = {}
    total_bytes = 0
    failed: list[str] = []

    # Responses seen by the stealth browser, read once its page has settled:
    # Scrapling's patched Playwright can't read a body inside the event itself.
    pending: list = []

    def drain() -> None:
        while pending:
            on_response(pending.pop(0))

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

    def block_ads(route) -> None:
        try:
            if _host_is_blocked(route.request.url, blocked):
                route.abort()
            else:
                route.continue_()
        except Exception:
            pass

    def create_page(browser):
        context = browser.new_context(service_workers="block")
        if blocked:
            context.route("**/*", block_ads)
        page = context.new_page()
        page.on("response", on_response)
        page.on("requestfailed", lambda req: failed.append(req.url[:180]))
        return context, page

    def navigate(page, target=url):
        response = None
        try:
            response = page.goto(target, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1300)
        except Exception as exc:
            progress(f"Browser navigation incomplete: {str(exc)[:150]}")
        drain()
        return response

    def settle(page):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1100)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(600)
        except Exception as exc:
            progress(f"Browser preview incomplete: {str(exc)[:150]}")
        drain()

    def links(page) -> list[str]:
        try:
            found = page.eval_on_selector_all("a[href]", "els => els.map((e) => e.href)")
        except Exception:
            return []
        return [str(link).split("#", 1)[0] for link in found or [] if isinstance(link, str)]

    queue = [url] + [p.split("#", 1)[0] for p in pages]
    visited_pages: set[str] = {url}

    def enqueue(found: Iterable[str]) -> None:
        for link in found:
            if follow and link not in queue and link not in visited_pages and follow(link):
                queue.append(link)

    def visit_rest(page) -> None:
        """Open the queued pages after the first, in the same browser context."""
        enqueue(links(page))
        while queue and len(visited_pages) < max(1, max_pages):
            target = queue.pop(0)
            if target in visited_pages:
                continue
            visited_pages.add(target)
            progress(f"Browser opening {target}")
            response = navigate(page, target)
            if _page_is_challenged(page, response):
                progress(f"Skipped {target}: the website asked for verification again.")
                continue
            settle(page)
            enqueue(links(page))
        if queue:
            progress(f"Page limit reached; {len(queue)} linked pages were not opened in the browser.")

    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright, headless=True)
        try:
            context, page = create_page(browser)
            try:
                queue.pop(0)
                response = navigate(page)
                challenged = _page_is_challenged(page, response)
                if not challenged:
                    settle(page)
                    challenged = _page_is_challenged(page, response)
                if not challenged:
                    visit_rest(page)
            finally:
                context.close()
        finally:
            browser.close()

    if challenged and auto_solve:
        # Do not save response bodies belonging to the challenge itself.
        captured.clear()
        total_bytes = 0
        failed.clear()
        progress("Verification check detected; trying to solve it automatically "
                 "with Scrapling's stealth browser...")
        solved = False
        # If the stealth walk stops partway, the manual fallback starts the pages over.
        queue_before, visited_before = list(queue), set(visited_pages)

        def listen(page) -> None:
            # Before the first load, so the entry page's own files are seen too.
            page.on("response", lambda response: pending.append(response))
            page.on("requestfailed", lambda req: failed.append(req.url[:180]))

        def walk_solved(page) -> None:
            nonlocal solved
            if _page_is_challenged(page, None):
                pending.clear()
                return
            drain()
            settle(page)
            visit_rest(page)
            solved = True

        try:
            session = _stealth_session(
                headless=True, solve_cloudflare=True, google_search=False,
                blocked_domains=set(blocked) or None, timeout=60000,
            )
            try:
                session.fetch(url, page_setup=listen, page_action=walk_solved)
            finally:
                session.close()
        except BrowserUnavailable as exc:
            progress(str(exc))
        except Exception as exc:
            progress(f"Automatic verification failed: {str(exc)[:200]}")
        if solved:
            challenged = False
            progress("Verification check solved automatically; captured the website's resources.")
        else:
            captured.clear()
            total_bytes = 0
            queue[:] = queue_before
            visited_pages.clear()
            visited_pages.update(visited_before)
            progress("The check could not be solved automatically.")

    if challenged and allow_manual_verification:
        # A fresh Playwright: the stealth browser above can't share a running one.
        with sync_playwright() as playwright:
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
                        while True:
                            if page.is_closed():
                                raise VerificationIncomplete("The verification browser was closed.")
                            if not _page_is_challenged(page, None):
                                break
                            if monotonic() >= deadline:
                                raise VerificationIncomplete(
                                    "Verification was not completed within the allowed time."
                                )
                            page.wait_for_timeout(750)
                        progress("Verification completed; capturing visible website resources.")
                    if page.is_closed():
                        raise VerificationIncomplete("The verification browser was closed.")
                    settle(page)
                    if page.is_closed():
                        raise VerificationIncomplete("The verification browser was closed.")
                    if _page_is_challenged(page, None):
                        raise VerificationIncomplete(
                            "The website presented another verification check; capture was stopped."
                        )
                    visit_rest(page)
                finally:
                    try:
                        context.close()
                    except Exception:
                        # The user may have closed the visible browser themselves.
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    elif challenged:
        progress(
            "Site verification detected. Enable manual verification in the "
            "GUI to complete the check in a visible browser."
        )

    if failed:
        progress(f"Browser had {len(failed)} failed requests; some features may need the internet.")
    progress(f"Browser observed {len(captured)} same-origin assets ({total_bytes // 1024} KB).")
    return list(captured.values())
