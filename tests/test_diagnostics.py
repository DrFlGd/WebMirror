"""Offline compatibility report tests: detection, privacy, and snapshot lifecycle."""
from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from webmirror.core import MirrorEngine, MirrorLibrary
from webmirror.diagnostics import CompatibilityDiagnostics, safe_location


def test_dependency_and_export_observations_do_not_include_payloads_or_queries():
    diag = CompatibilityDiagnostics()
    diag.observe_asset("https://tools.example/_pt/openscad/openscad.js?token=secret",
                       "application/javascript", b"const moduleName = 'OpenSCAD'; WebAssembly.instantiate()")
    diag.observe_asset("https://tools.example/_pt/openscad/openscad.wasm?token=secret",
                       "application/wasm", b"\\0asm")
    diag.observe_request("https://api.example/v1/render?access_token=secret", "POST",
                         "fetch", "request")
    diag.observe_request("https://api.example/v1/render?access_token=secret", "POST",
                         "fetch", "response", 503)
    diag.observe_request("https://api.example/v1/render?access_token=secret", "POST",
                         "fetch", "failed")
    # A normal GET is not considered an export action.
    diag.observe_request("https://tools.example/logo.png?token=secret", "GET",
                         "image", "response", 200)
    diag.observe_request("https://cdn.example/open.wasm?session=secret", "GET",
                         "fetch", "response", 404, "application/wasm")
    report = diag.report({
        "https://tools.example/not-found.wasm?token=secret": None,
        "https://tools.example/logo.png?token=secret": None,
    })
    dumped = json.dumps(report)
    assert "secret" not in dumped
    assert "access_token" not in dumped
    assert "OpenSCAD" not in dumped  # source text is never retained
    assert report["dependencies"][0]["type"] == "openscad-wasm"
    assert report["dependencies"][0]["wasmObserved"] is True
    assert report["exportRequests"] == [{
        "method": "POST", "host": "api.example", "path": "/v1/render",
        "resourceType": "fetch", "outcome": "network-failed", "status": 503,
    }]
    assert report["missingAssets"] == [
        {"host": "tools.example", "path": "/not-found.wasm"},
        {"host": "cdn.example", "path": "/open.wasm"},
    ]


def test_url_metadata_strips_credentials_query_and_opaque_segments():
    url = "https://username:password@example.com/api/0123456789abcdef0123456789abcdef/export?key=secret"
    data = safe_location(url)
    assert data == {"host": "example.com", "path": "/api/[redacted]/export"}
    assert "password" not in json.dumps(data)
    assert "secret" not in json.dumps(data)


def test_static_mirror_writes_compatibility_report(tmp_path):
    site = tmp_path / "site"
    tool = site / "tool"
    tool.mkdir(parents=True)
    (tool / "index.html").write_text(
        '<html><body><script src="/tool/app.js"></script>'
        '<script src="/tool/missing.js"></script></body></html>', encoding="utf-8"
    )
    (tool / "app.js").write_text(
        'const engine="OpenSCAD"; const wasm=fetch("/tool/engine.wasm?session=secret");',
        encoding="utf-8",
    )
    (tool / "engine.wasm").write_bytes(b"\\0asm")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(site)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        library = MirrorLibrary(tmp_path / "library")
        record = MirrorEngine(library).mirror(
            f"http://127.0.0.1:{server.server_port}/tool/"
        )
        root = library.path(record)
        report = json.loads((root / ".webmirror-compat.json").read_text("utf-8"))
        assert report["schemaVersion"] == 1
        assert report["dependencies"][0]["type"] == "openscad-wasm"
        assert report["dependencies"][0]["wasmObserved"] is True
        assert {"host": "127.0.0.1", "path": "/tool/missing.js"} in report["missingAssets"]
        assert "secret" not in json.dumps(report)
        routes = json.loads((root / ".webmirror-routes.json").read_text("utf-8"))
        wasm_file = root / routes["/tool/engine.wasm?session=secret"].lstrip("/")
        assert wasm_file.read_bytes() == b"\\0asm"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
