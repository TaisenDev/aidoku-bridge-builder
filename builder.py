#!/usr/bin/env python3
"""bridge-builder: generate Aidoku .aix bridges (1 per Suwayomi source) + repo index.

Reads installed extensions from Suwayomi via GraphQL, auto-installs the ones
matching LANGUAGES, applies pending updates, and per source generates:
  repo/<bridge>.aix  (= shared bridge.wasm + generated res/ + opaque 128 icon)
  repo/index.min.json (Aidoku-style source list) + icons/

WASM resolution (first hit wins):
  1. $BRIDGE_WASM when it points to an existing non-empty file (user override)
  2. /wasm/bridge.wasm baked into the image from a pinned template release

Env:
  SUWAYOMI_URL      internal URL, e.g. http://suwayomi:4567
  SUWAYOMI_USER / SUWAYOMI_PASS
                    basic-auth credentials for SUWAYOMI_URL. Required when the
                    server runs with AUTH_MODE=BASIC_AUTH (both or neither).
  PUBLIC_SUWAYOMI_URL
                    public URL baked into settings, e.g. https://suwayomi.example.com (required)
  PUBLIC_REPO_BASE  e.g. https://aidoku.example.com for iconURL/downloadURL (required)
  CADDY_USER        reverse-proxy basic-auth username, baked (required)
  BAKE_CREDENTIALS  "true" to also bake the password (CADDY_PASS). Default false.
                    Baked passwords are OBFUSCATED (obf1:), not encrypted: anyone
                    holding the .aix plus the public template source can recover
                    them. Only enable this if you accept that. The gateway
                    (password never baked) is the real fix.
  CADDY_PASS        only used when BAKE_CREDENTIALS=true
  LANGUAGES         "es" (default) | "es,en" | "all" ... Only matching sources
                    get bridges. Extensions for other languages are left
                    untouched on the server.
  BRIDGE_WASM       override path to a user-supplied bridge.wasm template.
                    Default /wasm/bridge.wasm (image-bundled, pinned release).
  REPO_DIR          output dir. Default /repo
  POLL_SECONDS      Default 3600.
"""

import base64
import hashlib
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import requests
from PIL import Image

SUWAYOMI_URL = os.environ.get("SUWAYOMI_URL", "http://suwayomi:4567")
SUWAYOMI_USER = os.environ.get("SUWAYOMI_USER", "")
SUWAYOMI_PASS = os.environ.get("SUWAYOMI_PASS", "")
PUBLIC_SUWAYOMI_URL = os.environ.get("PUBLIC_SUWAYOMI_URL", "").rstrip("/")
PUBLIC_REPO_BASE = os.environ.get("PUBLIC_REPO_BASE", "").rstrip("/")
CADDY_USER = os.environ.get("CADDY_USER", "")
BAKE_CREDENTIALS = os.environ.get("BAKE_CREDENTIALS", "false").lower() in ("1", "true", "yes")
CADDY_PASS = os.environ.get("CADDY_PASS", "")
LANGUAGES = [l.strip().lower() for l in os.environ.get("LANGUAGES", "es").split(",") if l.strip()]
BRIDGE_WASM = Path(os.environ.get("BRIDGE_WASM", "/wasm/bridge.wasm"))
REPO_DIR = Path(os.environ.get("REPO_DIR", "/repo"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "3600"))
STATE_FILE = REPO_DIR / ".builder-state.json"
TEMPLATE_VERSION = 4

OBF_TAG = "obf1:"
OBF_SALT = "taisendev-obf1"


