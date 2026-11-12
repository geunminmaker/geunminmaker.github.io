"""Static file server for local development and tests.

    python tools/serve.py [port] [--dir web]

Serves ``web/`` with the content types the app needs (ES modules, WebAssembly)
and without caching, on http://localhost:<port>/ (service workers work on
localhost without HTTPS).
"""
from __future__ import annotations

import http.server
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".wasm": "application/wasm",
    ".whl": "application/octet-stream",
    ".zip": "application/zip",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def guess_type(self, path):
        ext = os.path.splitext(str(path))[1].lower()
        return TYPES.get(ext) or super().guess_type(path)

    #: tests set these on a subclass: count requests per path, mimic the caching of the real host
    counter = None
    cache_control = "no-cache"

    def do_GET(self):
        if self.counter is not None:
            self.counter[self.path.split("?")[0]] += 1
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", self.cache_control)
        super().end_headers()

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # the browser closed the connection (navigation, aborted download)

    def log_message(self, format, *args):  # quiet
        if os.environ.get("SERVE_VERBOSE"):
            super().log_message(format, *args)


def make_server(directory: str, port: int = 0, counter=None,
                cache_control: str = "no-cache") -> http.server.ThreadingHTTPServer:
    bound = type("BoundHandler", (Handler,), {"counter": counter, "cache_control": cache_control})
    handler = lambda *a, **kw: bound(*a, directory=directory, **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve_in_thread(directory: str | None = None, port: int = 0, counter=None, cache_control: str = "no-cache"):
    """Start a server in the background; returns (server, base_url)."""
    directory = directory or os.path.join(ROOT, "web")
    server = make_server(directory, port, counter, cache_control)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://localhost:{server.server_address[1]}"


def main() -> None:
    rest = sys.argv[1:]
    directory = os.path.join(ROOT, "web")
    if "--dir" in rest:
        at = rest.index("--dir")
        directory = os.path.abspath(rest[at + 1])
        del rest[at:at + 2]
    port = int(rest[0]) if rest else 8765
    server = make_server(directory, port)
    print(f"http://localhost:{port}/  ->  {directory}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
