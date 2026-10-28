import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit
import requests
from bs4 import BeautifulSoup

BASE = "https://skipthecad.com/"
H = {"User-Agent": "Mozilla/5.0 (compatible; GeneratorLibraryCatalog/1.0)"}
s = requests.Session()
s.headers.update(H)

print("ROBOTS", s.get(BASE + "robots.txt", timeout=20).text[:1500].replace("\n", " | "))
urls = set()
todo = [BASE + "sitemap.xml", BASE + "sitemap-index.xml", BASE + "sitemap_index.xml"]
seen = set()
while todo:
    u = todo.pop()
    if u in seen:
        continue
    seen.add(u)
    r = s.get(u, timeout=30)
    print("SITEMAP", u, r.status_code, len(r.text))
    if r.status_code != 200:
        continue
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text):
        if loc.endswith(".xml"):
            todo.append(loc)
        elif "/generators/" in loc:
            urls.add(loc)
print("GENERATOR_PAGES", len(urls))
if not urls:
    page = s.get(BASE + "generators/", timeout=30).text
    for a in BeautifulSoup(page, "html.parser").select("a[href*='/generators/']"):
        urls.add(urljoin(BASE, a["href"]))
    print("FROM_INDEX", len(urls))


def grab(u):
    for attempt in range(3):
        try:
            r = s.get(u, timeout=30)
            break
        except Exception as e:
            time.sleep(2)
    else:
        return {"url": u, "error": "fetch"}
    soup = BeautifulSoup(r.text, "html.parser")
    out = {"url": u, "status": r.status_code}
    out["title"] = soup.title.string.strip() if soup.title and soup.title.string else ""
    h1 = soup.find("h1")
    out["h1"] = h1.get_text(" ", strip=True) if h1 else ""
    m = soup.find("meta", attrs={"name": "description"})
    out["desc"] = m["content"] if m and m.get("content") else ""
    out["ld"] = []
    for t in soup.find_all("script", type="application/ld+json"):
        try:
            out["ld"].append(json.loads(t.string or ""))
        except Exception:
            pass
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(u, a["href"])
        host = urlsplit(href).netloc
        if host and "skipthecad.com" not in host:
            links.append([a.get_text(" ", strip=True)[:60], href])
    out["links"] = links
    pairs = []
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.append([dt.get_text(" ", strip=True)[:40], dd.get_text(" ", strip=True)[:200]])
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True)[:200] for c in tr.find_all(["th", "td"])]
        if len(cells) == 2:
            pairs.append(cells)
    out["pairs"] = pairs
    crumbs = [a.get_text(" ", strip=True) for a in soup.select("nav a, .breadcrumb a")][:12]
    out["crumbs"] = crumbs
    return out


with ThreadPoolExecutor(4) as pool:
    for rec in pool.map(grab, sorted(urls)):
        print("REC " + json.dumps(rec, ensure_ascii=False))
