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
pub struct SmList { pub bridge_fetch_source_manga: SmListPayload }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmListPayload { pub mangas: Vec<SmManga>, pub has_next_page: Option<bool> }
#[derive(Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct SmCh { pub id: i32, pub name: String, pub chapter_number: Option<f32>,
    pub upload_date: Option<String>, pub scanlator: Option<String>, pub real_url: Option<String>, pub url: Option<String> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmFull { pub bridge_fetch_manga_and_chapters: SmFullPayload }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmFullPayload { pub manga: SmManga, pub chapters: Vec<SmCh> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmPages { pub bridge_fetch_chapter_pages: SmPagesPayload }
#[derive(Deserialize)]
pub struct SmPagesPayload { pub pages: Vec<String> }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmResolve { pub bridge_resolve_url: SmResolvePayload }
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SmResolvePayload { pub status: String, pub message: Option<String>, pub manga: Option<SmManga> }

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
    String::from(s)
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', "\\n")
        .replace('\r', "\\r")
}

pub fn fetch_source(base: &str, tok: &Option<String>, src: &str, ty: &str, q: Option<&str>, page: i32, filters: &[String]) -> Result<SmListPayload> {
    let qs = q.map(|s| format!(r#"query: "{}""#, esc(s))).unwrap_or_default();
    let fs = if filters.is_empty() { String::new() } else { format!(r#",filters: [{}]"#, filters.join(",")) };
    let query = format!(r#"mutation {{ bridgeFetchSourceManga(input: {{ source: "{src}", type: {ty}, {qs} page: {page}{fs} }}) {{ mangas {{ id title thumbnailUrl description author artist genre status realUrl url }} hasNextPage }} }}"#);
    gql::<SmList>(base, tok, &query).map(|d| d.bridge_fetch_source_manga)
}

pub fn resolve_url(base: &str, tok: &Option<String>, url: &str) -> Result<SmManga> {
    let q = format!(r#"mutation {{ bridgeResolveUrl(url: "{}") {{ status message manga {{ id title thumbnailUrl description author artist genre status realUrl url }} }} }}"#, esc(url));
    let result = gql::<SmResolve>(base, tok, &q)?.bridge_resolve_url;
    if result.status != "FOUND" {
        return Err(error!("URL lookup failed: {}", result.message.unwrap_or(result.status)));
    }
    result.manga.ok_or(error!("URL lookup returned no manga"))
}

pub fn fetch_manga_and_chapters(base: &str, tok: &Option<String>, src: &str, url: &str, fetch_manga: bool, fetch_chapters: bool, title: Option<&str>, author: Option<&str>, artist: Option<&str>, description: Option<&str>) -> Result<SmFullPayload> {
    let title = title.map(|s| format!(r#",title:"{}""#, esc(s))).unwrap_or_default();
    let author = author.map(|s| format!(r#",author:"{}""#, esc(s))).unwrap_or_default();
    let artist = artist.map(|s| format!(r#",artist:"{}""#, esc(s))).unwrap_or_default();
    let description = description.map(|s| format!(r#",description:"{}""#, esc(s))).unwrap_or_default();
    let q = format!(r#"mutation {{ bridgeFetchMangaAndChapters(input: {{ source: "{src}", url: "{}"{title}{author}{artist}{description}, fetchManga: {fetch_manga}, fetchChapters: {fetch_chapters} }}) {{ manga {{ id title thumbnailUrl description author artist genre status realUrl url }} chapters {{ id name chapterNumber uploadDate scanlator realUrl url }} }} }}"#, esc(url));
    gql::<SmFull>(base, tok, &q).map(|d| d.bridge_fetch_manga_and_chapters)
}

pub fn fetch_chapters(base: &str, tok: &Option<String>, src: &str, manga_url: &str) -> Result<Vec<SmCh>> {
    fetch_manga_and_chapters(base, tok, src, manga_url, false, true, None, None, None, None).map(|d| d.chapters)
}

pub fn fetch_manga_details(base: &str, tok: &Option<String>, src: &str, manga_url: &str) -> Result<SmManga> {
    fetch_manga_and_chapters(base, tok, src, manga_url, true, false, None, None, None, None).map(|d| d.manga)
}

pub fn fetch_pages(base: &str, tok: &Option<String>, src: &str, manga_url: &str, chapter_url: &str, chapter_name: Option<&str>, chapter_number: Option<f32>, scanlator: Option<&str>) -> Result<Vec<String>> {
    let name = chapter_name.map(|s| format!(r#",chapterName:"{}""#, esc(s))).unwrap_or_default();
    let number = chapter_number.map(|n| format!(r#",chapterNumber:{n}"#)).unwrap_or_default();
    let scan = scanlator.map(|s| format!(r#",scanlator:"{}""#, esc(s))).unwrap_or_default();
    let q = format!(r#"mutation {{ bridgeFetchChapterPages(input: {{ source: "{src}", mangaUrl: "{}", chapterUrl: "{}"{name}{number}{scan} }}) {{ pages }} }}"#, esc(manga_url), esc(chapter_url));
    gql::<SmPages>(base, tok, &q).map(|d| d.bridge_fetch_chapter_pages.pages)
}

pub fn abs(base: &str, path: &str) -> String {
    if path.starts_with("http://") || path.starts_with("https://") { String::from(path) }
    else { format!("{base}{path}") }
}

pub fn media_url(base: &str, handle: &str) -> String {
    format!("{}/api/v1/bridge/media/{}", base.trim_end_matches('/'), handle)
}

pub fn manga_key(src: &str, m: &SmManga) -> String {
    let url = m.real_url.clone().or(m.url.clone()).unwrap_or_default();
    if url.starts_with("http://") || url.starts_with("https://") {
        format!("sm|{src}|0|{url}")
    } else {
        format!("{}", m.id)
    }
}

pub fn split_key(key: &str) -> Option<(&str, i32, &str)> {
    let rest = key.strip_prefix("sm|")?;
    let (s, rest) = rest.split_once('|')?;
    let (id_s, url) = rest.split_once('|')?;
    if s.is_empty() || url.is_empty() { return None; }
    Some((s, id_s.parse::<i32>().ok()?, url))
}

pub fn manga_url(src: &str, key: &str, fallback: Option<&str>) -> Result<String> {
    if let Some((key_source, _, url)) = split_key(key) {
        if key_source != src { return Err(error!("manga key source mismatch")); }
        return Ok(String::from(url));
    }
    if let Some(url) = fallback.map(str::trim).filter(|u| u.starts_with("http://") || u.starts_with("https://")) {
        return Ok(String::from(url));
    }
    Err(error!("saved manga has no source URL; reopen it from the source and refresh its local entry"))
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
