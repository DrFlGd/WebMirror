import base64, json, os, sys
import requests

T = os.environ["GH_TOKEN"]
s = requests.Session()
s.headers.update({"Authorization": f"Bearer {T}", "Accept": "application/vnd.github+json"})


def get(path, **kw):
    r = s.get("https://api.github.com/" + path, timeout=30, **kw)
    return r.json() if r.status_code == 200 else None


def content(repo, path, ref):
    d = get(f"repos/{repo}/contents/{path}", params={"ref": ref})
    if isinstance(d, dict) and d.get("content"):
        return base64.b64decode(d["content"]).decode("utf-8", "replace")
    return None


for name in open("scripts/repos.txt").read().split():
    meta = get(f"repos/{name}")
    if not meta:
        print("REPO " + json.dumps({"name": name, "missing": True}))
        continue
    full = meta["full_name"]
    ref = meta["default_branch"]
    out = {"name": name, "full": full, "archived": meta["archived"], "fork": meta["fork"],
           "license": (meta.get("license") or {}).get("spdx_id"), "homepage": meta.get("homepage"),
           "desc": meta.get("description"), "stars": meta["stargazers_count"], "pushed": meta["pushed_at"],
           "lang": meta.get("language"), "pages": meta.get("has_pages"), "branch": ref, "topics": meta.get("topics", [])}
    rel = get(f"repos/{full}/releases", params={"per_page": 5}) or []
    out["releases"] = [{"tag": r["tag_name"], "pre": r["prerelease"], "assets": [a["name"] for a in r["assets"]][:20]} for r in rel]
    tree = get(f"repos/{full}/git/trees/{ref}", params={"recursive": "1"})
    paths = [t["path"] for t in (tree or {}).get("tree", []) if t["type"] == "blob"]
    out["nfiles"] = len(paths)
    out["truncated"] = (tree or {}).get("truncated")
    out["top"] = sorted({p.split("/")[0] + ("/" if "/" in p else "") for p in paths})[:60]
    out["index_html"] = [p for p in paths if p.endswith("index.html") and p.count("/") <= 2 and "node_modules" not in p][:10]
    out["pkg_paths"] = [p for p in paths if p.endswith("package.json") and "node_modules" not in p and p.count("/") <= 2][:8]
    out["workflows"] = [p.split("/")[-1] for p in paths if p.startswith(".github/workflows/")]
    out["scad"] = sum(1 for p in paths if p.endswith(".scad"))
    out["py"] = sum(1 for p in paths if p.endswith(".py"))
    out["generator_json"] = [p for p in paths if p.endswith(("generator.json", "generator-library.json"))]
    pk = {}
    for p in out["pkg_paths"][:3]:
        txt = content(full, p, ref)
        try:
            j = json.loads(txt or "")
        except Exception:
            continue
        deps = {**j.get("dependencies", {}), **j.get("devDependencies", {})}
        pk[p] = {"scripts": {k: v[:120] for k, v in (j.get("scripts") or {}).items() if k in ("build", "prebuild", "postbuild", "generate", "export")},
                 "tools": sorted(d for d in deps if d in ("vite", "next", "react-scripts", "@sveltejs/kit", "webpack", "parcel", "astro", "nuxt", "@angular/cli", "esbuild", "rollup", "typescript", "electron", "@tauri-apps/cli", "vue", "react", "svelte", "three", "manifold-3d", "openscad-wasm", "@jscad/modeling", "replicad", "opencascade.js")),
                 "pm": j.get("packageManager"), "workspaces": bool(j.get("workspaces"))}
    out["pkg"] = pk
    out["locks"] = [p for p in paths if p.split("/")[-1] in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock") and p.count("/") <= 2]
    deploy = []
    for w in out["workflows"][:6]:
        txt = content(full, ".github/workflows/" + w, ref) or ""
        if "pages" in txt.lower():
            deploy.append({"file": w, "runs": [l.strip()[:120] for l in txt.splitlines() if l.strip().startswith(("run:", "- run:", "path:", "working-directory:"))][:12]})
    out["deploy"] = deploy
    print("REPO " + json.dumps(out))
