"""Optional real-site check, run on CI; never captures signed-in user data."""
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from webmirror.core import MirrorEngine, MirrorLibrary, MirrorServer


def main():
    url = "https://fitforge-c5058.web.app/drawer"
    with TemporaryDirectory() as directory:
        library = MirrorLibrary(Path(directory))
        try:
            engine = MirrorEngine(
                library, progress=lambda text: print(text[:250], flush=True),
                max_files=180, max_bytes=15 * 1024 * 1024,
                browser_capture=True,
            )
            record = engine.mirror(url)
            print("MIRROR", record, flush=True)
            host = MirrorServer(library)
            try:
                import socket
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                local = host.start(record, port)
                response = requests.get(local, timeout=10, allow_redirects=False)
                print("LOCAL", response.status_code, response.url,
                      "HTML", len(response.content), flush=True)
                print("APP_JS", [
                    path.name for path in library.path(record).glob("assets/*.js")
                ][:12], flush=True)
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel="chrome", headless=True)
                    try:
                        context = browser.new_context(service_workers="block")
                        page = context.new_page()
                        page_errors = []
                        misses = []
                        page.on("pageerror", lambda err: page_errors.append(str(err)[:350]))
                        page.on("response", lambda r: misses.append(
                            (r.status, r.url)) if r.status >= 400
                            and r.url.startswith(f"http://127.0.0.1:{port}") else None)
                        # Simulate no external internet but keep loopback available.
                        page.route("**/*", lambda route: (
                            route.continue_() if route.request.url.startswith(
                                f"http://127.0.0.1:{port}/"
                            ) else route.abort()
                        ))
                        page.goto(local, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000)
                        print("OFFLINE_RENDER", "title=", page.title(),
                              "visible_chars=", len(page.locator("body").inner_text()),
                              "page_errors=", page_errors[:8],
                              "local_404=", misses[:8], flush=True)
                        Path("artifacts").mkdir(exist_ok=True)
                        page.screenshot(path="artifacts/fitforge-offline.png", full_page=True)
                        context.close()
                    finally:
                        browser.close()
            finally:
                host.stop()
        except Exception as exc:
            # Availability and anti-bot protection are outside CI's control;
            # this is a diagnostic check, not a merge gate.
            print("SMOKE CHECK INCOMPLETE:", type(exc).__name__, str(exc)[:500], flush=True)


if __name__ == "__main__":
    main()
