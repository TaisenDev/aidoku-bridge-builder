#!/usr/bin/env python3
"""Narrow authenticated gateway for the stateless Suwayomi bridge API."""
import base64
import hmac
import ipaddress
import io
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
from PIL import Image, ImageOps, UnidentifiedImageError

GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
SUWAYOMI_URL = os.environ.get("SUWAYOMI_URL", "http://suwayomi:4567").rstrip("/")
SUWAYOMI_USER = os.environ.get("SUWAYOMI_USER", "")
SUWAYOMI_PASS = os.environ.get("SUWAYOMI_PASS", "")
PORT = int(os.environ.get("PORT", "8080"))
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")

MAX_GRAPHQL_BODY = 64 * 1024
MAX_GRAPHQL_RESPONSE = 8 * 1024 * 1024
MAX_MEDIA_RESPONSE = 16 * 1024 * 1024
MAX_PAGE_PIXELS = 50_000_000
MAX_PAGE_TILES = 64
PAGE_TILE_WIDTH = 896
PAGE_TILE_HEIGHT = 1600
PAGE_TILE_CACHE_BYTES = 96 * 1024 * 1024
PAGE_TILE_CACHE_SECONDS = 1800
MAX_GRAPHQL_DEPTH = 14
MAX_GRAPHQL_FIELDS = 300
MAX_IN_FLIGHT = 48

# Comfortable reader bursts, a sustained per-IP ceiling, and a global cap.
GRAPHQL_RATE = (180 / 60, 45)
MEDIA_RATE = (300 / 60, 60)
GLOBAL_GRAPHQL_RATE = (600 / 60, 60)
GLOBAL_MEDIA_RATE = (1800 / 60, 240)
MEDIA_PATH = re.compile(r"^/api/v1/bridge/media/[A-Za-z0-9_-]{20,32768}\.[A-Za-z0-9_-]{20,128}$")
PAGE_ASSETS_PATH = re.compile(r"^/api/v1/bridge/page-assets/(?P<handle>[A-Za-z0-9_-]{20,32768}\.[A-Za-z0-9_-]{20,128})$")
PAGE_TILE_PATH = re.compile(r"^/api/v1/bridge/page-tile/(?P<handle>[A-Za-z0-9_-]{20,32768}\.[A-Za-z0-9_-]{20,128})/(?P<index>[0-9]{1,2})$")
Image.MAX_IMAGE_PIXELS = MAX_PAGE_PIXELS
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
_IMAGE_SLOTS = threading.BoundedSemaphore(2)
_PAGE_TILE_LOCK = threading.Lock()
_PAGE_TILES: OrderedDict[str, tuple[float, list[bytes], int, int, int]] = OrderedDict()
_PAGE_TILE_BYTES = 0


def response_cache_control(path: str, status: int, content_type: str) -> str:
    """Cache only successful signed image handles in the requesting device's cache."""
    if (status == 200
            and content_type.lower().startswith("image/")
            and (MEDIA_PATH.fullmatch(path) or PAGE_TILE_PATH.fullmatch(path))):
        # Signed media handles live for one hour. Keep this private TTL shorter
        # so the device can reuse covers without making them publicly cacheable.
        return "private, max-age=900"
    return "no-store"


def encode_page_tiles(data: bytes) -> tuple[int, int, list[bytes]]:
    """Downscale to an iPad-friendly width and split long pages into transient JPEG tiles."""
    with Image.open(io.BytesIO(data)) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PAGE_PIXELS:
            raise ValueError("image dimensions exceed the page limit")
        if "A" in image.getbands():
            rgba = image.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, "white")
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            image = flattened
        else:
            image = image.convert("RGB")
        if image.width > PAGE_TILE_WIDTH:
            height = max(1, round(image.height * PAGE_TILE_WIDTH / image.width))
            image = image.resize((PAGE_TILE_WIDTH, height), Image.Resampling.LANCZOS)
        width, height = image.size
        count = (height + PAGE_TILE_HEIGHT - 1) // PAGE_TILE_HEIGHT
        if count < 1 or count > MAX_PAGE_TILES:
            raise ValueError("page is too tall to optimize safely")
        tiles = []
        for y in range(0, height, PAGE_TILE_HEIGHT):
            tile = image.crop((0, y, width, min(height, y + PAGE_TILE_HEIGHT)))
            out = io.BytesIO()
            tile.save(out, format="JPEG", quality=88, optimize=False, progressive=False)
            tiles.append(out.getvalue())
        image.close()
        return width, height, tiles


def cache_page_tiles(handle: str, width: int, height: int, tiles: list[bytes]) -> None:
    global _PAGE_TILE_BYTES
    size = sum(len(tile) for tile in tiles)
    now = time.monotonic()
    with _PAGE_TILE_LOCK:
        for key, item in list(_PAGE_TILES.items()):
            if item[0] <= now:
                _PAGE_TILE_BYTES -= item[2]
                del _PAGE_TILES[key]
        old = _PAGE_TILES.pop(handle, None)
        if old:
            _PAGE_TILE_BYTES -= old[2]
        if size > PAGE_TILE_CACHE_BYTES:
            return
        while _PAGE_TILES and _PAGE_TILE_BYTES + size > PAGE_TILE_CACHE_BYTES:
            _, evicted = _PAGE_TILES.popitem(last=False)
            _PAGE_TILE_BYTES -= evicted[2]
        _PAGE_TILES[handle] = (now + PAGE_TILE_CACHE_SECONDS, tiles, size, width, height)
        _PAGE_TILE_BYTES += size


