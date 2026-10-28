import sys, re, json
from playwright.sync_api import sync_playwright
url = sys.argv[1]
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page(); log = []
    page.on("response", lambda r: log.append((r.status, r.request.resource_type, r.url[:250], r.headers.get("content-type", ""))))
    page.goto(url, wait_until="networkidle", timeout=60000); page.wait_for_timeout(3000)
    print("TEXT", page.locator("body").inner_text()[:3000].replace("\n", " | "))
    links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.innerText.trim().slice(0,60), e.href, e.getAttribute('download')])")
    for l in links:
        if not re.match(r"https://miniskyline.com/[a-z-]*$", l[1]): print("LINK", l)
    buttons = page.eval_on_selector_all("button", "els => els.map(e => e.innerText.trim().slice(0,60))")
    print("BUTTONS", buttons[:40])
    for x in log:
        if x[1] in ("fetch", "xhr") or "json" in x[3] or re.search(r"\.(exe|msi|dmg|AppImage|gz|zip)\b", x[2]): print("REQ", x)
    for host in sorted({re.sub(r"^https?://([^/]+).*", r"\1", x[2]) for x in log}): print("HOST", host)
    # Click download-looking buttons and capture the download URL without saving.
    for label in ("Windows", "Download", "macOS", "Linux", "AppImage"):
        for el in page.get_by_text(label, exact=False).all()[:4]:
            try:
                with page.expect_download(timeout=4000) as d:
                    el.click(timeout=2000)
                print("DOWNLOAD", label, d.value.url, d.value.suggested_filename); d.value.cancel()
            except Exception as exc:
                pass
    print("REQS_AFTER", [x for x in log if re.search(r"\.(exe|msi|dmg|AppImage|gz|zip|json)\b", x[2])][:30])
    b.close()
