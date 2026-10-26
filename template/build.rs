use std::io::Write;

// Deployment config slots. When BRIDGE_API_URL is unset the API slot holds
// a padded marker instead of failing: the stock binary is meant to be
// patched per deployment (same-length overwrite, markers located by the
// patcher), never configured by hand.
const API_LEN: usize = 256;
const TOK_LEN: usize = 128;
const API_MARKER: &str = "__BRIDGE_API_URL__";
const TOK_MARKER: &str = "__BRIDGE_TOKEN__";

// Slots are length-prefixed ([len:u32 LE][payload][zero pad]) so XORed bytes
// can never truncate the value. Unset slots hold the marker as payload.
fn slot(value: &[u8], marker: &str, len: usize) -> Vec<u8> {
    let mut out = vec![0u8; len];
    let src = if value.is_empty() { marker.as_bytes() } else { value };
    assert!(src.len() + 4 <= len, "config value too long for wasm slot");
    out[..4].copy_from_slice(&(src.len() as u32).to_le_bytes());
    out[4..4 + src.len()].copy_from_slice(src);
    out
}

fn main() {
    println!("cargo:rerun-if-env-changed=BRIDGE_API_URL");
    println!("cargo:rerun-if-env-changed=BRIDGE_TOKEN");
    let api = std::env::var("BRIDGE_API_URL").unwrap_or_default();
    let tok = std::env::var("BRIDGE_TOKEN").unwrap_or_default();
    let api_slot = slot(api.trim().as_bytes(), API_MARKER, API_LEN);
    // Token stored XORed with the API bytes (same scheme the patcher uses,
    // so compiled and patched values decode through one runtime path).
    let salt = if api.trim().is_empty() { API_MARKER.as_bytes() } else { api.trim().as_bytes() };
    let xored: Vec<u8> = tok
        .as_bytes()
        .iter()
        .enumerate()
        .map(|(i, &b)| b ^ salt[i % salt.len()])
        .collect();
    let tok_slot = slot(&xored, TOK_MARKER, TOK_LEN);
    let fmt = |v: Vec<u8>| v.iter().map(|b| b.to_string()).collect::<Vec<_>>().join(",");
    let out = std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("bridge_cfg.rs");
    let mut f = std::fs::File::create(out).unwrap();
    writeln!(f, "pub const BRIDGE_API_SLOT: &[u8; {API_LEN}] = &[{}];", fmt(api_slot)).unwrap();
    writeln!(f, "pub const BRIDGE_TOK_SLOT: &[u8; {TOK_LEN}] = &[{}];", fmt(tok_slot)).unwrap();
    println!("cargo:rerun-if-changed=build.rs");
}
