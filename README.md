# WebMirror

A small Python desktop app for saving public, mostly static websites and serving them locally.

## Features

- Paste a website URL and click **Mirror website**.
- Store each snapshot in your personal mirror library, with its source URL and date.
- Select an existing mirror, click **Host selected**, and open it in your browser.
- Stop the local server, refresh the library, or delete old snapshots.
- Download same-site pages, styles, images, fonts, scripts, and referenced cross-origin assets; rewrite links for local hosting.
- Preserve URL subpaths (for example, `/terrain-press/` on GitHub Pages).
- Optional **GitHub Pages: include all published repository files** mode to capture data files with dynamically computed names (may consume hundreds of MB).
- Host only on `127.0.0.1`, not on your network.

## Requirements

- Python **3.10+**
- Tkinter (bundled with most Python installations; on Debian/Ubuntu install `python3-tk` if needed)
- Internet access when creating mirrors, but not when viewing a successfully mirrored static site

## Quick start

```bash
git clone https://github.com/DrFlGd/WebMirror.git
cd WebMirror
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m webmirror
```

Alternatively launch with the installed `webmirror` command.

In the app, enter `https://ianbauters.github.io/terrain-press/` (or another publicly accessible website), click **Mirror website**, select it from the library, and click **Host selected**. Click **Open in browser** to visit its local address. For the Terrain Press example, tick **GitHub Pages: include all published repository files** before mirroring to also download the project's elevation tiles (a potentially large download). Port `8000` is the default; change it if that port is already in use. Mirrored files live in `~/.webmirror/mirrors/` by default (override with the `WEBMIRROR_HOME` environment variable).

## Important limitations

This is a **best-effort static crawler**, not a browser automation recorder or full replacement for a live backend. It follows same-site HTML pages under the entered base path and downloads linked assets, including CSS imports, static JavaScript imports, and literal `fetch()` / `new URL()` references. Content assembled at runtime, JavaScript-computed resource URLs, streaming map tiles, API requests, authenticated services, server-side features, and external embeds might **not** work offline. For public GitHub Pages project sites, the optional full published-files mode copies files committed under a `site/`, `docs/`, or root publication directory from the corresponding GitHub repository, which helps with generated asset URLs (including Terrain Press elevation tiles). It cannot recover files created only during deployment or replace remote APIs. Verify critical functionality while disconnected before relying on a snapshot.

The app downloads cross-origin **assets referenced by the site**, but does not crawl links to other sites. Use it only for content you have permission to copy; respect licenses, site policies, and server load. It is intentionally limited to 1,500 resources and 30 MB per file to prevent unbounded downloads. Full GitHub Pages mode needs access to GitHub's public API and raw file hosting and may hit rate limits. Downloads do not bypass login requirements or DRM.

## Development

```bash
python -m pip install -e '.[test]'
pytest -q
```

See `webmirror/core.py` for the mirroring engine, `webmirror/gui.py` for the GUI, and `tests/` for the tests.
