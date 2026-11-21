"""Privacy-conscious dependency and export diagnostics for offline mirrors.

The report contains only resource paths and network metadata: no cookies,
headers, query strings, form values, request bodies, or response bodies.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

COMPAT_FILE = ".webmirror-compat.json"
MAX_SOURCE_SCAN = 512 * 1024
MAX_EXPORT_REQUESTS = 100
MAX_MISSING_ASSETS = 100
ASSET_SUFFIXES = (".js", ".mjs", ".wasm", ".data", ".scad", ".json", ".css", ".worker")
EXPORT_TERMS = re.compile(r"(?:export|download|render|generate|compile|\.stl|\.3mf|\.scad)(?:/|$|[-_.])", re.I)
OPENSCAD = re.compile(r"openscad", re.I)
EMSCRIPTEN = re.compile(r"emscripten|WebAssembly|\.wasm(?:\b|$)", re.I)
SENSITIVE_SEGMENT = re.compile(r"^(?:\d{5,}|[a-f0-9]{24,}|[A-Za-z0-9_-]{32,}|[^/]*@[^/]*)$", re.I)


def safe_location(url: str) -> dict[str, str]:
    """Keep only the host and recognizable path, never URL credentials or query."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()[:180]
        segments = parsed.path.split("/")
        path = "/".join("[redacted]" if SENSITIVE_SEGMENT.fullmatch(s) else s
                        for s in segments)
        return {"host": host, "path": path[:240] or "/"}
    except ValueError:
        return {"host": "", "path": "/"}


class CompatibilityDiagnostics:
    """Bounded observations; does not retain fetched content or request objects."""

    def __init__(self) -> None:
        self._openscad = False
        self._emscripten = False
        self._wasm = False
        self._evidence: set[str] = set()
        self._exports: dict[tuple[str, str, str], dict] = {}

    def observe_asset(self, url: str, mime: str, data: bytes) -> None:
        location = safe_location(url)
        path = location["path"]
        suffix = path.lower().split("/")[-1].split(".")[-1]
        is_wasm = suffix == "wasm" or mime.lower() == "application/wasm"
        if is_wasm:
            self._wasm = True
            self._evidence.add(path)
        # Inspect a bounded prefix of text-like assets; never retain the text.
        is_text = (mime.startswith(("text/", "application/javascript", "application/json"))
                   or path.lower().endswith((".js", ".mjs", ".json", ".html")))
        if not is_text:
            if OPENSCAD.search(path):
                self._openscad = True
                self._evidence.add(path)
            return
        sample = data[:MAX_SOURCE_SCAN].decode("utf-8", errors="ignore")
        if OPENSCAD.search(sample) or OPENSCAD.search(path):
            self._openscad = True
            self._evidence.add(path)
        if EMSCRIPTEN.search(sample):
            self._emscripten = True
            self._evidence.add(path)

    def observe_request(self, url: str, method: str, resource_type: str,
                        event: str, status: int | None = None) -> None:
        """Record export-like requests and non-GET calls; never inspect their data."""
        where = safe_location(url)
        method = method.upper()
        if (method in ("GET", "HEAD")
                and not EXPORT_TERMS.search(where["path"])
                and not (event == "failed" and resource_type in ("fetch", "xhr"))):
            return
        key = (method, where["host"], where["path"])
        if key not in self._exports and len(self._exports) >= MAX_EXPORT_REQUESTS:
            return
        record = self._exports.setdefault(key, {
            "method": method, **where, "resourceType": str(resource_type)[:32],
            "outcome": "requested",
        })
        if event == "failed":
            record["outcome"] = "network-failed"
        elif event == "response":
            record["outcome"] = "http-response"
            if isinstance(status, int):
                record["status"] = status

    def report(self, visited: dict[str, str | None]) -> dict:
        missing = []
        for url, local in visited.items():
            if local is not None:
                continue
            where = safe_location(url)
            if where["path"].lower().endswith(ASSET_SUFFIXES):
                missing.append(where)
            if len(missing) >= MAX_MISSING_ASSETS:
                break
        dependencies = []
        if self._openscad:
            dependencies.append({
                "type": "openscad-wasm",
                "detection": "reference",
                "wasmObserved": self._wasm,
                "evidence": sorted(self._evidence)[:16],
            })
        elif self._wasm or self._emscripten:
            dependencies.append({
                "type": "webassembly",
                "detection": "reference",
                "wasmObserved": self._wasm,
                "evidence": sorted(self._evidence)[:16],
            })
        return {
            "schemaVersion": 1,
            "dependencies": dependencies,
            "exportRequests": list(self._exports.values()),
            "missingAssets": missing,
            "note": ("Requests are observations only; a preview does not prove offline "
                     "export works. No request payloads, headers or query strings are saved."),
        }
