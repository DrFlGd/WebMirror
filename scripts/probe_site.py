import requests, base64, gzip
for slug in ["koolm-screw-generator", "skapa-skadis-generator", "meshy", "perplexing-labs-gridfinity-generator"]:
    t = requests.get(f"https://skipthecad.com/generators/{slug}/", headers={"User-Agent": "Mozilla/5.0"}, timeout=30).text
    print("PAGE", slug, base64.b64encode(gzip.compress(t.encode())).decode())
print("SITEMAP", base64.b64encode(gzip.compress(requests.get("https://skipthecad.com/sitemap-0.xml", timeout=30).text.encode())).decode())
