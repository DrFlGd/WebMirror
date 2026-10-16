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

## Download a ready-to-run executable

**No Python installation is needed** to run the packaged app.

1. Open [GitHub Actions — Build desktop executables](https://github.com/DrFlGd/WebMirror/actions/workflows/build.yml).
2. Open the most recent **successful** run on `main`.
3. Under **Artifacts**, download the package for your operating system:
   - **WebMirror-Windows** — unzip and double-click `WebMirror.exe` (portable executable, no installer).
   - **WebMirror-macOS** — unzip `WebMirror-macOS.zip`, then open `WebMirror.app`.
   - **WebMirror-Linux** — extract `WebMirror-Linux.tar.gz` and run `./WebMirror`.

GitHub may require you to sign in to download workflow artifacts. These binaries are **unsigned**: Windows SmartScreen or macOS Gatekeeper may show a security warning. Only run files downloaded from the project's own Actions workflow and review the code if you have concerns. On macOS, use Finder's **Open** context-menu option if Gatekeeper blocks the unsigned app. On Linux, install desktop Tcl/Tk libraries if the app cannot launch.

These build artifacts expire after **90 days**. The build workflow also has a **Run workflow** button to create new downloads.

Each executable saves mirrors in your user directory (`~/.webmirror/mirrors`), **not** next to the executable, so replacing an older build keeps your saved websites.

### Build locally

On a computer with Python 3.10+ installed (and Tkinter available):

```bash
python -m pip install -e . "pyinstaller>=6.11,<7"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name WebMirror launcher.py
```

On Windows, the executable appears at `dist/WebMirror.exe`. On Linux it is `dist/WebMirror`. On macOS, GitHub Actions instead packages a `WebMirror.app` bundle with the `--onedir --windowed` flags.

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
