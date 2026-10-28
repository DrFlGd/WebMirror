import re, requests
from playwright.sync_api import sync_playwright
H = {"User-Agent": "Mozilla/5.0"}
raw = requests.get("https://miniskyline.com/desktop", headers=H, timeout=20)
print("RAW", raw.status_code, len(raw.text), sorted(set(re.findall(r"downloads/desktop/[^\"'\s<>]+", raw.text)))[:20])
scripts = re.findall(r'src="([^"]+\.js)"', raw.text)
print("SCRIPTS", scripts[:20])
for s in scripts:
    js = requests.get(requests.compat.urljoin("https://miniskyline.com/desktop", s), headers=H, timeout=20).text
    for m in set(re.findall(r"[^\"'`\s]{0,80}downloads/desktop[^\"'`\s]{0,120}|[^\"'`\s]{0,60}latest[^\"'`\s]{0,60}\.json", js)):
        print("JS_REF", s[-40:], m)
for path in ("/downloads/desktop/", "/downloads/desktop/latest.json", "/downloads/desktop/latest", "/api/desktop/latest", "/api/desktop", "/api/desktop/version", "/downloads/desktop/version.json", "/desktop/latest.json", "/api/version"):
    r = requests.get("https://miniskyline.com" + path, headers=H, timeout=20, allow_redirects=False)
    print("TRY", path, r.status_code, r.headers.get("content-type"), r.text[:300].replace("\n", " "))
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page()
    page.goto("https://miniskyline.com/desktop", wait_until="networkidle", timeout=60000)
    for tab in ("Windows", "Mac", "Linux"):
        page.get_by_role("button", name=tab, exact=True).click(); page.wait_for_timeout(800)
        links = page.eval_on_selector_all("a[href*='downloads']", "els => els.map(e => [e.innerText.trim().slice(0,50), e.href])")
        print("TAB", tab, links)
        print("TABTEXT", tab, re.findall(r"Version[^|]{0,120}", page.locator("body").inner_text().replace("\n", " | "))[:2])
    b.close()
