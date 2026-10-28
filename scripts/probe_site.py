import json, sys, tempfile, collections
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
from webmirror.core import MirrorEngine, MirrorLibrary, MirrorServer

URL = sys.argv[1]
ACTIONS = sys.argv[2:]


def visit(page_url, label):
    hosts = collections.Counter()
    failed, errors, reqs = [], [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.on("console", lambda m: errors.append(f"{m.type}: {m.text}"[:400]) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: errors.append(f"PAGEERROR {e}"[:600]))
        pg.on("requestfailed", lambda r: failed.append(f"{r.method} {r.url[:200]} {r.failure}"))

        def resp(r):
            hosts[urlsplit(r.url).netloc] += 1
            size = r.headers.get("content-length", "?")
            reqs.append(f"{r.status} {r.request.method} {r.request.resource_type} {size} {r.headers.get('content-type', '')[:40]} {r.url[:220]}")

        pg.on("response", resp)
        try:
            pg.goto(page_url, wait_until="networkidle", timeout=90000)
        except Exception as e:
            print("GOTO", e)
        pg.wait_for_timeout(8000)
        for sel in ACTIONS:
            try:
                pg.locator(sel).first.click(timeout=5000)
                print("CLICKED", sel)
                pg.wait_for_timeout(15000)
            except Exception as e:
                print("NOCLICK", sel, str(e)[:120])
        text = pg.inner_text("body")[:2500].replace("\n", " | ")
        for a in pg.eval_on_selector_all("a[href]", "els => els.map(e => e.textContent.trim().slice(0,40) + ' => ' + e.href)"):
            print("LINK", a[:250])
        b.close()
    print(f"===== {label} {page_url}")
    print("HOSTS", dict(hosts))
    for r in reqs:
        print("REQ", r)
    for f in failed:
        print("FAILED", f)
    for e in errors:
        print("CONSOLE", e)
    print("TEXT", text)


visit(URL, "LIVE")
home = Path(tempfile.mkdtemp())
lib = MirrorLibrary(home)
eng = MirrorEngine(lib, progress=lambda m: print("PROGRESS", m[:200]), browser_capture=True)
rec = eng.mirror(URL)
folder = lib.path(rec)
print("RECORD", json.dumps(rec.__dict__))
files = sorted(str(x.relative_to(folder)) for x in folder.rglob("*") if x.is_file())
print("FILES", len(files))
for f in files:
    print("FILE", f, (folder / f).stat().st_size)
routes = folder / ".webmirror-routes.json"
if routes.is_file():
    for k, v in list(json.loads(routes.read_text("utf-8")).items())[:200]:
        print("ROUTE", k[:200], "->", v)
srv = MirrorServer(lib)
local = srv.start(rec, port=8765)
visit(local, "MIRROR")
srv.stop()
