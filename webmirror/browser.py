"""Best-effort capture of JavaScript-triggered GET assets in a real Chromium browser.

A static HTML crawler cannot see files loaded by bundler runtimes, lazy routes,
WebAssembly loaders, or dynamically constructed URLs. Playwright observes the
public, anonymous page load and returns small successful same-origin GET results
to the archiver. It does not bypass authentication or capture private sessions.
"""
from __future__ import annotations

from urllib.parse import urlsplit
from typing import Callable

# Do not copy logged-in responses or API secrets from the user's browser:
# each launch creates an isolated, empty browser context.
CAPTURE_LIMIT = 160 * 1024 * 1024
ALLOWED_CONTENT_TYPES = (
    "text/html", "text/css", "text/javascript", "application/javascript",
    "application/x-javascript", "application/json", "text/plain",
    "application/wasm", "application/manifest+json", "image/",
    "font/", "application/font-", "application/octet-stream",
    "text/xml", "application/xml",
)


class BrowserUnavailable(RuntimeError):
    """No usable Chromium browser or Playwright runtime was found."""


def capture_public_assets(
    url: str, *, max_files: int, max_bytes: int,
    progress: Callable[[str], None],
) -> list[tuple[str, str, bytes]]:
    """Observe public GET responses on initial load and a short scroll.

    Returns (absolute URL, MIME, body). Total memory consumption is bounded.
    Rendering is opt-in, does not sign into any service, and uses a clean context.
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
        total_bytes += len(body)
        captured[target] = (target, mime, body)

    with sync_playwright() as p:
        browser = None
        errors = []
        # Prefer the Chrome/Edge already installed by the user. No browser
        # executable is silently downloaded or installed.
        for channel in ("chrome", "msedge", None):
            try:
                options = {"headless": True}
                if channel:
                    options["channel"] = channel
                browser = p.chromium.launch(**options)
                break
            except Exception as exc:
                errors.append(str(exc).splitlines()[0])
        if browser is None:
            raise BrowserUnavailable(
                "No supported Chromium browser found. Install Google Chrome or Microsoft Edge "
                "to enable dynamic asset capture. " + "; ".join(errors[:2])
            )
        try:
            context = browser.new_context(service_workers="block")
            page = context.new_page()
            page.on("response", on_response)
            page.on("requestfailed", lambda req: failed.append(req.url[:180]))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1300)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(700)
            except Exception as exc:
                progress(f"Browser preview was incomplete: {str(exc)[:150]}")
            if failed:
                progress(f"Browser had {len(failed)} failed requests; some features may need the internet.")
            context.close()
        finally:
            browser.close()
    progress(f"Browser observed {len(captured)} same-origin assets ({total_bytes // 1024} KB).")
    return list(captured.values())
