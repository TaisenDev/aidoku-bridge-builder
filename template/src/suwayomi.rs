use aidoku::{
    alloc::{String, Vec, format},
    imports::net::Request,
    prelude::*,
    Result,
};
use serde::Deserialize;

#[derive(Deserialize)]
struct Gql<T> { data: T }

#[derive(Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct SmManga { pub id: i32, pub title: String, pub thumbnail_url: Option<String>,
    pub description: Option<String>, pub author: Option<String>, pub artist: Option<String>,
    pub genre: Option<Vec<String>>, pub status: Option<String>, pub real_url: Option<String>, pub url: Option<String> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmList { pub fetch_source_manga: SmListPayload }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmListPayload { pub mangas: Vec<SmManga>, pub has_next_page: Option<bool> }
#[derive(Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct SmCh { pub id: i32, pub name: String, pub chapter_number: Option<f32>,
    pub upload_date: Option<String>, pub scanlator: Option<String>, pub real_url: Option<String> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmChs { pub fetch_chapters: SmChsPayload }
#[derive(Deserialize)]
pub struct SmChsPayload { pub chapters: Vec<SmCh> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmPages { pub fetch_chapter_pages: SmPagesPayload }
#[derive(Deserialize)]
pub struct SmPagesPayload { pub pages: Vec<String> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AddByUrl { pub add_manga_from_url: AddByUrlPayload }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AddByUrlPayload { pub status: Option<String>, pub message: Option<String>,
    pub installed_extension_pkg_name: Option<String>,
    pub manga: Option<SmManga> }

include!(concat!(env!("OUT_DIR"), "/bridge_cfg.rs"));

fn read_slot<const N: usize>(slot: &[u8; N]) -> Option<Vec<u8>> {
    if N < 4 {
        return None;
    }
    // The builder patches these bytes after compilation. Volatile loads stop
    // LLVM from folding the immutable static's stock placeholder into code.
    let mut header = [0u8; 4];
    for (i, byte) in header.iter_mut().enumerate() {
        *byte = unsafe { core::ptr::read_volatile(slot.as_ptr().add(i)) };
    }
    let n = u32::from_le_bytes(header) as usize;
    if n > N - 4 {
        return None;
    }
    let mut value = Vec::with_capacity(n);
    for i in 0..n {
        value.push(unsafe { core::ptr::read_volatile(slot.as_ptr().add(4 + i)) });
    }
    Some(value)
}

fn is_marker(v: &[u8]) -> bool {
    v.starts_with(b"__BRIDGE_")
}

pub fn api_base() -> core::result::Result<String, &'static str> {
    let raw = read_slot(&BRIDGE_API_SLOT).ok_or("API slot invalid")?;
    if raw.is_empty() || is_marker(&raw) {
        return Err("API slot is empty or still a placeholder");
    }
    String::from_utf8(raw).map_err(|_| "API slot is not UTF-8")
}

/// Deployment token (XORed with the API bytes, same scheme at compile and
/// patch time). Errors identify only the failed stage, never the value.
pub fn token(api: &str) -> core::result::Result<String, &'static str> {
    let raw = read_slot(&BRIDGE_TOK_SLOT).ok_or("token slot invalid")?;
    if raw.is_empty() {
        return Err("token slot is empty");
    }
    if is_marker(&raw) {
        return Err("token slot is still a placeholder");
    }
    let kb = api.as_bytes();
    if kb.is_empty() {
        return Err("embedded API URL is empty");
    }
    let clr: Vec<u8> = raw.iter().enumerate().map(|(j, &x)| x ^ kb[j % kb.len()]).collect();
    let token = String::from_utf8(clr).map_err(|_| "decoded token is not UTF-8")?;
    if token.is_empty() {
        return Err("decoded token is empty");
    }
    Ok(token)
}

pub fn token_value(tok: &Option<String>) -> Option<&str> {
    tok.as_deref().filter(|s| !s.is_empty())
}

fn gql<T: serde::de::DeserializeOwned>(base: &str, tok: &Option<String>, query: &str) -> Result<T> {
    #[derive(serde::Serialize)]
    struct Body<'a> { query: &'a str }
    let url = format!("{}/api/graphql", base);
    let body = serde_json::to_vec(&Body { query }).map_err(|e| error!("json: {:?}", e))?;
    let mut req = Request::post(&url).map_err(|e| error!("req: {:?}", e))?;
    req.set_header("Content-Type", "application/json");
    req.set_header("Accept", "application/json");
    if let Some(t) = token_value(tok) { req.set_header("X-Bridge-Token", t); }
    req.set_body(&body);
    let g: Gql<T> = req.send().map_err(|e| error!("send: {:?}", e))?
        .get_json_owned().map_err(|e| error!("parse: {:?}", e))?;
    Ok(g.data)
}

pub fn esc(s: &str) -> String {
    let mut o = String::from(s);
    o = o.replace('\\', "");
    o = o.replace('"', "");
    o
}

