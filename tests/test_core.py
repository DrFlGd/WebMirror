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
        assert url.endswith("/demo/index.html")
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