def obf_nonce() -> str:
    seed = f"{PUBLIC_SUWAYOMI_URL}:{CADDY_USER}:{OBF_SALT}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def obf_encode(secret: str) -> str:
    nonce = obf_nonce()
    key = hashlib.sha256(f"{nonce}:{PUBLIC_SUWAYOMI_URL}:{CADDY_USER}:{OBF_SALT}".encode()).digest()
    raw = secret.encode()
    x = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return f"{OBF_TAG}{nonce}.{base64.b64encode(x).decode()}"


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
        "query { extensions(first: 5000) { nodes { pkgName name versionName versionCode lang isInstalled hasUpdate } } }"
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
        'query { sources(first: 5000) { nodes { id name lang displayName iconUrl extension { pkgName } } } }'
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
    pw_item = (
        '\n      { "type": "text", "key": "password", "title": "Password", "placeholder": "pass", "secure": true },'
        if not BAKE_CREDENTIALS
        else f'\n      {{ "type": "text", "key": "password", "title": "Password", "default": "{obf_encode(CADDY_PASS)}", "secure": true }},'
    )
    return (
        '[\n  { "type": "group", "title": "Server", "footer": "Preconfigurado automáticamente, no tocar.", "items": [\n'
        f'    {{ "type": "text", "key": "serverUrl", "title": "Suwayomi Server URL", "default": "{PUBLIC_SUWAYOMI_URL}" }}\n'
        "  ]},\n"
        '  { "type": "group", "title": "Auth (Caddy Basic)", "footer": "Si cambias servidor o usuario, reescribe la contraseña.", "items": [\n'
        f'    {{ "type": "text", "key": "username", "title": "Username", "default": "{CADDY_USER}" }},'
        f"{pw_item}\n"
        "  ]},\n"
        '  { "type": "group", "title": "Source", "footer": "Preconfigurado automáticamente, no tocar.", "items": [\n'
        f'    {{ "type": "text", "key": "sourceId", "title": "Suwayomi Source ID", "default": "{source_id}" }}\n'
        "  ]}\n]"
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
            "config": {"supportsTagSearch": False},
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
    return PUBLIC_SUWAYOMI_URL


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

    # 1. auto-install missing extensions for wanted languages (bounded per cycle)
    to_install = [
        e["pkgName"]
        for e in exts
        if not e["isInstalled"] and want_lang(e.get("lang") or "")
    ]
    if to_install:
        print(f"installing {len(to_install)} extensions...", flush=True)
        for i in range(0, len(to_install), 10):
            batch = to_install[i : i + 10]
            try:
                install_or_update(batch, update=False)
                print(f"  installed batch {i // 10 + 1}", flush=True)
            except Exception as e:
                print(f"  WARN batch failed: {e}", flush=True)

    # 2. update installed ones with updates available
    to_update = [e["pkgName"] for e in exts if e["isInstalled"] and e.get("hasUpdate")]
    if to_update:
        print(f"updating {len(to_update)} extensions...", flush=True)
        try:
            install_or_update(to_update, update=True)
        except Exception as e:
            print(f"WARN update failed: {e}", flush=True)
        exts = fetch_all_extensions()
        by_pkg = {e["pkgName"]: e for e in exts}

    # 3. bridges per installed source (wanted langs)
    sources = installed_sources()
    index_entries = []
    seen_ids: set[str] = set()
    live_aix: set[str] = set()
    for s in sources:
        lang = (s.get("lang") or "").lower()
        if not want_lang(lang):
            continue
        pkg = (s.get("extension") or {}).get("pkgName", "")
        ext = by_pkg.get(pkg, {})
        version = int(ext.get("versionCode") or 1) * 10 + TEMPLATE_VERSION
        base_slug = slugify(s.get("displayName") or s["name"])
        bridge_id = f"{lang}.{base_slug}-bridge"
        if bridge_id in seen_ids:
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

    # prune stale bridges (source gone / lang disabled)
    for f in REPO_DIR.glob("*.aix"):
        if f.name not in live_aix:
            f.unlink()
            print(f"pruned {f.name}", flush=True)

    index_entries.sort(key=lambda e: (e["languages"][0], e["name"].lower()))
    (REPO_DIR / "index.min.json").write_text(
        json.dumps({"name": "Suwayomi Bridges", "sources": index_entries}, ensure_ascii=False), encoding="utf-8"
    )
    save_state(state)
    print(f"== cycle done: {len(index_entries)} bridges ==", flush=True)


def entry(bridge_id: str, s: dict, version: int, aix: str, icon_rel: str, lang: str) -> dict:
    return {
        "id": bridge_id,
        "name": f"{s.get('displayName') or s['name']} (Bridge)",
        "version": version,
        "iconURL": f"{PUBLIC_REPO_BASE}/{icon_rel}" if PUBLIC_REPO_BASE else icon_rel,
        "downloadURL": f"{PUBLIC_REPO_BASE}/{aix}" if PUBLIC_REPO_BASE else aix,
        "languages": [lang],
        "contentRating": 0,
        "minAppVersion": "0.7.0",
    }


def placeholder_icon() -> bytes:
    img = Image.new("RGB", (128, 128), (124, 92, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    import sys

    REPO_DIR.mkdir(parents=True, exist_ok=True)
    (REPO_DIR / "icons").mkdir(exist_ok=True)
    if not BRIDGE_WASM.exists() or BRIDGE_WASM.stat().st_size == 0:
        print(f"FATAL: missing template wasm at {BRIDGE_WASM}", flush=True)
        sys.exit(1)
    if BRIDGE_WASM.read_bytes()[:4] != b"\0asm":
        print(f"FATAL: {BRIDGE_WASM} is not a wasm binary", flush=True)
        sys.exit(1)
    missing = [k for k, v in {
        "PUBLIC_SUWAYOMI_URL": PUBLIC_SUWAYOMI_URL,
        "PUBLIC_REPO_BASE": PUBLIC_REPO_BASE,
        "CADDY_USER": CADDY_USER,
    }.items() if not v]
    if BAKE_CREDENTIALS and not CADDY_PASS:
        missing.append("CADDY_PASS (required when BAKE_CREDENTIALS=true)")
    if bool(SUWAYOMI_USER) != bool(SUWAYOMI_PASS):
        missing.append("SUWAYOMI_USER + SUWAYOMI_PASS (both or neither)")
    if missing:
        print(f"FATAL: missing required env: {', '.join(missing)}", flush=True)
        sys.exit(1)
    if not PUBLIC_REPO_BASE:
        print("WARN: PUBLIC_REPO_BASE empty -> relative URLs in index", flush=True)
    interval = POLL_SECONDS
    while True:
        try:
            cycle()
        except Exception as e:
            print(f"ERROR cycle: {e}", flush=True)
        print(f"sleep {interval}s", flush=True)
        time.sleep(interval)