pub fn fetch_source(base: &str, tok: &Option<String>, src: &str, ty: &str, q: Option<&str>, page: i32, filters: &[String]) -> Result<SmListPayload> {
    let qs = q.map(|s| format!(r#"query: "{}""#, esc(s))).unwrap_or_default();
    let fs = if filters.is_empty() { String::new() } else { format!(r#",filters: [{}]"#, filters.join(",")) };
    let query = format!(r#"mutation {{ fetchSourceManga(input: {{ source: "{src}", type: {ty}, {qs} page: {page}{fs} }}) {{ mangas {{ id title thumbnailUrl description author artist genre status realUrl url }} hasNextPage }} }}"#);
    gql::<SmList>(base, tok, &query).map(|d| d.fetch_source_manga)
}

pub fn fetch_chapters(base: &str, tok: &Option<String>, manga_id: i32) -> Result<Vec<SmCh>> {
    let q = format!(r#"mutation {{ fetchChapters(input: {{ mangaId: {manga_id} }}) {{ chapters {{ id name chapterNumber uploadDate scanlator realUrl }} }} }}"#);
    gql::<SmChs>(base, tok, &q).map(|d| d.fetch_chapters.chapters)
}

pub fn fetch_pages(base: &str, tok: &Option<String>, chapter_id: i32) -> Result<Vec<String>> {
    let q = format!(r#"mutation {{ fetchChapterPages(input: {{ chapterId: {chapter_id} }}) {{ pages }} }}"#);
    gql::<SmPages>(base, tok, &q).map(|d| d.fetch_chapter_pages.pages)
}

pub fn add_by_url(base: &str, tok: &Option<String>, url: &str) -> Result<AddByUrlPayload> {
    let q = format!(r#"mutation {{ addMangaFromUrl(input: {{ url: "{}", autoInstallExtension: true, addToLibrary: false }}) {{ status message installedExtensionPkgName manga {{ id title url }} }} }}"#, esc(url));
    gql::<AddByUrl>(base, tok, &q).map(|d| d.add_manga_from_url)
}

pub fn abs(base: &str, path: &str) -> String {
    if path.starts_with("http://") || path.starts_with("https://") { String::from(path) }
    else { format!("{base}{path}") }
}

pub fn manga_key(src: &str, m: &SmManga) -> String {
    let url = m.real_url.clone().or(m.url.clone()).unwrap_or_default();
    if url.starts_with("http://") || url.starts_with("https://") {
        format!("sm|{src}|{}|{url}", m.id)
    } else {
        format!("{}", m.id)
    }
}

fn split_key(key: &str) -> Option<(&str, i32, &str)> {
    let rest = key.strip_prefix("sm|")?;
    let (s, rest) = rest.split_once('|')?;
    let (id_s, url) = rest.split_once('|')?;
    if s.is_empty() || url.is_empty() { return None; }
    Some((s, id_s.parse::<i32>().ok()?, url))
}

pub fn resolve_manga_id(base: &str, tok: &Option<String>, src: &str, key: &str) -> Result<i32> {
    if let Ok(id) = key.parse::<i32>() { return Ok(id); }
    let (s, row_id, url) = split_key(key).ok_or(error!("bad manga key"))?;
    if s != src { return Err(error!("manga key mismatch")); }
    if let Ok(d) = fetch_manga_details(base, tok, row_id) {
        let live = d.real_url.as_deref().or(d.url.as_deref()).unwrap_or("");
        if live == url { return Ok(row_id); }
    }
    let r = add_by_url(base, tok, url)?;
    r.manga.map(|m| m.id).ok_or(error!("could not resolve manga"))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmFull { pub fetch_manga_and_chapters: SmFullPayload }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmFullPayload { pub manga: SmManga }

pub fn fetch_manga_details(base: &str, tok: &Option<String>, manga_id: i32) -> Result<SmManga> {
    let q = format!(r#"mutation {{ fetchMangaAndChapters(input: {{ id: {manga_id}, fetchManga: true, fetchChapters: false }}) {{ manga {{ id title thumbnailUrl description author artist genre status realUrl url }} }} }}"#);
    gql::<SmFull>(base, tok, &q).map(|d| d.fetch_manga_and_chapters.manga)
}

#[derive(Deserialize)]
#[serde(tag = "__typename")]
pub enum SwFilter {
    CheckBoxFilter { name: String, #[serde(default)] cbDefault: bool },
    SelectFilter { name: String, #[serde(default)] selDefault: i32, #[serde(default)] values: Vec<String> },
    TextFilter { name: String },
    #[serde(other)] Other,
}

#[derive(Deserialize)]
pub struct SwFilters { pub source: SwFiltersPayload }
#[derive(Deserialize)]
pub struct SwFiltersPayload { pub filters: Vec<SwFilter> }

pub fn fetch_filters(base: &str, tok: &Option<String>, src: &str) -> Result<Vec<SwFilter>> {
    let q = format!(r#"query {{ source(id: "{src}") {{ filters {{ __typename ... on CheckBoxFilter {{ name cbDefault: default }} ... on SelectFilter {{ name selDefault: default values }} ... on TextFilter {{ name }} }} }} }}"#);
    gql::<SwFilters>(base, tok, &q).map(|d| d.source.filters)
}
