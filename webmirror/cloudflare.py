"""Optional automatic solving of Cloudflare checks during browser capture.

Ported from Scrapling (https://github.com/D4Vinci/Scrapling): the challenge
detection and Turnstile clicking of `StealthySession._cloudflare_solver` and
`_detect_cloudflare` (scrapling/engines/_browsers/_stealth.py and _base.py),
and the launch flags of scrapling/engines/constants.py. Scrapling is
distributed under this license:

    BSD 3-Clause License

    Copyright (c) 2024, Karim shoair

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice, this
       list of conditions and the following disclaimer.

    2. Redistributions in binary form must reproduce the above copyright notice,
       this list of conditions and the following disclaimer in the documentation
       and/or other materials provided with the distribution.

    3. Neither the name of the copyright holder nor the names of its
       contributors may be used to endorse or promote products derived from
       this software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
    AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
    DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
    FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
    DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
    SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
    CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
    OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
    OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

What makes the solving work is the browser more than this file: patchright, the
Playwright fork Scrapling drives, removes the automation traces Cloudflare looks
for. WebMirror's browser capture uses patchright when it is installed.
"""
from __future__ import annotations

from random import randint
import re
from time import monotonic
from typing import Callable

CHALLENGE_FRAME = re.compile(r"^https?://challenges\.cloudflare\.com/cdn-cgi/challenge-platform/.*")
TURNSTILE_SCRIPT = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*["'][^"']*challenges\.cloudflare\.com/turnstile/v""", re.I
)
MAX_ATTEMPTS = 3

# Chrome flags that make an automated browser look and behave like a person's.
STEALTH_ARGS = (
    "--no-pings", "--no-first-run", "--disable-infobars", "--disable-breakpad",
    "--no-service-autorun", "--homepage=about:blank", "--password-store=basic",
    "--disable-hang-monitor", "--no-default-browser-check",
    "--disable-session-crashed-bubble", "--disable-search-engine-choice-screen",
    "--test-type", "--mute-audio", "--disable-sync", "--hide-scrollbars",
    "--disable-logging", "--start-maximized", "--enable-async-dns",
    "--use-mock-keychain", "--do-not-de-elevate", "--disable-translate",
    "--disable-voice-input", "--window-position=0,0", "--disable-wake-on-wifi",
    "--ignore-gpu-blocklist", "--enable-tcp-fast-open", "--enable-web-bluetooth",
    "--disable-cloud-import", "--disable-print-preview", "--disable-dev-shm-usage",
    "--metrics-recording-only", "--disable-crash-reporter", "--disable-partial-raster",
    "--disable-gesture-typing", "--disable-checker-imaging",
    "--disable-prompt-on-repost", "--force-color-profile=srgb",
    "--font-render-hinting=none", "--aggressive-cache-discard",
    "--disable-cookie-encryption", "--disable-domain-reliability",
    "--disable-threaded-animation", "--disable-threaded-scrolling",
    "--enable-simple-cache-backend", "--disable-background-networking",
    "--enable-surface-synchronization", "--disable-image-animation-resync",
    "--disable-renderer-backgrounding", "--disable-ipc-flooding-protection",
    "--prerender-from-omnibox=disabled", "--safebrowsing-disable-auto-update",
    "--disable-offer-upload-credit-cards", "--disable-background-timer-throttling",
    "--disable-new-content-rendering-timeout", "--run-all-compositor-stages-before-draw",
    "--disable-client-side-phishing-detection", "--disable-backgrounding-occluded-windows",
    "--disable-layer-tree-host-memory-pressure", "--autoplay-policy=user-gesture-required",
    "--disable-offer-store-unmasked-wallet-cards",
    "--disable-blink-features=AutomationControlled",
    "--disable-component-extensions-with-background-pages",
    "--enable-features=NetworkService,NetworkServiceInProcess,TrustTokens,TrustTokensAlwaysAllowIssuance",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    "--disable-features=AudioServiceOutOfProcess,TranslateUI,BlinkGenPropertyTrees",
)
# Playwright's own defaults that give automation away.
IGNORED_DEFAULT_ARGS = (
    "--enable-automation", "--disable-popup-blocking", "--disable-component-update",
    "--disable-default-apps", "--disable-extensions",
)


