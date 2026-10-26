#!/usr/bin/env python3
"""bridge-builder: generate Aidoku .aix bridges (1 per Suwayomi source) + repo index.

Reads the Suwayomi catalog via GraphQL, installs a selected extension only
when Aidoku requests its package, applies pending updates to installed
extensions, and per source generates:
  repo/<bridge>.aix  (= deployment wasm + generated res/ + opaque 128 icon)
  repo/index.min.json (Aidoku-style source list) + icons/

The wasm is built for this deployment (API URL + optional token baked in);
every bridge shares it and carries zero user-facing configuration (only the
sourceId, pre-filled). Runtime traffic goes to the gateway, never to
Suwayomi directly.

Env (only STATIC_URL and SUWAYOMI_URL are effectively required; optionals
may simply be omitted from compose):
  STATIC_URL        public base serving this repo, e.g. https://aidoku.example.com (required)
  SUWAYOMI_URL      internal URL, e.g. http://suwayomi:4567 (default set)
  SUWAYOMI_USER / SUWAYOMI_PASS
                    optional pair for builder -> Suwayomi auth. Omit both when
                    Suwayomi runs without auth (private network). One without
                    the other is a config error.
  LANGUAGES         "all" (default) | "es" | "es,en" | "es,multi" ... Only
                    matching catalog entries are published and installable.
  REPO_DIR          output dir. Default /repo
  POLL_SECONDS      seconds between cycles. Default 3600.
"""

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import threading
import time
import zipfile
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image

SUWAYOMI_URL = os.environ.get("SUWAYOMI_URL", "http://suwayomi:4567")
SUWAYOMI_USER = os.environ.get("SUWAYOMI_USER", "")
SUWAYOMI_PASS = os.environ.get("SUWAYOMI_PASS", "")
STATIC_URL = os.environ.get("STATIC_URL", "").rstrip("/")
LANGUAGES = [l.strip().lower() for l in os.environ.get("LANGUAGES", "all").split(",") if l.strip()]
BRIDGE_WASM = Path(os.environ.get("BRIDGE_WASM", "/tmp/bridge.wasm"))
BRIDGE_STOCK = Path("/wasm-stock/bridge.wasm")
API_URL = os.environ.get("API_URL", "").rstrip("/")
API_BASE_URL = API_URL or STATIC_URL
TOKEN = os.environ.get("TOKEN", "")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
REPO_DIR = Path(os.environ.get("REPO_DIR", "/repo"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "3600"))
INSTALL_HOST = os.environ.get("INSTALL_HOST", "127.0.0.1")
INSTALL_PORT = int(os.environ.get("INSTALL_PORT", "8081"))
STATE_FILE = REPO_DIR / ".builder-state.json"
TEMPLATE_VERSION = 25

API_SLOT_LEN = 256
TOK_SLOT_LEN = 128
API_MARKER = b"__BRIDGE_API_URL__"
TOK_MARKER = b"__BRIDGE_TOKEN__"
_CYCLE_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()
_INSTALL_REQUESTS: OrderedDict[str, list[float]] = OrderedDict()
MAX_INSTALLS_PER_HOUR = 6
MAX_GLOBAL_INSTALLS_PER_HOUR = 30
MAX_INSTALL_CLIENTS = 8192
MAX_INSTALL_CLIENT_THREADS = 16
MAX_INSTALL_AIX_BYTES = 4 * 1024 * 1024
_INSTALL_LOCK = threading.Lock()
_INSTALL_REQUEST_SLOTS = threading.BoundedSemaphore(1)


