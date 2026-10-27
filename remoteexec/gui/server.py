"""Local HTTP server for the dashboard (stdlib only, binds 127.0.0.1)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..serialize import __version__
from .service import JobService

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "DashboardServer"

    def log_message(self, *args):  # keep the terminal quiet
        pass

    # -- responses --
    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never cache: a stale app.js silently keeps running old client logic.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _guard(self, post: bool) -> bool:
        """Only answer requests addressed to loopback (defeats DNS rebinding),
        and only accept POSTs carrying our header (a cross-site form can't)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if self.server.loopback_only and host not in LOOPBACK:
            self._send(403, b"forbidden", "text/plain")
            return False
        if post and self.headers.get("X-Remoteexec") != "1":
            self._send(403, b"missing X-Remoteexec header", "text/plain")
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return {}

    # -- routing --
    def do_GET(self):
        if not self._guard(post=False):
            return
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        svc = self.server.service
        try:
            if not parts:
                return self._static("index.html")
            if parts[0] == "static" and len(parts) == 2:
                return self._static(parts[1])
            if parts[0] != "api":
                return self._send(404, b"not found", "text/plain")
            route = parts[1:]
            if route == ["health"]:
                return self._json({"ok": True, "version": __version__, "pid": os.getpid()})
            if route == ["jobs"]:
                selected = {f for f in q.get("filters", "").split(",") if f}
                jobs = svc.list_jobs(selected or None)
                errors = svc.host_errors() if hasattr(svc, "host_errors") else {}
                return self._json({"jobs": jobs, "errors": errors})
            if route == ["hosts"]:
                return self._json(svc.hosts())
            if len(route) >= 2 and route[0] == "jobs":
                jid, rest = route[1], route[2:]
                if not rest:
                    d = svc.detail(jid)
                    return self._json(d) if d else self._json({"error": "unknown job"}, 404)
                if rest == ["logs"]:
                    return self._json(svc.read_log(jid, int(q.get("offset", 0) or 0)))
                if rest == ["files"]:
                    return self._json(svc.files(jid))
                if rest == ["script"]:
                    return self._json(svc.script(jid))
                if rest == ["file"]:
                    if q.get("raw"):
                        status, ctype, body = svc.raw(jid, q.get("path", ""))
                        return self._send(status, body, ctype)
                    return self._json(svc.preview(jid, q.get("path", "")))
            self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if not self._guard(post=True):
            return
        parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
        svc, body = self.server.service, self._body()
        try:
            if parts[:2] == ["api", "jobs"] and len(parts) == 4:
                jid, action = parts[2], parts[3]
                if action == "cancel":
                    return self._json(svc.cancel(jid))
                if action == "fetch":
                    return self._json(svc.fetch_outputs(jid))
            if parts == ["api", "reveal"]:
                return self._json(svc.reveal(str(body.get("path", ""))))
            if parts == ["api", "hosts"]:
                return self._json(svc.save_hosts(body.get("clusters", {})))
            if parts == ["api", "hosts", "test"]:
                return self._json(svc.test_alias(str(body.get("alias", ""))))
            self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _static(self, name: str):
        path = (STATIC_DIR / name).resolve()
        if path.parent != STATIC_DIR.resolve() or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, path.read_bytes(), CTYPES.get(path.suffix, "application/octet-stream"))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, service: JobService | None = None):
        super().__init__(addr, Handler)
        self.service = service or JobService()
        self.loopback_only = addr[0] in LOOPBACK


def create_server(host: str = "127.0.0.1", port: int = 8765, service=None) -> DashboardServer:
    return DashboardServer((host, port), service)


def _exit_with_parent(pid: int):
    """The menu bar app passes its pid; don't outlive it."""
    def watch():
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass
            time.sleep(2)
    threading.Thread(target=watch, daemon=True).start()


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          parent_pid: int | None = None):
    try:
        server = create_server(host, port)
    except OSError as e:
        print(f"remoteexec: cannot listen on {host}:{port} ({e.strerror}); "
              f"is the GUI already running? Try --port.", file=sys.stderr)
        sys.exit(1)
    print(f"remoteexec GUI -> http://{host}:{port}/  (Ctrl-C to stop)", flush=True)
    if parent_pid:
        _exit_with_parent(parent_pid)
    if open_browser:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
