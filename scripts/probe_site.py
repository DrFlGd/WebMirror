import re, requests
H = {"User-Agent": "Mozilla/5.0"}
raw = requests.get("https://miniskyline.com/desktop", headers=H, timeout=20).text
for s in re.findall(r'src="([^"]+\.js)"', raw) + re.findall(r'href="([^"]+\.js)"', raw):
    js = requests.get("https://miniskyline.com" + s, headers=H, timeout=30).text
    print("SCRIPT", s, len(js))
    for m in re.finditer(r"setup\.exe|AppImage|mac-arm64|MiniSkylines-Desktop|downloads/desktop|desktopVersion|DESKTOP_VERSION", js):
        print("CTX", s[-30:], js[max(0, m.start()-300):m.end()+200].replace("\n", " "))
    imports = set(re.findall(r'["\'](\./[^"\']+\.js|/assets/[^"\']+\.js)["\']', js))
    print("IMPORTS", len(imports), sorted(imports)[:80])
    for imp in sorted(imports):
        path = imp if imp.startswith("/") else "/assets/" + imp[2:]
        sub = requests.get("https://miniskyline.com" + path, headers=H, timeout=30).text
        for m in re.finditer(r"setup\.exe|MiniSkylines-Desktop|downloads/desktop", sub):
            print("SUBCTX", path, sub[max(0, m.start()-400):m.end()+250].replace("\n", " "))
for path in ("/downloads/desktop/MiniSkylines-Desktop-1.511-setup.exe", "/version.json", "/api/desktop-version", "/desktop.json"):
    r = requests.head("https://miniskyline.com" + path, headers=H, timeout=20, allow_redirects=False)
    print("HEAD", path, r.status_code, {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-length", "location", "etag", "last-modified")})
