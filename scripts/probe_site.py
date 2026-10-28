import re, requests, base64, gzip, json
from urllib.parse import urljoin
UA = {"User-Agent": "Mozilla/5.0"}
for path in ["/", "/generators/case/", "/robots.txt", "/terms/", "/privacy/", "/license/", "/about/"]:
    r = requests.get("https://alpaco.it" + path, headers=UA, timeout=30)
    print("PAGE", path, r.status_code, r.headers.get("content-type"), len(r.content))
    if path in ("/robots.txt",):
        print(r.text[:1500])
    if path in ("/generators/case/",):
        html = r.text
        print("HTMLGZ", base64.b64encode(gzip.compress(html.encode())).decode())
        srcs = re.findall(r'(?:src|href)="([^"]+)"', html)
        print("REFS", json.dumps(srcs))
        for s in srcs:
            if re.search(r"\.m?js(\?|$)", s):
                u = urljoin(r.url, s)
                t = requests.get(u, headers=UA, timeout=30).text
                hits = sorted(set(re.findall(r"[\w./@:-]*(?:wasm|openscad|manifold|worker|cdn|unpkg|jsdelivr|esm\.sh|import\()[\w./@:?=-]*", t, re.I)))[:80]
                print("JS", u, len(t), json.dumps(hits))
    if path in ("/terms/", "/privacy/", "/license/", "/about/") and r.status_code == 200:
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        for kw in ["copy", "licen", "reproduc", "mirror", "scrap", "download", "personal use", "commercial"]:
            for m in re.finditer(kw, text, re.I):
                print("TERMS", path, kw, "|", text[max(0, m.start()-150): m.start()+200])
                break