def detect_challenge(html: str) -> str | None:
    """The kind of Cloudflare check on a page: "non-interactive", "managed",
    "interactive", "embedded" (a Turnstile widget inside a normal page), or None."""
    for kind in ("non-interactive", "managed", "interactive"):
        if f"cType: '{kind}'" in html:
            return kind
    if TURNSTILE_SCRIPT.search(html):
        return "embedded"
    return None


def challenge_cleared(html: str, kind: str) -> bool:
    return kind == "embedded" or detect_challenge(html) is None


def _content(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""


def _settle(target, network_idle: bool = False, timeout: int | None = None) -> None:
    try:
        target.wait_for_load_state("load")
        target.wait_for_load_state("domcontentloaded")
        if network_idle:
            target.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def solve(page, log: Callable[[str], None] = lambda message: None, _attempt: int = 0) -> bool:
    """Pass the Cloudflare check on `page`, if there is one. True when none is left."""
    if _attempt:
        _settle(page, network_idle=True, timeout=5000)
    deadline = monotonic() + 5
    kind = None
    for poll in range(51):
        if not poll % 5 or monotonic() >= deadline:
            kind = detect_challenge(_content(page))
        if kind and (kind != "embedded" or page.frame(url=CHALLENGE_FRAME) is not None):
            break
        remaining = deadline - monotonic()
        if _attempt or poll == 50 or remaining <= 0:
            kind = None
            break
        page.wait_for_timeout(min(100, remaining * 1000))
    if not kind:
        return detect_challenge(_content(page)) in (None, "embedded")
    if _attempt >= MAX_ATTEMPTS:
        log(f"Cloudflare check still showing after {_attempt} attempts.")
        return False
    log(f'Cloudflare check found ("{kind}"); solving it.')

    if kind == "embedded":
        frame = page.frame(url=CHALLENGE_FRAME)
        if frame is None:
            return True
        _settle(frame)
        try:
            frame.locator('input[type="checkbox"]').click(timeout=10000, delay=randint(100, 200))
        except Exception:
            return True
        _settle(page, network_idle=True, timeout=5000)
        return True

    if kind == "non-interactive":
        for _ in range(10):
            if detect_challenge(_content(page)) != "non-interactive":
                break
            page.wait_for_timeout(1000)
            try:
                page.wait_for_load_state()
            except Exception:
                pass
        if challenge_cleared(_content(page), kind):
            return True
        return solve(page, log, _attempt + 1)

    # A managed or interactive page: wait for the widget, then click its box.
    for _ in range(20):
        if page.frame(url=CHALLENGE_FRAME) is not None or challenge_cleared(_content(page), kind):
            break
        page.wait_for_timeout(500)
    box = None
    frame = page.frame(url=CHALLENGE_FRAME)
    if frame is not None:
        _settle(frame)
        for _ in range(20):
            if frame.frame_element().is_visible():
                break
            page.wait_for_timeout(500)
        else:
            return solve(page, log, _attempt + 1)
        box = frame.frame_element().bounding_box()
    if not box:
        if challenge_cleared(_content(page), kind):
            return True
        elements = page.locator(".main-content p+div>div>div").last.element_handles()
        if elements:
            box = elements[0].bounding_box()
            elements[0].dispose()
        if not box:
            page.wait_for_timeout(1000)
            return solve(page, log, _attempt + 1)
    page.mouse.click(box["x"] + randint(26, 28), box["y"] + randint(25, 27),
                     delay=randint(100, 200), button="left")
    _settle(page, network_idle=True)
    for _ in range(100):
        if challenge_cleared(_content(page), kind):
            break
        page.wait_for_timeout(100)
    _settle(page)
    if challenge_cleared(_content(page), kind):
        return True
    return solve(page, log, _attempt + 1)
