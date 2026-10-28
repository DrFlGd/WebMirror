import re, requests
from urllib.parse import urljoin, urlparse
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
base = "https://www.generatestl.com/"
seen = {}
for u in [base, base + "gear-generator/", base + "sitemap.xml", base + "robots.txt"]:
    r = requests.get(u, headers=UA, timeout=30)
    seen[u] = r.text
    print("GET", u, r.status_code, len(r.text), r.headers.get("content-type"), r.headers.get("server"), r.headers.get("x-powered-by"))
print("SITEMAP", re.findall(r"<loc>([^<]+)</loc>", seen[base + "sitemap.xml"])[:80])
print("ROBOTS", seen[base + "robots.txt"][:600])
for page in [base, base + "gear-generator/"]:
    html = seen[page]
    links = sorted({urljoin(page, h) for h in re.findall(r'href="([^"#]+)"', html) if urlparse(urljoin(page, h)).netloc.endswith("generatestl.com")})
    print("LINKS", page, links[:80])
    scripts = sorted({urljoin(page, s) for s in re.findall(r'<script[^>]+src="([^"]+)"', html)})
    print("SCRIPTS", page, scripts)
    for s in scripts:
        try:
            js = requests.get(s, headers=UA, timeout=30).text
        except Exception as e:
            print("ERR", s, e); continue
        hits = set(re.findall(r'["\'`](https?://[^"\'`\s]{3,80})["\'`]', js)) | set(re.findall(r'["\'`](/api/[\w/-]+)', js))
        if hits: print("JS", s, len(js), sorted(hits)[:40])
print("FRAMEWORK", "__NEXT_DATA__" in seen[base], "_next/" in seen[base], "astro" in seen[base].lower(), "svelte" in seen[base].lower())
