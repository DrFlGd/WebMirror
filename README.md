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
- **Dynamic browser capture** (enabled by default in the GUI) observes assets fetched by JavaScript in a clean Chrome/Edge browser session. It can recover lazy bundles, images, fonts, WASM and public initial GET data. The browser opens the entry page and then every linked page under the same path (up to 40), including links a script adds, so each tool page on a hub keeps the engine it loads when opened. A page link that could not be saved opens the live page instead.
- **Human verification handoff:** If a robot-check interstitial appears, WebMirror can open a visible browser so you complete the check yourself and then continue capturing eligible public assets (subject to website restrictions).
- **Browser-like requests** (on by default in the GUI): the static crawl's requests look like Chrome's, down to the TLS handshake, so sites that refuse Python scripts with a 403 still serve their pages. Needs `curl_cffi` (included in the downloads; `pip install -e ".[browserlike]"` otherwise).
- **Ad and tracker blocking** (on by default in the GUI): about 3,500 known ad and tracker domains are never fetched, in the crawl or in the browser, so captures skip analytics junk. Their links are left pointing online.
- **Polite crawling:** **Slow down when a site is busy** (on by default) waits between requests about as long as the site takes to answer, and when it answers 429 or 503 doubles the wait (or waits what its `Retry-After` asks) and retries up to three times. **Obey robots.txt** (off by default) skips what a site's robots.txt disallows, leaves disallowed pages linking to the live site, and honours `Crawl-delay`.
- **Pause and resume:** **Pause** stops a mirror after the current page and keeps what it saved. Mirror the same address again and choose **Yes** to continue where it stopped, or **No** to start over.
- **Only re-download files that changed** (on by default): when a site was mirrored before, WebMirror asks the server whether each saved image, font, model or other binary file changed (`If-None-Match` / `If-Modified-Since`) and reuses the saved copy when it didn't. Pages, styles and scripts are always fetched again.
- **Filters:** HTTrack-style rules such as `+*.glb -*/forum/*`. `*` matches anything and the last rule that matches a URL decides. `-` leaves matching files and pages out (left-out pages link to the live site); `+` also lets the crawl follow same-site pages outside the starting folder.
- **Automatic Cloudflare solving** (optional, off by default): if browser capture meets a Cloudflare check, WebMirror retries it in [Scrapling](https://github.com/D4Vinci/Scrapling)'s stealth browser, which solves Turnstile and interstitial checks by itself. If that fails and manual verification is on, the visible-browser handoff below is the fallback. See [Automatic Cloudflare solving](#automatic-cloudflare-solving).
- Original SPA routes (such as `/drawer` and `/app`) and query-string asset URLs work when hosting offline. Nested client-side routes fall back to the saved app shell.
- Host only on `127.0.0.1`, not on your network.

## Download a ready-to-run executable

**No Python installation is needed** to run the packaged app.

1. Open [GitHub Actions — Build desktop executables](https://github.com/DrFlGd/WebMirror/actions/workflows/build.yml).
2. Open the most recent **successful** run on `main`.
3. Under **Artifacts**, download the package for your operating system:
   - **WebMirror-Windows** — unzip and double-click `WebMirror.exe` (portable executable, no installer).
   - **WebMirror-macOS** — unzip `WebMirror-macOS.zip`, then open `WebMirror.app`.
   - **WebMirror-Linux** — extract `WebMirror-Linux.tar.gz` and run `./WebMirror`.

Dynamic capture needs a locally installed **Google Chrome or Microsoft Edge**; otherwise the app logs a warning and still creates a static mirror. No browser is silently downloaded. GitHub may require you to sign in to download workflow artifacts. These binaries are **unsigned**: Windows SmartScreen or macOS Gatekeeper may show a security warning. Only run files downloaded from the project's own Actions workflow and review the code if you have concerns. On macOS, use Finder's **Open** context-menu option if Gatekeeper blocks the unsigned app. On Linux, install desktop Tcl/Tk libraries if the app cannot launch.

These build artifacts expire after **90 days**. The build workflow also has a **Run workflow** button to create new downloads.

Each executable saves mirrors in your user directory (`~/.webmirror/mirrors`), **not** next to the executable, so replacing an older build keeps your saved websites.

### Build locally

On a computer with Python 3.10+ installed (and Tkinter available):

```bash
python -m pip install -e ".[dynamic,browserlike,stealth]" "pyinstaller>=6.11,<7"
python -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all playwright --collect-all patchright --collect-all curl_cffi --collect-all scrapling --collect-all browserforge --collect-all apify_fingerprint_datapoints --name WebMirror launcher.py
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
python -m pip install -e ".[dynamic,browserlike]"   # add ,stealth for automatic Cloudflare solving
python -m webmirror
```

Alternatively launch with the installed `webmirror` command.

In the app, enter `https://ianbauters.github.io/terrain-press/` (or another publicly accessible website), click **Mirror website**, select it from the library, and click **Host selected**. Click **Open in browser** to visit its local address. For the Terrain Press example, tick **GitHub Pages: include all published repository files** before mirroring to also download the project's elevation tiles (a potentially large download). Port `8000` is the default; change it if that port is already in use. Mirrored files live in `~/.webmirror/mirrors/` by default (override with the `WEBMIRROR_HOME` environment variable).

## Windows long-path handling

Websites that use long opaque JavaScript or image URLs (including certain ad and analytics resources on \`https://3dp.rocks/lithophane/\`) previously could fail with a Windows path such as \`C:\Users\...\mirrors\.webmirror-...\<very-long-token>.js\`. WebMirror now shortens the **saved local filename** to a deterministic hash, while retaining the original URL in its local server's route map. Deep folder structures also use short hashed local paths. No change to Windows' registry or LongPathsEnabled setting is required.

If an older WebMirror build displayed a long filename error, install a fresh executable and **mirror the website again**. Partial temporary snapshots will not appear in the library; existing completed mirrors remain available.

## Human-verification checks

Enable both checkboxes under **Mirror a website**:

1. **Capture JavaScript-loaded assets with Chrome or Edge.**
2. **Let me complete human-verification checks in an opened browser.**

If the site presents a recognized challenge (for example a Cloudflare “Just a moment…” page), WebMirror opens a temporary, visible Chrome or Edge window and displays a notice in its GUI. **You** complete the check using the browser window. WebMirror observes the page, resumes capture when the verification clears, and closes the temporary window. Closing the window yourself cancels the verification; checks have a maximum of **three minutes**.

Use this only for publicly accessible pages you are authorized to mirror. **Do not sign into personal accounts, enter passwords, or provide sensitive information in the verification window.** WebMirror uses a fresh temporary browser context and does not reuse your browser profile or save its cookies. The manual handoff does not solve checks itself (see [Automatic Cloudflare solving](#automatic-cloudflare-solving) for the optional automatic route), cannot bypass site policy, or guarantee that a website permits automated capture even after verification. Cloudflare may still reject the browser. Some backend-dependent app functions remain unavailable offline.

If **Capture JavaScript-loaded assets** is unchecked, verification prompts are disabled. Failed captures do not overwrite older successfully saved snapshots.

## Automatic Cloudflare solving

Tick **Solve Cloudflare checks automatically with Scrapling's stealth browser** (it needs **Capture JavaScript-loaded assets** too). When the normal browser capture meets a Cloudflare check, WebMirror opens Scrapling's patched Chrome in the background, lets it pass the check, and then captures the site from that browser exactly as a normal capture would.

- It uses your installed Google Chrome. Without Chrome, run `scrapling install` once to download Scrapling's own browser (source installs only).
- From source, install it with `pip install -e ".[stealth]"`. The downloadable apps include it.
- It is a fresh, temporary profile: no cookies or sign-ins from your own browser are used.
- Solving a check does not make it acceptable to copy a site. Use it only for sites you are allowed to mirror, and respect their terms and server load. Some sites will still refuse.

## Important limitations

### JavaScript-heavy apps and offline limitations

For `https://fitforge-c5058.web.app/drawer` and `https://orbitstudio3d.com/app`, keep **Capture JavaScript-loaded assets** checked and **mirror again**; old snapshots are not retroactively updated. Chrome or Edge must be installed. The app observes anonymous GET requests made during initial render and a short scroll, then caches those files. Query-string response mapping and SPA history fallback let the original `/drawer` and `/app` routes work in the local server.

**This is not an offline clone of a live backend.** Browser capture cannot archive every state or route not visited, private/authenticated APIs, paid operations, streaming data, dynamic POST requests, or server-generated 3D models. Sites protected by Cloudflare or login may block automated capture. Third-party map tiles may remain online-only. Run a disconnected-browser test before relying on either mirror; check the GUI activity log for skipped assets. Captured files are based on a clean, anonymous browser context, not your signed-in browser.


This is a **best-effort static crawler**, not a browser automation recorder or full replacement for a live backend. It follows same-site HTML pages under the entered base path and downloads linked assets, including CSS imports, static JavaScript imports, and literal `fetch()` / `new URL()` references. Content assembled at runtime, JavaScript-computed resource URLs, streaming map tiles, API requests, authenticated services, server-side features, and external embeds might **not** work offline. For public GitHub Pages project sites, the optional full published-files mode copies files committed under a `site/`, `docs/`, or root publication directory from the corresponding GitHub repository, which helps with generated asset URLs (including Terrain Press elevation tiles). It cannot recover files created only during deployment or replace remote APIs. Verify critical functionality while disconnected before relying on a snapshot.

The app downloads cross-origin **assets referenced by the site**, but does not crawl links to other sites. Use it only for content you have permission to copy; respect licenses, site policies, and server load. It is intentionally limited to 1,000 HTML pages, 5,000 other resources and 30 MB per file to prevent unbounded downloads; pages past the page limit open the live site. Mirroring a site's home page saves every linked page under it, so a hub with hundreds of tools is captured in one go (the browser capture still opens only the first 40 pages). Full GitHub Pages mode needs access to GitHub's public API and raw file hosting and may hit rate limits. Downloads do not bypass login requirements or DRM.

## Development

```bash
python -m pip install -e '.[test]'
pytest -q
```

The tests need none of the optional extras; ones that use `curl_cffi` are skipped when it is not installed.

See `webmirror/core.py` for the mirroring engine, `webmirror/gui.py` for the GUI, and `tests/` for the tests.
