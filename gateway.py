#!/usr/bin/env python3
"""Narrow authenticated gateway for the stateless Suwayomi bridge API."""
import base64
import hmac
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from graphql import OperationType, parse
from graphql.language.ast import FieldNode, InlineFragmentNode, OperationDefinitionNode

GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
SUWAYOMI_URL = os.environ.get("SUWAYOMI_URL", "http://suwayomi:4567").rstrip("/")
SUWAYOMI_USER = os.environ.get("SUWAYOMI_USER", "")
SUWAYOMI_PASS = os.environ.get("SUWAYOMI_PASS", "")
PORT = int(os.environ.get("PORT", "8080"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")

MAX_GRAPHQL_BODY = 64 * 1024
MAX_GRAPHQL_RESPONSE = 8 * 1024 * 1024
MAX_MEDIA_RESPONSE = 16 * 1024 * 1024
MAX_GRAPHQL_DEPTH = 14
MAX_GRAPHQL_FIELDS = 300
MAX_IN_FLIGHT = 48

# Comfortable reader bursts, a sustained per-IP ceiling, and a global cap.
GRAPHQL_RATE = (180 / 60, 45)
MEDIA_RATE = (300 / 60, 60)
GLOBAL_GRAPHQL_RATE = (600 / 60, 60)
GLOBAL_MEDIA_RATE = (1800 / 60, 240)
MEDIA_PATH = re.compile(r"^/api/v1/bridge/media/[A-Za-z0-9_-]{20,32768}\.[A-Za-z0-9_-]{20,128}$")
ALLOWED_FIELDS = {
    "source",
    "bridgeFetchSourceManga",
    "bridgeFetchMangaAndChapters",
    "bridgeFetchChapterPages",
    "bridgeResolveUrl",
}

_TOKEN_LOCK = threading.Lock()
_BUCKETS: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()
MAX_RATE_BUCKETS = 8192
_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT)


def check_token(handler: BaseHTTPRequestHandler) -> bool:
    supplied = handler.headers.get("X-Bridge-Token")
    if supplied is None:
        auth = handler.headers.get("Authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
    return bool(supplied) and hmac.compare_digest(supplied, GATEWAY_TOKEN)


def client_key(handler: BaseHTTPRequestHandler) -> str:
    # Trust forwarding headers only in the documented Cloudflare-proxied deployment.
    candidate = handler.headers.get("CF-Connecting-IP", "")
    if not candidate:
        candidate = handler.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    try:
        if candidate:
            return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    return handler.client_address[0]


def allow_rate(key: str, route: str, rate: tuple[float, int]) -> bool:
    refill, capacity = rate
    now = time.monotonic()
    with _TOKEN_LOCK:
        bucket_key = (route, key)
        previous = _BUCKETS.get(bucket_key)
        tokens, updated = previous if previous is not None else (float(capacity), now)
        tokens = min(float(capacity), tokens + max(0.0, now - updated) * refill)
        if tokens < 1.0:
            _BUCKETS[bucket_key] = (tokens, now)
            _BUCKETS.move_to_end(bucket_key)
            return False
        _BUCKETS[bucket_key] = (tokens - 1.0, now)
        _BUCKETS.move_to_end(bucket_key)
        while len(_BUCKETS) > MAX_RATE_BUCKETS:
            _BUCKETS.popitem(last=False)
        return True


def _document_shape_is_allowed(query: str) -> bool:
    try:
        document = parse(query, max_tokens=1500)
    except Exception:
        return False
    if len(document.definitions) != 1 or not isinstance(document.definitions[0], OperationDefinitionNode):
        return False
    operation = document.definitions[0]
    roots = operation.selection_set.selections
    if len(roots) != 1 or not isinstance(roots[0], FieldNode):
        return False
    root = roots[0]
    if root.name.value not in ALLOWED_FIELDS or root.alias or root.directives:
        return False
    expected = OperationType.QUERY if root.name.value == "source" else OperationType.MUTATION
    if operation.operation != expected or operation.directives:
        return False

    # Field allowlists also prevent the generic `source` query from walking into
    # Suwayomi's persistent manga/library tables. __typename is needed for the
    # source filter union, but schema introspection and aliases remain forbidden.
    allowed_children = {
        "source": {"filters"},
        "filters": {"__typename"},
        "CheckBoxFilter": {"name", "default"},
        "SelectFilter": {"name", "default", "values"},
        "TextFilter": {"name"},
        "bridgeFetchSourceManga": {"mangas", "hasNextPage"},
        "mangas": {"id", "sourceId", "url", "realUrl", "title", "thumbnailUrl", "author", "artist", "description", "genre", "status"},
        "bridgeFetchMangaAndChapters": {"manga", "chapters"},
        "manga": {"id", "sourceId", "url", "realUrl", "title", "thumbnailUrl", "author", "artist", "description", "genre", "status"},
        "chapters": {"id", "url", "realUrl", "name", "uploadDate", "chapterNumber", "scanlator"},
        "bridgeFetchChapterPages": {"pages"},
        "bridgeResolveUrl": {"status", "message", "manga"},
    }
    count = 0

    def visit(selection_set, parent: str, depth: int) -> bool:
        nonlocal count
        if depth > MAX_GRAPHQL_DEPTH:
            return False
        for item in selection_set.selections:
            count += 1
            if count > MAX_GRAPHQL_FIELDS:
                return False
            if isinstance(item, InlineFragmentNode):
                # Only the three known source filter variants are valid fragments.
                type_name = item.type_condition.name.value if item.type_condition else ""
                if parent != "filters" or type_name not in {"CheckBoxFilter", "SelectFilter", "TextFilter"} or item.directives:
                    return False
                if not visit(item.selection_set, type_name, depth + 1):
                    return False
                continue
            if not isinstance(item, FieldNode) or item.directives:
                return False
            name = item.name.value
            alias = item.alias.value if item.alias else None
            if alias and not (
                name == "default"
                and ((parent == "CheckBoxFilter" and alias == "cbDefault")
                     or (parent == "SelectFilter" and alias == "selDefault"))
            ):
                return False
            if name == "__typename":
                if parent != "filters" or item.selection_set:
                    return False
                continue
            if name.startswith("__") or name not in allowed_children.get(parent, set()):
                return False
            if item.selection_set and not visit(item.selection_set, name, depth + 1):
                return False
            if not item.selection_set and name in allowed_children:
                return False
        return True

    count = 1  # The allowlisted root field.
    return bool(root.selection_set) and visit(root.selection_set, root.name.value, 2)


def server_auth() -> dict[str, str]:
    if SUWAYOMI_USER:
        token = base64.b64encode(f"{SUWAYOMI_USER}:{SUWAYOMI_PASS}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def forward(method: str, path: str, body: bytes | None, incoming: dict[str, str]) -> tuple[int, bytes, str]:
    headers = {k: v for k, v in incoming.items()
               if k.lower() in {"accept", "user-agent", "range", "if-none-match", "if-modified-since"}}
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers.update(server_auth())
    headers["Referer"] = SUWAYOMI_URL
    req = urllib.request.Request(SUWAYOMI_URL + path, data=body, headers=headers, method=method)
    limit = MAX_GRAPHQL_RESPONSE if path == "/api/graphql" else MAX_MEDIA_RESPONSE
    try:
        with urllib.request.urlopen(req, timeout=75) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                return 502, b'{"error":"upstream response too large"}', "application/json"
            return response.status, data, response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        return error.code, error.read(min(limit, 1024 * 1024)), "application/json"
    except Exception:
        return 502, b'{"error":"upstream unavailable"}', "application/json"


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def process_request(self, request, client_address):
        if not _SLOTS.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            _SLOTS.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            _SLOTS.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "bridge-gateway"
    sys_version = ""

    def log_message(self, fmt, *args):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/bridge/media/"):
            path = "/api/v1/bridge/media/[signed-handle]"
        print(f"{self.command} {path} -> {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code: int, msg: str):
        self._send(code, json.dumps({"error": msg}).encode())

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._send(200, b'{"ok":true}')
        if not check_token(self):
            return self._deny(401, "unauthorized")
        if not MEDIA_PATH.fullmatch(path) or urllib.parse.urlsplit(self.path).query:
            return self._deny(404, "not found")
        if (not allow_rate(client_key(self), "media", MEDIA_RATE)
                or not allow_rate("global", "media", GLOBAL_MEDIA_RATE)):
            return self._deny(429, "media request rate exceeded")
        code, body, ctype = forward("GET", path, None, dict(self.headers))
        self._send(code, body, ctype)

    def do_POST(self):
        if self.path != "/api/graphql":
            return self._deny(404, "not found")
        if not check_token(self):
            return self._deny(401, "unauthorized")
        if (not allow_rate(client_key(self), "graphql", GRAPHQL_RATE)
                or not allow_rate("global", "graphql", GLOBAL_GRAPHQL_RATE)):
            return self._deny(429, "API request rate exceeded")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "bad content length")
        if length <= 0 or length > MAX_GRAPHQL_BODY:
            return self._deny(413, "request body too large or empty")
        raw = self.rfile.read(length)
        if len(raw) != length:
            return self._deny(400, "incomplete request body")
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return self._deny(400, "invalid JSON")
        if not isinstance(request, dict) or set(request) - {"query", "variables"}:
            return self._deny(400, "unsupported GraphQL request fields")
        query = request.get("query")
        variables = request.get("variables", {})
        if not isinstance(query, str) or not isinstance(variables, dict) or not _document_shape_is_allowed(query):
            return self._deny(403, "operation not allowed")
        out = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode()
        if len(out) > MAX_GRAPHQL_BODY:
            return self._deny(413, "request body too large")
        code, body, ctype = forward("POST", "/api/graphql", out, {"Accept": "application/json"})
        self._send(code, body, ctype)


if __name__ == "__main__":
    if len(GATEWAY_TOKEN) < 32:
        raise SystemExit("FATAL: GATEWAY_TOKEN must contain at least 32 characters")
    if bool(SUWAYOMI_USER) != bool(SUWAYOMI_PASS):
        raise SystemExit("FATAL: SUWAYOMI_USER + SUWAYOMI_PASS (both or neither)")
    print(f"gateway -> {SUWAYOMI_URL} on {BIND_HOST}:{PORT} [token required]", flush=True)
    BoundedThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
