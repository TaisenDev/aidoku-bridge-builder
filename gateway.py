#!/usr/bin/env python3
"""bridge-gateway: scoped public front for Suwayomi.

The phone (wasm) talks here; Suwayomi stays on the private docker network.
Only the operations the bridge needs are allowed — everything else 403/404.

Env (all optional except as noted; optionals may simply be omitted):
  GATEWAY_TOKEN   required bearer token as `Authorization: Bearer <token>`.
  SUWAYOMI_URL    internal Suwayomi base. Default http://suwayomi:4567
  SUWAYOMI_USER / SUWAYOMI_PASS
                  optional pair injected as Basic auth toward Suwayomi.
                  Client Authorization headers are NEVER forwarded.
  PORT            listen port. Default 8080
"""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from graphql import OperationType, parse
from graphql.language.ast import FieldNode, OperationDefinitionNode, ObjectFieldNode, StringValueNode

GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
SUWAYOMI_URL = os.environ.get("SUWAYOMI_URL", "http://suwayomi:4567").rstrip("/")
SUWAYOMI_USER = os.environ.get("SUWAYOMI_USER", "")
SUWAYOMI_PASS = os.environ.get("SUWAYOMI_PASS", "")
PORT = int(os.environ.get("PORT", "8080"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
MAX_GRAPHQL_BODY = 64 * 1024
MAX_GRAPHQL_RESPONSE = 8 * 1024 * 1024
MAX_MEDIA_RESPONSE = 48 * 1024 * 1024
MAX_URL_IMPORTS_PER_HOUR = 120
_IMPORT_RATE_LOCK = threading.Lock()
_IMPORT_REQUESTS: dict[str, list[float]] = {}

# GraphQL root fields used by the bridge. Parse the document instead of
# substring-matching it, so comments, aliases, and nested fields cannot bypass
# the allowlist.
ALLOWED_GQL_FIELDS = {
    "fetchSourceManga",
    "fetchChapters",
    "fetchChapterPages",
    "fetchMangaAndChapters",
    "addMangaFromUrl",
    "source",
}
ALLOWED_MEDIA_PATH = re.compile(
    r"^/api/v1/manga/[0-9]+/(?:thumbnail|chapter/[0-9]+/page/[0-9]+)$"
)


def check_token(handler: BaseHTTPRequestHandler) -> bool:
    custom = handler.headers.get("X-Bridge-Token")
    auth = handler.headers.get("Authorization", "")
    if custom is not None:
        supplied = custom
    elif auth.startswith("Bearer "):
        supplied = auth[7:]
    elif not auth:
        print("auth rejected: token headers missing", flush=True)
        return False
    else:
        print("auth rejected: unsupported Authorization scheme", flush=True)
        return False
    if not hmac.compare_digest(supplied, GATEWAY_TOKEN):
        print(f"auth rejected: token mismatch (length={len(supplied)})", flush=True)
        return False
    return True


def client_key(handler: BaseHTTPRequestHandler) -> str:
    # Trust these only because the documented deployment accepts inbound
    # traffic from Cloudflare's proxy ranges; direct origin access is blocked.
    candidate = handler.headers.get("CF-Connecting-IP", "")
    if not candidate:
        candidate = handler.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    if candidate:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return handler.client_address[0]


def allow_url_import(key: str) -> bool:
    now = time.monotonic()
    cutoff = now - 3600
    with _IMPORT_RATE_LOCK:
        recent = [t for t in _IMPORT_REQUESTS.get(key, []) if t > cutoff]
        if len(recent) >= MAX_URL_IMPORTS_PER_HOUR:
            _IMPORT_REQUESTS[key] = recent
            return False
        recent.append(now)
        _IMPORT_REQUESTS[key] = recent
        return True


def allowed_graphql_document(query: str) -> bool:
    try:
        document = parse(query, max_tokens=5000)
    except Exception:
        return False
    if len(document.definitions) != 1 or not isinstance(document.definitions[0], OperationDefinitionNode):
        return False
    operation = document.definitions[0]
    if operation.operation not in (OperationType.QUERY, OperationType.MUTATION):
        return False
    root_fields = operation.selection_set.selections
    if not root_fields or any(not isinstance(node, FieldNode) or node.name.value not in ALLOWED_GQL_FIELDS for node in root_fields):
        return False
    for field in root_fields:
        if field.name.value != "addMangaFromUrl":
            continue
        input_arg = next((arg.value for arg in field.arguments if arg.name.value == "input"), None)
        if input_arg is None or not hasattr(input_arg, "fields"):
            return False
        url_node = next((item.value for item in input_arg.fields if isinstance(item, ObjectFieldNode) and item.name.value == "url"), None)
        if not isinstance(url_node, StringValueNode) or not safe_public_url(url_node.value):
            return False
    return True


def safe_public_url(value: str) -> bool:
    if len(value) > 2048:
        return False
    try:
        url = urllib.parse.urlsplit(value)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return False
        host = url.hostname.rstrip(".").lower()
        if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False


def server_auth() -> dict:
    if SUWAYOMI_USER:
        token = base64.b64encode(f"{SUWAYOMI_USER}:{SUWAYOMI_PASS}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def forward(method: str, path: str, body: bytes | None, incoming: dict) -> tuple:
    url = SUWAYOMI_URL + path
    forward_headers = {"accept", "user-agent", "range", "if-none-match", "if-modified-since"}
    headers = {k: v for k, v in incoming.items() if k.lower() in forward_headers}
    if body is not None:
        # GraphQL JSON is parsed by Suwayomi based on this header.
        headers["Content-Type"] = "application/json"
    headers.update(server_auth())
    headers["Referer"] = SUWAYOMI_URL
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            limit = MAX_GRAPHQL_RESPONSE if path == "/api/graphql" else MAX_MEDIA_RESPONSE
            data = r.read(limit + 1)
            if len(data) > limit:
                return 502, b'{"error":"upstream response too large"}', "application/json"
            return r.status, data, r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        return e.code, e.read(1024 * 1024), "application/json"


class Handler(BaseHTTPRequestHandler):
    server_version = "bridge-gateway"
    sys_version = ""

    def log_message(self, fmt, *args):
        print(f"{self.address_string()[0]} {self.command} {self.path} -> {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code: int, msg: str):
        self._send(code, json.dumps({"error": msg}).encode())

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._send(200, b'{"ok":true}')
        if not check_token(self):
            return self._deny(401, "missing or invalid token")
        if not ALLOWED_MEDIA_PATH.fullmatch(path) or urllib.parse.urlsplit(self.path).query:
            return self._deny(404, "not found")
        code, body, ctype = forward("GET", path, None, dict(self.headers))
        self._send(code, body, ctype)

    def do_POST(self):
        if self.path != "/api/graphql":
            return self._deny(404, "not found")
        if not check_token(self):
            return self._deny(401, "missing or invalid token")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "bad length")
        if length <= 0 or length > MAX_GRAPHQL_BODY:
            return self._deny(413, "request body too large or empty")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            query = json.loads(raw).get("query", "")
        except Exception:
            return self._deny(400, "bad json")
        if not isinstance(query, str) or not allowed_graphql_document(query):
            return self._deny(403, "operation not allowed")
        if "addMangaFromUrl" in query and not allow_url_import(client_key(self)):
            return self._deny(429, "URL import rate limit reached")
        out = json.dumps({"query": query}).encode()
        code, body, ctype = forward(
            "POST", "/api/graphql", out,
            {"Content-Type": "application/json", "Accept": "application/json"},
        )
        self._send(code, body, ctype)


if __name__ == "__main__":
    if len(GATEWAY_TOKEN) < 32:
        raise SystemExit("FATAL: GATEWAY_TOKEN must contain at least 32 characters")
    if bool(SUWAYOMI_USER) != bool(SUWAYOMI_PASS):
        raise SystemExit("FATAL: SUWAYOMI_USER + SUWAYOMI_PASS (both or neither)")
    print(f"gateway -> {SUWAYOMI_URL} on {BIND_HOST}:{PORT} [token required]", flush=True)
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