def patch_wasm(api: str, token: str) -> None:
    """Same-length patch of the stock wasm config slots. Idempotent, runs at
    every startup (seconds); config changes apply on container recreate."""
    if len(api.encode()) > 200 or len(token.encode()) > 96:
        print("FATAL: API_URL (max 200B) or TOKEN (max 96B) too long", flush=True)
        sys.exit(1)
    try:
        d = bytearray(BRIDGE_STOCK.read_bytes())
    except Exception as e:
        print(f"FATAL: cannot read stock wasm: {e}", flush=True)
        sys.exit(1)

    def fill(marker: bytes, total: int, payload: bytes) -> bool:
        j = d.find(marker)
        if j < 4:
            return False
        i = j - 4
        if int.from_bytes(d[i:i + 4], "little") != len(marker):
            return False  # not our slot layout; refuse to touch
        if len(payload) + 4 > total:
            return False
        blob = len(payload).to_bytes(4, "little") + payload
        d[i:i + total] = blob + b"\0" * (total - len(blob))
        return True

    api_b = api.encode()
    if not fill(API_MARKER, API_SLOT_LEN, api_b):
        print("FATAL: API marker missing or malformed; refusing to publish an unconfigured WASM", flush=True)
        sys.exit(1)
    tok_xor = bytes(b ^ api_b[j % len(api_b)] for j, b in enumerate(token.encode())) if token else b""
    if not fill(TOK_MARKER, TOK_SLOT_LEN, tok_xor):
        print("FATAL: token marker missing or malformed; refusing to publish an unconfigured WASM", flush=True)
        sys.exit(1)
    BRIDGE_WASM.parent.mkdir(parents=True, exist_ok=True)
    BRIDGE_WASM.write_bytes(bytes(d))
    print(f"patched deployment wasm (api={api}, token={'set' if token else 'open'})", flush=True)

