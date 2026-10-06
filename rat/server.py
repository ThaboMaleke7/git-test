"""Stdlib HTTP server: JSON API + static dashboard.

No third-party dependencies; ``python3 run.py`` is all it takes to start.
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .gitscan import ScanError
from .store import Store, StoreError, DEFAULT_DATA_DIR

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _filters_from_query(query: dict) -> dict:
    def first(name, default=None):
        vals = query.get(name)
        return vals[0] if vals else default

    mode = first("mode", "all")
    if mode not in ("all", "range", "list"):
        mode = "all"

    def as_int(name):
        raw = first(name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ApiError(f"invalid value for '{name}': {raw!r}")

    commits = [h.strip() for h in (first("commits", "") or "").split(",") if h.strip()]
    return {
        "mode": mode,
        "from": as_int("from"),
        "to": as_int("to"),
        "commits": commits,
        "author": first("author") or None,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"RAT/{__version__}"
    protocol_version = "HTTP/1.1"
    store: Store = None  # injected

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    def log_message(self, fmt, *args):  # keep the console tidy
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400):
        self._json({"error": message}, status)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(f"invalid JSON body: {exc}") from exc

    # ------------------------------------------------------------------ #
    # routing
    # ------------------------------------------------------------------ #
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            self._route(method, path, query)
        except ApiError as exc:
            self._error(str(exc), exc.status)
        except ScanError as exc:
            self._error(str(exc), 400)
        except StoreError as exc:
            self._error(str(exc), 404 if "unknown" in str(exc) else 400)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - last resort for the UI
            import traceback
            traceback.print_exc()
            self._error(f"internal error: {exc}", 500)

    def _route(self, method: str, path: str, query: dict):
        store = self.store

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path in ("/css", "/css/"):
            return self._static("css/styles.css")
        if path.startswith("/css/"):
            return self._static(path[1:])
        if path.startswith("/js/"):
            return self._static(path[1:])
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if not path.startswith("/api/"):
            return self._error("not found", 404)

        parts = [p for p in path[len("/api/"):].split("/") if p]

        # /api/health
        if method == "GET" and parts == ["health"]:
            return self._json({"ok": True, "version": __version__})

        # /api/jobs/<id>
        if parts and parts[0] == "jobs":
            if len(parts) < 2:
                raise ApiError("missing job id")
            return self._json(store.get_job(parts[1]))

        # /api/repos ...
        if not parts or parts[0] != "repos":
            return self._error("not found", 404)

        # /api/repos
        if parts == ["repos"]:
            if method == "GET":
                return self._json({"repos": store.list_repos()})
            return self._error("method not allowed", 405)

        # /api/repos/upload
        if parts == ["repos", "upload"] and method == "POST":
            blob = self._read_body()
            if not blob:
                raise ApiError("empty upload body")
            filename = self.headers.get("X-Filename") or "upload.zip"
            filename = urllib.parse.unquote(filename)
            if not filename.lower().endswith(".zip"):
                raise ApiError("only .zip archives are accepted")
            job_id = store.upload_zip_async(filename, blob)
            return self._json({"job_id": job_id}, 202)

        # /api/repos/clone
        if parts == ["repos", "clone"] and method == "POST":
            body = self._read_json()
            url = (body.get("url") or "").strip()
            if not url:
                raise ApiError("'url' is required")
            job_id = store.clone_async(url, body.get("name") or "")
            return self._json({"job_id": job_id}, 202)

        if len(parts) >= 2 and parts[1]:
            repo_id = parts[1]
            rest = parts[2:]
            if not rest and method == "DELETE":
                store.delete_repo(repo_id)
                return self._json({"ok": True})
            if rest == ["refs"]:
                refresh = (query.get("refresh", ["0"])[0] or "0") not in ("0", "", "false")
                return self._json(store.refs(repo_id, refresh=refresh))
            if rest == ["commits"]:
                ref = query.get("ref", ["HEAD"])[0] or "HEAD"
                offset = int(query.get("offset", ["0"])[0] or 0)
                limit = int(query.get("limit", ["100"])[0] or 100)
                q = query.get("q", [""])[0]
                return self._json(store.commits_page(repo_id, ref, offset, limit, q))
            if rest == ["authors"]:
                ref = query.get("ref", ["HEAD"])[0] or "HEAD"
                light = (query.get("light", ["0"])[0] or "0") not in ("0", "", "false")
                if light:
                    return self._json({"canonical": store.authors_light(repo_id, ref),
                                       "identities": [], "merges": store.get_repo(repo_id).get("merges", {})})
                return self._json(store.authors(repo_id, ref))
            if rest == ["merges"]:
                if method == "POST":
                    body = self._read_json()
                    target = body.get("target") or ""
                    sources = body.get("sources") or []
                    if not target or not isinstance(sources, list):
                        raise ApiError("'target' and 'sources' are required")
                    return self._json({"merges": store.set_merges(repo_id, target, sources)})
                if method == "DELETE":
                    return self._json({"merges": store.clear_merges(repo_id)})
                return self._error("method not allowed", 405)
            if rest == ["metrics"] and method == "GET":
                ref = query.get("ref", ["HEAD"])[0] or "HEAD"
                filters = _filters_from_query(query)
                return self._json(store.metrics(repo_id, ref, filters))
            if rest == ["object"] and method == "GET":
                ref = query.get("ref", ["HEAD"])[0] or "HEAD"
                path_ = query.get("path", [""])[0]
                kind = query.get("kind", [""])[0] or None
                filters = _filters_from_query(query)
                return self._json(store.object_detail(repo_id, ref, filters, path_, kind))
            if rest == ["stats"] and method == "GET":
                ref = query.get("ref", ["HEAD"])[0] or "HEAD"
                return self._json(store.tree_stats(repo_id, ref))

        return self._error("not found", 404)

    # ------------------------------------------------------------------ #
    # static files
    # ------------------------------------------------------------------ #
    def _static(self, rel: str):
        rel = rel.replace("\\", "/").lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
            return self._error("not found", 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        return self._send(200, body, ctype)


def serve(host: str = "127.0.0.1", port: int = 8000, data_dir: str = DEFAULT_DATA_DIR):
    store = Store(data_dir)
    handler = type("BoundHandler", (Handler,), {"store": store})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}"
    print("=" * 62)
    print("  RAT - Repo Analysis Tool   (COMS3011A)")
    print(f"  Dashboard : {url}")
    print(f"  Data dir  : {store.data_dir}")
    print("  Stop with Ctrl+C")
    print("=" * 62, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