def get_cached_page_tiles(handle: str):
    now = time.monotonic()
    with _PAGE_TILE_LOCK:
        item = _PAGE_TILES.get(handle)
        if not item:
            return None
        if item[0] <= now:
            global _PAGE_TILE_BYTES
            _PAGE_TILE_BYTES -= item[2]
            del _PAGE_TILES[handle]
            return None
        _PAGE_TILES.move_to_end(handle)
        return item


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
        "HeaderFilter": {"name"},
        "SeparatorFilter": {"name"},
        "CheckBoxFilter": {"name", "default"},
        "SelectFilter": {"name", "default", "values"},
        "TextFilter": {"name", "default"},
        "TriStateFilter": {"name", "default"},
        "SortFilter": {"name", "default", "values"},
        "GroupFilter": {"name", "filters"},
        "default": {"index", "ascending"},
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
                # Only known source filter variants are valid fragments.
                type_name = item.type_condition.name.value if item.type_condition else ""
                if parent != "filters" or type_name not in {
                    "HeaderFilter", "SeparatorFilter", "CheckBoxFilter", "SelectFilter",
                    "TextFilter", "TriStateFilter", "SortFilter", "GroupFilter",
                } or item.directives:
                    return False
                if not visit(item.selection_set, type_name, depth + 1):
                    return False
                continue
            if not isinstance(item, FieldNode) or item.directives:
                return False
            name = item.name.value
            alias = item.alias.value if item.alias else None
            allowed_aliases = {
                ("CheckBoxFilter", "default"): "cbDefault",
                ("SelectFilter", "default"): "selDefault",
                ("TextFilter", "default"): "textDefault",
                ("TriStateFilter", "default"): "triDefault",
                ("SortFilter", "default"): "sortDefault",
                ("GroupFilter", "filters"): "groupFilters",
            }
            if alias and alias != allowed_aliases.get((parent, name)):
                return False
            if name == "__typename":
                if parent != "filters" or item.selection_set:
                    return False
                continue
            if name.startswith("__") or name not in allowed_children.get(parent, set()):
                return False
            if item.selection_set and not visit(item.selection_set, name, depth + 1):
                return False
            requires_selection = {
                ("source", "filters"),
                ("bridgeFetchSourceManga", "mangas"),
                ("bridgeFetchMangaAndChapters", "manga"),
                ("bridgeFetchMangaAndChapters", "chapters"),
                ("bridgeResolveUrl", "manga"),
                ("SortFilter", "default"),
                ("GroupFilter", "filters"),
            }
            if not item.selection_set and (parent, name) in requires_selection:
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


def page_assets_response(handle: str) -> tuple[int, bytes, str]:
    cached = get_cached_page_tiles(handle)
    if cached is None:
        if not _IMAGE_SLOTS.acquire(blocking=False):
            return 503, b'{"error":"image optimizer is busy"}', "application/json"
        try:
            code, image_data, content_type = forward(
                "GET", f"/api/v1/bridge/media/{handle}", None, {"Accept": "image/*"}
            )
            if code != 200:
                return code, image_data[:1024 * 1024], content_type
            if not content_type.lower().startswith("image/"):
                return 502, b'{"error":"source did not return an image"}', "application/json"
            width, height, tiles = encode_page_tiles(image_data)
            cache_page_tiles(handle, width, height, tiles)
            cached = get_cached_page_tiles(handle)
            if cached is None:
                # A valid image larger than the bounded cache can still be served for this response.
                cached = (time.monotonic() + PAGE_TILE_CACHE_SECONDS, tiles, sum(map(len, tiles)), width, height)
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
            return 502, b'{"error":"source image could not be optimized safely"}', "application/json"
        finally:
            _IMAGE_SLOTS.release()
    _, tiles, _, width, height = cached
    tile_info = []
    for index, tile in enumerate(tiles):
        tile_height = min(PAGE_TILE_HEIGHT, height - index * PAGE_TILE_HEIGHT)
        tile_info.append({
            "path": f"/api/v1/bridge/page-tile/{handle}/{index}",
            "width": width,
            "height": tile_height,
        })
    payload = json.dumps({"width": width, "height": height, "tiles": tile_info}, separators=(",", ":")).encode()
    return 200, payload, "application/json"

class Handler(BaseHTTPRequestHandler):
    server_version = "bridge-gateway"
    sys_version = ""

    def log_message(self, fmt, *args):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/bridge/"):
            path = "/api/v1/bridge/[signed-handle]"
        print(f"{self.command} {path} -> {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        path = urllib.parse.urlsplit(self.path).path
        self.send_header("Cache-Control", response_cache_control(path, code, ctype))
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
        if urllib.parse.urlsplit(self.path).query:
            return self._deny(404, "not found")
        media_match = MEDIA_PATH.fullmatch(path)
        assets_match = PAGE_ASSETS_PATH.fullmatch(path)
        tile_match = PAGE_TILE_PATH.fullmatch(path)
        if not (media_match or assets_match or tile_match):
            return self._deny(404, "not found")
        if (not allow_rate(client_key(self), "media", MEDIA_RATE)
                or not allow_rate("global", "media", GLOBAL_MEDIA_RATE)):
            return self._deny(429, "media request rate exceeded")
        if assets_match:
            code, body, ctype = page_assets_response(assets_match.group("handle"))
        elif tile_match:
            cached = get_cached_page_tiles(tile_match.group("handle"))
            index = int(tile_match.group("index"))
            if cached is None or index >= len(cached[1]):
                return self._deny(410, "optimized page expired; reload the chapter page")
            return self._send(200, cached[1][index], "image/jpeg")
        else:
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