def suwayomi_auth() -> dict:
    if SUWAYOMI_USER:
        token = base64.b64encode(f"{SUWAYOMI_USER}:{SUWAYOMI_PASS}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def gql(query: str, variables: dict | None = None) -> dict:
    r = requests.post(
        f"{SUWAYOMI_URL}/api/graphql",
        json={"query": query, "variables": variables or {}},
        headers=suwayomi_auth(),
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "source"


def fetch_all_extensions() -> list[dict]:
    data = gql(
        "query { extensions(first: 5000) { nodes { pkgName name iconUrl versionName versionCode lang isInstalled hasUpdate } } }"
    )
    return data["extensions"]["nodes"]


def refresh_catalog() -> None:
    gql("mutation { fetchExtensions(input: {}) { extensions { pkgName } } }")


def install_or_update(pkg_names: list[str], update: bool = False) -> None:
    if not pkg_names:
        return
    patch = "update" if update else "install"
    gql(
        "mutation($ids: [String!]!) { updateExtensions(input: { ids: $ids, patch: {%s: true} }) { extensions { pkgName } } }"
        % patch,
        {"ids": pkg_names},
    )


def installed_sources() -> list[dict]:
    data = gql(
        'query { sources(first: 5000) { nodes { id name lang displayName homeUrl iconUrl extension { pkgName } } } }'
    )
    return [s for s in data["sources"]["nodes"] if s["id"] != "0"]


def want_lang(lang: str) -> bool:
    if "all" in LANGUAGES:
        return True
    return (lang or "").lower() in LANGUAGES


def make_icon(src_url: str) -> bytes:
    if src_url.startswith("/"):
        src_url = SUWAYOMI_URL + src_url
    r = requests.get(src_url, headers=suwayomi_auth(), timeout=60)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB").resize((128, 128), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def settings_json(source_id: str) -> str:
    # The deployment URL and token live in the generated WASM; Aidoku only needs the source ID.
    return json.dumps(
        [{
            "type": "group",
            "title": "Source",
            "footer": "Preconfigurado automáticamente, no tocar.",
            "items": [
                {"type": "text", "key": "sourceId", "title": "Suwayomi Source ID", "default": source_id},
            ],
        }],
        ensure_ascii=False,
    )

def source_json(bridge_id: str, name: str, site_url: str, lang: str, version: int) -> str:
    return json.dumps(
        {
            "info": {
                "id": bridge_id,
                "name": f"{name} (Bridge)",
                "version": version,
                "url": site_url,
                "contentRating": 0,
                "languages": [lang],
                "minAppVersion": "0.7.0",
            },
            "listings": [
                {"id": "popular", "name": "Popular", "kind": 0},
                {"id": "latest", "name": "Latest", "kind": 0},
            ],
            "config": {"supportsTagSearch": False, "breakingChangeVersion": version},
        },
        ensure_ascii=False,
    )


def build_aix(
    bridge_id: str,
    name: str,
    site_url: str,
    lang: str,
    version: int,
    source_id: str,
    icon_bytes: bytes,
    wasm_bytes: bytes,
) -> tuple[str, str]:
    """Returns (aix_filename, icon_filename)."""
    work = REPO_DIR / ".work" / bridge_id
    if work.exists():
        for p in sorted(work.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
    (work / "res").mkdir(parents=True, exist_ok=True)
    (work / "res" / "source.json").write_text(
        source_json(bridge_id, name, site_url, lang, version), encoding="utf-8"
    )
    (work / "res" / "settings.json").write_text(settings_json(source_id), encoding="utf-8")
    (work / "res" / "icon.png").write_bytes(icon_bytes)
    (work / "source.wasm").write_bytes(wasm_bytes)
    aix_name = f"{bridge_id}-v{version}.aix"
    aix_path = REPO_DIR / aix_name
    if aix_path.exists():
        aix_path.unlink()
    with zipfile.ZipFile(aix_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(work / "source.wasm", "Payload/main.wasm")
        z.write(work / "res" / "source.json", "Payload/source.json")
        z.write(work / "res" / "settings.json", "Payload/settings.json")
        z.write(work / "res" / "icon.png", "Payload/icon.png")
    icon_name = f"{bridge_id}-v{version}.png"
    (REPO_DIR / "icons").mkdir(exist_ok=True)
    (REPO_DIR / "icons" / icon_name).write_bytes(icon_bytes)
    return aix_name, f"icons/{icon_name}"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def site_url_for(source: dict) -> str:
    return (source.get("homeUrl") or "").strip().rstrip("/")


def catalog_bridge_id(ext: dict, occupied: set[str]) -> str:
    lang = (ext.get("lang") or "und").lower()
    stem = f"{lang}.{slugify(ext.get('name') or ext.get('pkgName') or 'extension')}"
    pkg_name = ext.get("pkgName") or ext.get("name") or "extension"
    suffix = hashlib.sha256(pkg_name.encode("utf-8")).hexdigest()[:6]
    candidate = f"{stem}-{suffix}-bridge"
    if candidate in occupied:
        candidate = f"{stem}-{hashlib.sha256(pkg_name.encode('utf-8')).hexdigest()[:10]}-bridge"
    return candidate


def catalog_icon(ext: dict, bridge_id: str, version: int) -> str:
    icon_name = f"{bridge_id}-catalog-v{version}.png"
    icon_path = REPO_DIR / "icons" / icon_name
    if not icon_path.exists():
        try:
            icon_bytes = make_icon(ext.get("iconUrl") or "")
        except Exception as e:
            print(f"WARN catalog icon {ext.get('pkgName')}: {e} (placeholder)", flush=True)
            icon_bytes = placeholder_icon()
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        icon_path.write_bytes(icon_bytes)
    return f"icons/{icon_name}"


def cycle() -> None:
    print("== cycle start ==", flush=True)
    wasm_bytes = BRIDGE_WASM.read_bytes()
    state = load_state()
    print("refreshing extension catalog...", flush=True)
    try:
        refresh_catalog()
    except Exception as e:
        print(f"WARN catalog refresh failed: {e}", flush=True)

    exts = fetch_all_extensions()
    by_pkg = {e["pkgName"]: e for e in exts}

    # Update only extensions the user has already installed.
    to_update = [e["pkgName"] for e in exts if e["isInstalled"] and e.get("hasUpdate")]
    if to_update:
        print(f"updating {len(to_update)} extensions...", flush=True)
        try:
            install_or_update(to_update, update=True)
        except Exception as e:
            print(f"WARN update failed: {e}", flush=True)
        exts = fetch_all_extensions()
        by_pkg = {e["pkgName"]: e for e in exts}

    # Build bridges for installed sources while retaining their existing IDs.
    sources = installed_sources()
    index_entries = []
    seen_ids: set[str] = set()
    live_aix: set[str] = set()
    package_ids = state.get("_package_ids", {})
    installed_packages: set[str] = set()
    for s in sources:
        lang = (s.get("lang") or "").lower()
        if not want_lang(lang):
            continue
        pkg = (s.get("extension") or {}).get("pkgName", "")
        installed_packages.add(pkg)
        ext = by_pkg.get(pkg, {})
        version = int(ext.get("versionCode") or 1) * 10 + TEMPLATE_VERSION
        mapped = package_ids.get(pkg, {})
        if mapped.get("sourceId") == s["id"] and mapped.get("bridge"):
            bridge_id = mapped["bridge"]
        else:
            base_slug = slugify(s.get("displayName") or s["name"])
            bridge_id = f"{lang}.{base_slug}-bridge"
        if bridge_id in seen_ids:
            base_slug = slugify(s.get("displayName") or s["name"])
            bridge_id = f"{lang}.{base_slug}-{s['id'][-6:]}-bridge"
        seen_ids.add(bridge_id)

        prev = state.get(s["id"], {})
        icon_bytes = None
        if prev.get("version") == version and prev.get("bridge") == bridge_id:
            aix_name = prev.get("aix")
            icon_rel = prev.get("icon")
            if aix_name and (REPO_DIR / aix_name).exists():
                index_entries.append(entry(bridge_id, s, version, aix_name, icon_rel, lang))
                live_aix.add(aix_name)
                continue
        # (re)build
        try:
            icon_bytes = make_icon(s.get("iconUrl") or "")
        except Exception as e:
            print(f"WARN icon {s['id']}: {e} (placeholder)", flush=True)
            icon_bytes = placeholder_icon()
        aix_name, icon_rel = build_aix(
            bridge_id,
            s.get("displayName") or s["name"],
            site_url_for(s),
            lang,
            version,
            s["id"],
            icon_bytes,
            wasm_bytes,
        )
        state[s["id"]] = {"bridge": bridge_id, "version": version, "aix": aix_name, "icon": icon_rel}
        live_aix.add(aix_name)
        index_entries.append(entry(bridge_id, s, version, aix_name, icon_rel, lang))
        print(f"built {bridge_id} v{version}", flush=True)

    # Advertise uninstalled extension packages without installing their APKs.
    # Aidoku's download URL installs only the selected, catalog-listed package.
    for ext in exts:
        pkg = ext.get("pkgName", "")
        lang = (ext.get("lang") or "").lower()
        if not pkg or pkg in installed_packages or not want_lang(lang):
            continue
        bridge_id = catalog_bridge_id(ext, seen_ids)
        seen_ids.add(bridge_id)
        version = int(ext.get("versionCode") or 1) * 10 + TEMPLATE_VERSION
        icon_rel = catalog_icon(ext, bridge_id, version)
        install_url = f"{STATIC_URL}/install/{quote(pkg, safe='')}.aix"
        index_entries.append(entry(bridge_id, ext, version, "", icon_rel, lang, download_url=install_url))

    # prune stale bridges (source gone / lang disabled)
    for f in REPO_DIR.glob("*.aix"):
        if f.name not in live_aix:
            f.unlink()
            print(f"pruned {f.name}", flush=True)

    index_entries.sort(key=lambda e: (e["languages"][0], e["name"].lower()))
    (REPO_DIR / "index.min.json").write_text(
        json.dumps({"name": "Suwayomi Bridges", "sources": index_entries}, ensure_ascii=False), encoding="utf-8"
    )
    live_icons = {
        item["iconURL"][len(STATIC_URL) + 1:]
        for item in index_entries
        if item.get("iconURL", "").startswith(STATIC_URL + "/")
    }
    for icon in (REPO_DIR / "icons").glob("*.png"):
        if str(icon.relative_to(REPO_DIR)).replace("\\", "/") not in live_icons:
            icon.unlink()
    save_state(state)
    print(f"== cycle done: {len(index_entries)} bridges ==", flush=True)


def entry(bridge_id: str, s: dict, version: int, aix: str, icon_rel: str, lang: str, download_url: str | None = None) -> dict:
    return {
        "id": bridge_id,
        "name": f"{s.get('displayName') or s['name']} (Bridge)",
        "version": version,
        "iconURL": f"{STATIC_URL}/{icon_rel}",
        "downloadURL": download_url or f"{STATIC_URL}/{aix}",
        "languages": [lang],
        "contentRating": 0,
        "minAppVersion": "0.7.0",
    }


def _request_client_key(handler: BaseHTTPRequestHandler) -> str:
    candidate = handler.headers.get("CF-Connecting-IP", "")
    if not candidate:
        candidate = handler.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    if candidate:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return handler.client_address[0]


def _allow_install_request(client_key: str) -> bool:
    now = time.monotonic()
    cutoff = now - 3600
    with _RATE_LOCK:
        client_bucket = f"client:{client_key}"
        global_bucket = "global"
        client_recent = [t for t in _INSTALL_REQUESTS.get(client_bucket, []) if t > cutoff]
        global_recent = [t for t in _INSTALL_REQUESTS.get(global_bucket, []) if t > cutoff]
        if len(client_recent) >= MAX_INSTALLS_PER_HOUR or len(global_recent) >= MAX_GLOBAL_INSTALLS_PER_HOUR:
            _INSTALL_REQUESTS[client_bucket] = client_recent
            _INSTALL_REQUESTS.move_to_end(client_bucket)
            _INSTALL_REQUESTS[global_bucket] = global_recent
            _INSTALL_REQUESTS.move_to_end(global_bucket)
            return False
        client_recent.append(now)
        global_recent.append(now)
        _INSTALL_REQUESTS[client_bucket] = client_recent
        _INSTALL_REQUESTS.move_to_end(client_bucket)
        _INSTALL_REQUESTS[global_bucket] = global_recent
        _INSTALL_REQUESTS.move_to_end(global_bucket)
        while len(_INSTALL_REQUESTS) > MAX_INSTALL_CLIENTS:
            _INSTALL_REQUESTS.popitem(last=False)
        return True


def _install_catalog_package(pkg_name: str) -> bytes:
    """Install only a currently catalog-listed package and return its first source AIX."""
    with _INSTALL_LOCK, _CYCLE_LOCK:
        refresh_catalog()
        extensions = fetch_all_extensions()
        ext = next((e for e in extensions if e.get("pkgName") == pkg_name), None)
        if not ext or not want_lang(ext.get("lang") or ""):
            raise KeyError("package is not in the enabled extension catalog")

        if not ext.get("isInstalled"):
            install_or_update([pkg_name], update=False)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                extensions = fetch_all_extensions()
                ext = next((e for e in extensions if e.get("pkgName") == pkg_name), None)
                sources = [
                    s for s in installed_sources()
                    if (s.get("extension") or {}).get("pkgName") == pkg_name
                ]
                if ext and ext.get("isInstalled") and sources:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Suwayomi did not finish installing the selected extension")
        else:
            sources = [
                s for s in installed_sources()
                if (s.get("extension") or {}).get("pkgName") == pkg_name
            ]
        if not sources:
            raise RuntimeError("the selected extension exposes no installed sources")

        sources.sort(key=lambda s: (s.get("name") or "").lower() + str(s.get("id", "")))
        source = sources[0]
        state = load_state()
        package_ids = state.setdefault("_package_ids", {})
        mapping = package_ids.get(pkg_name)
        if mapping and any(s.get("id") == mapping.get("sourceId") for s in sources):
            source = next(s for s in sources if s.get("id") == mapping["sourceId"])
            bridge_id = mapping.get("bridge") or catalog_bridge_id(ext, set())
        else:
            occupied = {
                str(value.get("bridge"))
                for key, value in state.items()
                if key != "_package_ids" and isinstance(value, dict)
            }
            # If the package was already installed before this builder version,
            # keep the bridge ID it has already published instead of turning a
            # package download into a source-ID change for Aidoku.
            existing = state.get(source["id"], {})
            bridge_id = existing.get("bridge") or catalog_bridge_id(ext, occupied)
            package_ids[pkg_name] = {"sourceId": source["id"], "bridge": bridge_id}
        save_state(state)

        # cycle() publishes metadata for all newly visible sources and uses the
        # saved package mapping to keep this first package ID stable.
        cycle()
        state = load_state()
        built = state.get(source["id"], {}).get("aix")
        if not built or not (REPO_DIR / built).is_file():
            raise RuntimeError("builder did not create the selected Aidoku package")
        return (REPO_DIR / built).read_bytes()


class InstallHandler(BaseHTTPRequestHandler):
    server_version = "bridge-installer"

    def log_message(self, fmt, *args):
        print("install endpoint request handled", flush=True)

    def _respond(self, status: int, body: bytes, content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        match = re.fullmatch(r"/install/([A-Za-z0-9_.-]{1,200})\.aix", self.path)
        if not match:
            return self._respond(404, b'{"error":"not found"}')
        if not _allow_install_request(_request_client_key(self)):
            return self._respond(429, b'{"error":"installation rate limit reached"}')
        if not _INSTALL_REQUEST_SLOTS.acquire(blocking=False):
            return self._respond(503, b'{"error":"an installation is already running"}')
        try:
            package = _install_catalog_package(match.group(1))
            if len(package) > MAX_INSTALL_AIX_BYTES:
                raise RuntimeError("generated package exceeds the response size limit")
        except KeyError:
            return self._respond(404, b'{"error":"package is not available"}')
        except Exception as e:
            print(f"WARN on-demand install failed: {type(e).__name__}: {e}", flush=True)
            return self._respond(502, b'{"error":"could not build selected source"}')
        finally:
            _INSTALL_REQUEST_SLOTS.release()
        return self._respond(200, package, "application/octet-stream")

    def do_POST(self):
        return self._respond(405, b'{"error":"method not allowed"}')


class BoundedInstallHTTPServer(ThreadingHTTPServer):
    """Bound unauthenticated installer sockets before creating handler threads."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address, request_handler_class):
        super().__init__(server_address, request_handler_class)
        self._client_slots = threading.BoundedSemaphore(MAX_INSTALL_CLIENT_THREADS)

    def process_request(self, request, client_address):
        if not self._client_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._client_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_slots.release()


def start_install_server() -> None:
    server = BoundedInstallHTTPServer((INSTALL_HOST, INSTALL_PORT), InstallHandler)
    threading.Thread(target=server.serve_forever, name="aidoku-install-server", daemon=True).start()
    print(f"on-demand package endpoint listening on {INSTALL_HOST}:{INSTALL_PORT}", flush=True)


def placeholder_icon() -> bytes:
    img = Image.new("RGB", (128, 128), (124, 92, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    import sys

    REPO_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_DIR / "icons").mkdir(exist_ok=True)
    api = API_BASE_URL
    if not STATIC_URL.startswith("https://") or not api.startswith("https://"):
        print("FATAL: STATIC_URL and API_URL must use HTTPS", flush=True)
        sys.exit(1)
    if len(TOKEN) < 32 or TOKEN != GATEWAY_TOKEN:
        print("FATAL: TOKEN and GATEWAY_TOKEN must match and contain at least 32 characters", flush=True)
        sys.exit(1)
    patch_wasm(api, TOKEN)
    if not BRIDGE_WASM.exists() or BRIDGE_WASM.stat().st_size == 0:
        print(f"FATAL: missing template wasm at {BRIDGE_WASM}", flush=True)
        sys.exit(1)
    if BRIDGE_WASM.read_bytes()[:4] != b"\0asm":
        print(f"FATAL: {BRIDGE_WASM} is not a wasm binary", flush=True)
        sys.exit(1)
    missing = [k for k, v in {
        "STATIC_URL": STATIC_URL,
    }.items() if not v]
    if bool(SUWAYOMI_USER) != bool(SUWAYOMI_PASS):
        missing.append("SUWAYOMI_USER + SUWAYOMI_PASS (both or neither)")
    if missing:
        print(f"FATAL: missing required env: {', '.join(missing)}", flush=True)
        sys.exit(1)
    start_install_server()
    interval = POLL_SECONDS
    while True:
        try:
            with _CYCLE_LOCK:
                cycle()
        except Exception as e:
            print(f"ERROR cycle: {e}", flush=True)
        print(f"sleep {interval}s", flush=True)
        time.sleep(interval)
