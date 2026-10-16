"""Integration tests using tiny local HTTP servers (no internet required)."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import socket

import pytest
import requests

from webmirror.core import MirrorEngine, MirrorLibrary, MirrorServer, normalize_url


class SilentHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def origin(tmp_path):
    site = tmp_path / "origin"
    demo = site / "demo"
    pages = {
        "index.html": """<!doctype html><html><head>
        <link rel="stylesheet" href="styles/main.css">
        <script type="module" src="js/main.js"></script>
        </head><body><h1>Demo</h1>
        <img src="assets/logo.svg" srcset="assets/logo.svg 2x">
        <a href="about.html#top">About</a><a href="/other.html">Outside</a>
        </body></html>""",
        "about.html": '<html><body><h1 id="top">About</h1><a href="index.html">Home</a></body></html>',
        "styles/main.css": '@import "nested.css";body{background:url("../assets/bg.png")}',
        "styles/nested.css": 'div{background-image:url("../assets/logo.svg")}',
        "js/main.js": 'import {num} from "./module.js"; fetch("../data.json");',
        "js/module.js": "export const num = 7;",
        "data.json": '{"ok": true}',
        "assets/logo.svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        "assets/bg.png": "fake-png",
    }
    for rel, content in pages.items():
        output = demo / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    (site / "other.html").write_text("Outside this mirrored path", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SilentHandler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/demo/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_url_validation():
    assert normalize_url("example.org/path#section") == "https://example.org/path"
    assert normalize_url("http://example.com") == "http://example.com/"
    for value in ("", "file:///etc/passwd", "https://user:pass@example.org"):
        with pytest.raises(ValueError):
            normalize_url(value)


def test_mirror_nested_assets_and_local_host(origin, tmp_path):
    library = MirrorLibrary(tmp_path / "data")
    record = MirrorEngine(library).mirror(origin)
    folder = library.path(record)
    html = (folder / "demo/index.html").read_text()
    css = (folder / "demo/styles/main.css").read_text()
    js = (folder / "demo/js/main.js").read_text()
    assert record.files >= 8
    assert record.pages == 2
    assert record.failures == 0
    assert 'href="/demo/styles/main.css"' in html
    assert 'src="/demo/js/main.js"' in html
    assert 'href="/demo/about.html#top"' in html
    assert 'href="/other.html"' in html
    assert not (folder / "other.html").exists()
    assert 'url("/demo/assets/bg.png")' in css
    assert '@import "/demo/styles/nested.css"' in css
    assert 'from "/demo/js/module.js"' in js
    assert 'fetch("/demo/data.json")' in js
    assert (folder / "demo/assets/logo.svg").is_file()
    assert library.list() == [record]

    host = MirrorServer(library)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        url = host.start(record, port=port)
        assert url.endswith("/demo/")
        page = requests.get(url, timeout=3)
        assert page.status_code == 200 and "Demo" in page.text
        js_page = requests.get(f"http://127.0.0.1:{port}/demo/js/module.js", timeout=3)
        assert js_page.status_code == 200
        assert "text/javascript" in js_page.headers["Content-Type"]
    finally:
        host.stop()
    assert host.url is None


def test_failed_refresh_preserves_existing_mirror(origin, tmp_path):
    library = MirrorLibrary(tmp_path / "data")
    record = MirrorEngine(library).mirror(origin)
    page = library.path(record) / "demo/index.html"
    before = page.read_bytes()
    with pytest.raises(RuntimeError):
        MirrorEngine(library).mirror(origin + "missing-page")
    assert page.read_bytes() == before
    assert library.list() == [record]
    library.delete(record)
    assert library.list() == []


def test_resource_limit_is_reported(origin, tmp_path):
    library = MirrorLibrary(tmp_path / "data")
    record = MirrorEngine(library, max_files=2).mirror(origin)
    assert record.files <= 2
    assert record.failures >= 1
    assert (library.path(record) / "demo/index.html").is_file()


def test_opt_in_full_assets_mode_ignored_for_non_github(origin, tmp_path):
    library = MirrorLibrary(tmp_path / "other")
    record = MirrorEngine(library, include_pages_files=True).mirror(origin)
    assert record.files >= 8
    assert record.failures == 0


def test_github_pages_full_asset_capture_with_mock_api(tmp_path):
    """Computed tile filenames are copied even without a literal HTML/JS link."""
    import json
    import requests

    library = MirrorLibrary(tmp_path / "data")
    engine = MirrorEngine(library, include_pages_files=True)
    engine.origin = ("https", "demo.github.io")
    engine.scope = "/example/"
    engine.root = tmp_path / "snapshot"
    (engine.root / "example").mkdir(parents=True)
    (engine.root / "example/index.html").write_text("<h1>Existing page</h1>")
    engine.session = requests.Session()
    engine.visited = {"https://demo.github.io/example/": "/example/index.html"}
    engine.failures = []
    engine.extra_files = 0

    class Response:
        def __init__(self, obj=None, body=b""):
            self.obj, self.body = obj, body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def json(self):
            return self.obj

        def iter_content(self, chunk_size):
            yield self.body

    def get(url, **kwargs):
        if url == "https://api.github.com/repos/demo/example":
            return Response({"default_branch": "main"})
        if "/git/trees/main?" in url:
            return Response({"tree": [
                {"path": "site/index.html", "type": "blob", "size": 22},
                {"path": "site/data/tile_00.webp", "type": "blob", "size": 6},
                {"path": "site/lib/app.js", "type": "blob", "size": 4},
            ]})
        if url.endswith("site/data/tile_00.webp"):
            return Response(body=b"tile00")
        if url.endswith("site/lib/app.js"):
            return Response(body=b"code")
        raise AssertionError(f"Unexpected request: {url}")

    engine.session.get = get
    engine._download_github_pages_assets("https://demo.github.io/example/")
    assert (engine.root / "example/data/tile_00.webp").read_bytes() == b"tile00"
    assert (engine.root / "example/lib/app.js").read_bytes() == b"code"
    assert (engine.root / "example/index.html").read_text() == "<h1>Existing page</h1>"
    assert engine.extra_files == 2
    assert engine.failures == []
    engine.session.close()



def test_spa_path_without_slash_and_client_route_fallback(tmp_path):
    """Original /app must render directly, without redirecting to /app/."""
    from datetime import datetime, timezone
    from webmirror.core import MirrorRecord

    library = MirrorLibrary(tmp_path / "data")
    record = MirrorRecord(
        key="single-page-a12b34c56def",
        url="https://example.org/app",
        entry="/app/index.html",
        saved_at=datetime.now(timezone.utc).isoformat(),
        files=2, pages=1, failures=0,
    )
    folder = library.path(record)
    (folder / "app").mkdir(parents=True)
    (folder / "app/index.html").write_text("<title>SPA App</title>")
    (folder / "app/assets.js").write_text("window.app = 1;")
    (folder / ".webmirror-routes.json").write_text(
        '{"' + '/app' + '": "/app/index.html"}'
    )

    server = MirrorServer(library)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        assert server.start(record, port).endswith("/app")
        root = f"http://127.0.0.1:{port}"
        page = requests.get(root + "/app", timeout=3, allow_redirects=False)
        assert page.status_code == 200
        assert "<title>SPA App</title>" in page.text
        assert "text/html" in page.headers["Content-Type"]
        nested = requests.get(root + "/app/editor", headers={"Accept": "text/html"}, timeout=3)
        assert nested.status_code == 200 and "SPA App" in nested.text
        missing_js = requests.get(root + "/app/missing.js", timeout=3)
        assert missing_js.status_code == 404
        javascript = requests.get(root + "/app/assets.js", timeout=3)
        assert javascript.status_code == 200 and "window.app" in javascript.text
    finally:
        server.stop()


def test_dynamic_capture_preserves_query_parameter_asset_urls(tmp_path):
    """A JS-created fetch('/data?...') resolves to its exact captured data."""
    from datetime import datetime, timezone
    from webmirror.core import MirrorRecord

    library = MirrorLibrary(tmp_path / "data")
    engine = MirrorEngine(library)
    engine.root = tmp_path / "capture"
    engine.root.mkdir()
    engine.visited = {}
    engine.in_progress = set()
    engine.failures = []
    engine.pages = 0
    engine.html_paths = set()
    engine.origin = ("https", "example.org")
    engine.scope = "/"
    url = "https://example.org/api/data?world=mars&tile=2"
    assert engine._store_browser_asset(url, "application/json", b'{"elevation":42}')
    engine._write_routes()

    from urllib.parse import urlsplit
    from pathlib import Path
    import json
    routes = json.loads((engine.root / ".webmirror-routes.json").read_text())
    assert "/api/data?world=mars&tile=2" in routes
    archive = engine.root / routes["/api/data?world=mars&tile=2"].lstrip("/")
    assert json.loads(archive.read_text())["elevation"] == 42
    record = MirrorRecord(
        key="example-org-abcdefabcdef", url="https://example.org/",
        entry="/index.html", saved_at=datetime.now(timezone.utc).isoformat(),
        files=2, pages=1, failures=0,
    )
    destination = library.path(record)
    destination.mkdir()
    (engine.root / "index.html").write_text("<h1>App</h1>")
    import shutil
    shutil.copytree(engine.root, destination, dirs_exist_ok=True)
    server = MirrorServer(library)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        server.start(record, port)
        response = requests.get(f"http://127.0.0.1:{port}/api/data?world=mars&tile=2", timeout=3)
        assert response.status_code == 200
        assert response.json() == {"elevation": 42}
    finally:
        server.stop()


def test_captured_js_chunk_is_written_to_expected_root_path(tmp_path):
    engine = MirrorEngine(MirrorLibrary(tmp_path / "data"))
    engine.root = tmp_path / "snapshot"
    engine.root.mkdir()
    engine.visited = {}
    engine.in_progress = set()
    engine.failures = []
    engine.pages = 0
    engine.html_paths = set()
    engine.origin = ("https", "example.org")
    engine.scope = "/app/"
    assert engine._store_browser_asset(
        "https://example.org/assets/chunk-RANDOM123.js",
        "application/javascript", b'export const mounted = true;',
    )
    assert (engine.root / "assets/chunk-RANDOM123.js").exists()
    assert engine.visited["https://example.org/assets/chunk-RANDOM123.js"] == "/assets/chunk-RANDOM123.js"



def test_cloudflare_challenge_is_not_saved_as_a_valid_website(origin, tmp_path):
    from unittest.mock import patch

    class FakeResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            yield b"<html><head><title>Just a moment...</title></head><body>cf-chl-opt</body></html>"

    def fake_get(self, url, **kwargs):
        return FakeResponse()

    with patch("requests.Session.get", fake_get):
        library = MirrorLibrary(tmp_path / "data")
        with pytest.raises(RuntimeError, match="Cloudflare"):
            MirrorEngine(library).mirror("https://example.org/app")
        assert not library.list()



def test_windows_overlong_asset_name_is_shortened_and_still_serves(tmp_path):
    """Real-world ad/script endpoints can generate >400-character .js names."""
    from datetime import datetime, timezone
    from webmirror.core import MirrorRecord

    library = MirrorLibrary(tmp_path / "data")
    record = MirrorRecord(
        key="3dp-rocks-123456abcdef", url="https://3dp.rocks/lithophane/",
        entry="/lithophane/index.html",
        saved_at=datetime.now(timezone.utc).isoformat(),
        files=2, pages=1, failures=0,
    )
    engine = MirrorEngine(library)
    engine.origin = ("https", "3dp.rocks")
    engine.scope = "/lithophane/"
    engine.root = library.path(record)
    engine.root.mkdir()
    engine.visited = {}
    engine.html_paths = set()
    engine.pages = 0
    engine.in_progress = set()
    engine.failures = []

    token = "0dxm3_" + ("rUs9WP8afH2kL05MQnYgXx72AbC" * 16)
    long_url = f"https://3dp.rocks/lithophane/{token}.js"
    payload = b'window.veryLongFilename = true;'
    assert len(token) > 255
    assert engine._store_browser_asset(long_url, "application/javascript", payload)
    rel = engine._destination(long_url, "js")
    assert rel.suffix == ".js"
    assert max(len(piece) for piece in rel.parts) <= 96
    assert len(rel.as_posix()) <= 160
    assert (engine.root / rel).read_bytes() == payload
    assert engine._destination(long_url, "js") == rel

    # Different names with identical prefixes may not overwrite each other.
    alternate = f"https://3dp.rocks/lithophane/{token[:-1]}Z.js"
    other = engine._destination(alternate, "js")
    assert other != rel

    (engine.root / "lithophane").mkdir(exist_ok=True)
    (engine.root / "lithophane/index.html").write_text("<title>Lithophane</title>")
    engine._write_routes()
    host = MirrorServer(library)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        host.start(record, port)
        response = requests.get(f"http://127.0.0.1:{port}/lithophane/{token}.js", timeout=5)
        assert response.status_code == 200
        assert response.content == payload
        assert "javascript" in response.headers["Content-Type"]
    finally:
        host.stop()


def test_deep_paths_are_flattened_and_queries_remain_distinct(tmp_path):
    engine = MirrorEngine(MirrorLibrary(tmp_path / "data"))
    engine.origin = ("https", "3dp.rocks")
    engine.scope = "/lithophane/"
    many_dirs = "/".join(["many-characters-" + ("x" * 52)] * 6)
    base = f"https://3dp.rocks/lithophane/{many_dirs}/viewer.js"
    first = engine._destination(base + "?id=1", "js")
    second = engine._destination(base + "?id=2", "js")
    assert len(first.as_posix()) <= 160
    assert all(len(part) <= 96 for part in first.parts)
    assert first != second
    assert first.suffix == second.suffix == ".js"
    assert first.parts[0] == "_webmirror_longpaths"


def test_windows_reserved_names_are_safe(tmp_path):
    engine = MirrorEngine(MirrorLibrary(tmp_path / "data"))
    engine.origin = ("https", "example.org")
    assert engine._destination("https://example.org/CON.txt", "text/plain").name == "_CON.txt"
    assert engine._destination("https://example.org/COM1.js", "js").name == "_COM1.js"



def test_static_403_recovers_via_user_verified_browser_capture(tmp_path, monkeypatch):
    """Verified browser HTML becomes the entry page without reusing cookies."""
    from unittest.mock import patch

    library = MirrorLibrary(tmp_path / "data")
    notices = []
    source = "https://example.org/app"
    responses = [
        (source, "text/html", b'<html><head><title>App</title><script src="/assets/main.js"></script></head><body>Hello</body></html>'),
        ("https://example.org/assets/main.js", "application/javascript", b"window.webmirror = true;"),
    ]

    class ForbiddenResponse:
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            raise requests.HTTPError("403 Forbidden - challenge")

    def fake_capture(url, *, on_verification_required, **kwargs):
        assert kwargs["allow_manual_verification"]
        on_verification_required("Complete human verification in the browser.")
        return responses

    with patch("requests.Session.get", return_value=ForbiddenResponse()), patch(
        "webmirror.browser.capture_public_assets", side_effect=fake_capture
    ):
        record = MirrorEngine(
            library,
            browser_capture=True,
            manual_verification=True,
            on_verification_required=notices.append,
        ).mirror(source)

    assert record.entry == "/app/index.html"
    assert notices == ["Complete human verification in the browser."]
    folder = library.path(record)
    page = (folder / "app/index.html").read_text()
    assert 'src="/assets/main.js"' in page
    assert (folder / "assets/main.js").read_bytes() == b"window.webmirror = true;"
    assert record.files >= 2
    assert record.pages == 1
    assert library.list() == [record]


def test_failed_browser_verification_does_not_replace_existing_snapshot(origin, tmp_path):
    """A challenged refresh must never destroy a previously complete mirror."""
    from unittest.mock import patch
    from webmirror.browser import VerificationIncomplete

    library = MirrorLibrary(tmp_path / "data")
    record = MirrorEngine(library).mirror(origin)
    original = (library.path(record) / "demo/index.html").read_bytes()

    class ForbiddenResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            raise requests.HTTPError("403 Forbidden")

    with patch("requests.Session.get", return_value=ForbiddenResponse()), patch(
        "webmirror.browser.capture_public_assets",
        side_effect=VerificationIncomplete("Human check cancelled."),
    ):
        with pytest.raises(RuntimeError, match="entry page"):
            MirrorEngine(
                library, browser_capture=True, manual_verification=True,
            ).mirror(origin)

    assert (library.path(record) / "demo/index.html").read_bytes() == original
    assert library.list() == [record]
