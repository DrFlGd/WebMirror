"""Optional real-site check, run on CI; never captures signed-in user data."""
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from webmirror.core import MirrorEngine, MirrorLibrary, MirrorServer


def main():
    url = "https://fitforge-c5058.web.app/drawer"
    with TemporaryDirectory() as directory:
        library = MirrorLibrary(Path(directory))
        try:
            engine = MirrorEngine(
                library, progress=lambda text: print(text[:250], flush=True),
                max_files=180, max_bytes=15 * 1024 * 1024,
                browser_capture=True,
            )
            record = engine.mirror(url)
            print("MIRROR", record, flush=True)
            host = MirrorServer(library)
            try:
                import socket
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                local = host.start(record, port)
                response = requests.get(local, timeout=10, allow_redirects=False)
                print("LOCAL", response.status_code, response.url,
                      "HTML", len(response.content), flush=True)
                print("APP_JS", [
                    path.name for path in library.path(record).glob("assets/*.js")
                ][:12], flush=True)
            finally:
                host.stop()
        except Exception as exc:
            # Availability and anti-bot protection are outside CI's control;
            # this is a diagnostic check, not a merge gate.
            print("SMOKE CHECK INCOMPLETE:", type(exc).__name__, str(exc)[:500], flush=True)


if __name__ == "__main__":
    main()
