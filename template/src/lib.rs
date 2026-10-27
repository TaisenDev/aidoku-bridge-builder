#![no_std]
use aidoku::{alloc::{String, Vec, format, borrow::Cow}, imports::{defaults::defaults_get, net::Request},
    prelude::*, Chapter, Filter, FilterKind, FilterValue, Listing, ListingProvider, Manga, MangaPageResult, MangaStatus,
    Page, PageContent, Result, Source, DynamicFilters, ImageRequestProvider, MigrationHandler};

mod suwayomi;
use suwayomi::SmManga;

struct Bridge;

// Some Mihon sources return a useful chapter title but leave chapterNumber at
// -1. Aidoku then displays that sentinel alongside the title (for example,
// "Capitulo -1 - 109"). Recover the numeric value from explicit chapter labels
// so the app can sort/display it correctly while retaining the original title.
fn number_from_chapter_name(name: &str) -> Option<f32> {
    fn parse_after(text: &str, marker: &str) -> Option<f32> {
        let lower = text.to_ascii_lowercase();
        let mut i = lower.find(marker)? + marker.len();
        let bytes = text.as_bytes();
        while i < bytes.len() && !bytes[i].is_ascii_digit() {
            i += 1;
        }
        if i == bytes.len() {
            return None;
        }
        let mut value = 0.0f32;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            value = value * 10.0 + (bytes[i] - b'0') as f32;
            i += 1;
        }
        if i + 1 < bytes.len() && bytes[i] == b'.' && bytes[i + 1].is_ascii_digit() {
            i += 1;
            let mut place = 0.1f32;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                value += (bytes[i] - b'0') as f32 * place;
                place *= 0.1;
                i += 1;
            }
        }
        (value > 0.0).then_some(value)
    }

    for marker in ["capitulo", "capítulo", "chapter", "episode", "episodio", "cap.", "chap.", "ep.", "cap"] {
        if let Some(number) = parse_after(name, marker) {
            return Some(number);
        }
    }
    None
}

impl Bridge {
    fn cfg(&self) -> Result<(String, Option<String>, String)> {
        let src = defaults_get::<String>("sourceId").filter(|s| !s.is_empty())
            .ok_or(aidoku::AidokuError::Message(String::from("Missing source configuration")))?;
        let embedded_base = suwayomi::api_base()
            .map_err(|reason| error!("deployment gateway URL unavailable: {}", reason))?;
        // The deployment URL and token are embedded in this generated WASM.
        // Only the source ID remains as a source setting.
        let configured_base = embedded_base.trim_end_matches('/');
        let base = String::from(configured_base.strip_suffix("/api").unwrap_or(configured_base));
        if base.is_empty() {
            return Err(error!("gateway URL not configured"));
        }
        let tok = suwayomi::token(&embedded_base)
            .map_err(|reason| error!("deployment gateway token unavailable: {}", reason))?;
        Ok((base, Some(tok), src))
    }
    fn manga(m: &SmManga, base: &str, src: &str) -> Manga {
        Manga { key: suwayomi::manga_key(src, m), title: m.title.clone(),
            cover: m.thumbnail_url.as_deref().map(|handle| suwayomi::media_url(base, handle)),
            authors: m.author.clone().filter(|s| !s.is_empty()).map(|a| Vec::from([a])),
            artists: m.artist.clone().filter(|s| !s.is_empty()).map(|a| Vec::from([a])),
            description: m.description.clone(), url: m.real_url.clone().or(m.url.clone()),
            tags: m.genre.clone(), status: match m.status.as_deref() {
                Some("ONGOING") => MangaStatus::Ongoing, Some("COMPLETED") => MangaStatus::Completed,
                Some("HIATUS") => MangaStatus::Hiatus, Some("CANCELLED") => MangaStatus::Cancelled,
                _ => MangaStatus::Unknown },
            ..Default::default() }
    }
}

fn to_change(pos: usize, fv: &FilterValue, defs: &[suwayomi::SwFilter]) -> Option<String> {
    match fv {
        FilterValue::Check { value, .. } => {
            Some(format!(r#"{{position:{pos},checkBoxState:{}}}"#, *value != 0))
        }
        FilterValue::Text { value, .. } => {
            Some(format!(r#"{{position:{pos},textState:"{}"}}"#, suwayomi::esc(value)))
        }
        FilterValue::Select { value, .. } => {
            let idx = match defs.get(pos) {
                Some(suwayomi::SwFilter::SelectFilter { values, .. }) => {
                    values.iter().position(|o| o == value).or_else(|| {
                        value.parse::<i32>().ok().filter(|&n| n >= 0 && (n as usize) < values.len()).map(|n| n as usize)
                    })
                }
                _ => None,
            };
            idx.map(|n| format!(r#"{{position:{pos},selectState:{n}}}"#))
        }
        _ => None,
    }
}

impl DynamicFilters for Bridge {
    fn get_dynamic_filters(&self) -> Result<Vec<Filter>> {
        let (base, tok, src) = self.cfg()?;
        let fs = suwayomi::fetch_filters(&base, &tok, &src)?;
        Ok(fs.into_iter().enumerate().filter_map(|(pos, f)| {
            let id = Cow::Owned(format!("{pos}"));
            match f {
                suwayomi::SwFilter::CheckBoxFilter { name, cbDefault: default } => Some(Filter {
                    id, title: Some(Cow::Owned(name)), hide_from_header: None,
                    kind: FilterKind::Check { name: None, can_exclude: false, default: Some(default) },
                }),
                suwayomi::SwFilter::SelectFilter { name, selDefault: default, values } => {
                    if values.is_empty() { return None; }
                    let def = values.get(default.max(0) as usize).cloned();
                    Some(Filter {
                        id, title: Some(Cow::Owned(name)), hide_from_header: None,
                        kind: FilterKind::Select {
                            is_genre: false, uses_tag_style: false,
                            options: values.into_iter().map(Cow::Owned).collect(),
                            ids: None, default: def.map(Cow::Owned),
                        },
                    })
                }
                suwayomi::SwFilter::TextFilter { name } => Some(Filter {
                    id, title: Some(Cow::Owned(name)), hide_from_header: None,
                    kind: FilterKind::Text { placeholder: None },
                }),
                suwayomi::SwFilter::Other => None,
            }
        }).collect())
    }
}

impl Source for Bridge {
    fn new() -> Self { Self }
    fn get_search_manga_list(&self, query: Option<String>, page: i32, f: Vec<FilterValue>) -> Result<MangaPageResult> {
        let (base, tok, src) = self.cfg()?;
        let q = query.unwrap_or_default();
        if q.trim_start().starts_with("http") {
            let m = suwayomi::resolve_url(&base, &tok, q.trim())?;
            return Ok(MangaPageResult { entries: Vec::from([Self::manga(&m, &base, &src)]), has_next_page: false });
        }
        let changes: Vec<String> = if f.is_empty() {
            Vec::new()
        } else {
            let defs = suwayomi::fetch_filters(&base, &tok, &src).unwrap_or_default();
            f.iter().filter_map(|fv| {
                let pos: usize = match fv {
                    FilterValue::Check { id, .. }
                    | FilterValue::Text { id, .. }
                    | FilterValue::Select { id, .. } => id.parse().ok()?,
                    _ => return None,
                };
                to_change(pos, fv, &defs)
            }).collect()
        };
        let d = suwayomi::fetch_source(&base, &tok, &src, "SEARCH", Some(&q), page, &changes)?;
        Ok(MangaPageResult { entries: d.mangas.iter().map(|m| Self::manga(m, &base, &src)).collect(),
            has_next_page: d.has_next_page.unwrap_or(false) })
    }
    fn get_manga_update(&self, mut manga: Manga, need_d: bool, need_c: bool) -> Result<Manga> {
        if !need_d && !need_c { return Ok(manga); }
        let (base, tok, s) = self.cfg()?;
        let url = suwayomi::manga_url(&s, &manga.key, manga.url.as_deref())?;
        let d = suwayomi::fetch_manga_and_chapters(
            &base, &tok, &s, &url, need_d, need_c,
            Some(&manga.title),
            manga.authors.as_ref().and_then(|values| values.first()).map(|value| value.as_str()),
            manga.artists.as_ref().and_then(|values| values.first()).map(|value| value.as_str()),
            manga.description.as_deref(),
        )?;
        if need_c {
            manga.chapters = Some(d.chapters.into_iter().map(|c| Chapter {
                key: c.real_url.clone().or(c.url.clone()).unwrap_or_default(), title: Some(c.name.clone()),
                chapter_number: c.chapter_number.filter(|n| *n >= 0.0)
                    .or_else(|| number_from_chapter_name(&c.name)),
                date_uploaded: c.upload_date.and_then(|ms| ms.parse::<i64>().ok()).map(|ms| ms / 1000),
                scanlators: c.scanlator.filter(|s| !s.is_empty()).map(|s| Vec::from([s])),
                url: c.real_url, ..Default::default() }).collect());
        }
        if need_d {
            if !d.manga.title.is_empty() { manga.title = d.manga.title.clone(); }
            if let Some(u) = d.manga.real_url.clone().or(d.manga.url.clone()).filter(|s| !s.is_empty()) { manga.url = Some(u); }
            if let Some(cover) = d.manga.thumbnail_url.as_deref() { manga.cover = Some(suwayomi::media_url(&base, cover)); }
            if d.manga.description.as_deref().map(|s| !s.is_empty()).unwrap_or(false) { manga.description = d.manga.description.clone(); }
            if let Some(a) = d.manga.author.filter(|s| !s.is_empty()) { manga.authors = Some(Vec::from([a])); }
            if let Some(a) = d.manga.artist.filter(|s| !s.is_empty()) { manga.artists = Some(Vec::from([a])); }
            if let Some(g) = d.manga.genre.filter(|v| !v.is_empty()) { manga.tags = Some(g); }
            match d.manga.status.as_deref() {
                Some("ONGOING") => manga.status = MangaStatus::Ongoing,
                Some("COMPLETED") => manga.status = MangaStatus::Completed,
                Some("HIATUS") => manga.status = MangaStatus::Hiatus,
                Some("CANCELLED") => manga.status = MangaStatus::Cancelled,
                _ => {}
            }
        }
        Ok(manga)
    }
    fn get_page_list(&self, manga: Manga, ch: Chapter) -> Result<Vec<Page>> {
        let (base, tok, src) = self.cfg()?;
        let manga_url = suwayomi::manga_url(&src, &manga.key, manga.url.as_deref())?;
        let chapter_url = ch.url.as_deref().filter(|u| u.starts_with("http://") || u.starts_with("https://"))
            .or_else(|| if ch.key.starts_with("http://") || ch.key.starts_with("https://") { Some(ch.key.as_str()) } else { None })
            .ok_or(error!("saved chapter has no source URL; refresh the chapter list"))?;
        Ok(suwayomi::fetch_pages(&base, &tok, &src, &manga_url, chapter_url,
            ch.title.as_deref(), ch.chapter_number, ch.scanlators.as_ref().and_then(|s| s.first()).map(|s| s.as_str()))?.into_iter()
            .map(|handle| Page { content: PageContent::url(suwayomi::media_url(&base, &handle)), ..Default::default() }).collect())
    }
}

impl ListingProvider for Bridge {
    fn get_manga_list(&self, listing: Listing, page: i32) -> Result<MangaPageResult> {
        let (base, tok, src) = self.cfg()?;
        let ty = match listing.id.as_str() { "popular" => "POPULAR", "latest" => "LATEST",
            _ => return Err(error!("unknown listing {}", listing.id)) };
        let d = suwayomi::fetch_source(&base, &tok, &src, ty, None, page, &[])?;
        Ok(MangaPageResult { entries: d.mangas.iter().map(|m| Self::manga(m, &base, &src)).collect(),
            has_next_page: d.has_next_page.unwrap_or(false) })
    }
}

impl ImageRequestProvider for Bridge {
    fn get_image_request(&self, url: String, _ctx: Option<aidoku::HashMap<String, String>>) -> Result<Request> {
        let (base, tok, _s) = self.cfg().unwrap_or((String::from(""), None, String::from("")));
        let mut req = Request::get(&url).map_err(|e| error!("{:?}", e))?;
        req.set_header("User-Agent", "Aidoku/1.0");
        let gateway_prefix = format!("{}/", base.trim_end_matches('/'));
        if !base.is_empty() && (url == base || url.starts_with(gateway_prefix.as_str())) {
            if let Some(t) = suwayomi::token_value(&tok) { req.set_header("X-Bridge-Token", t); }
            req.set_header("Referer", &base);
        }
        Ok(req)
    }
}

register_source!(Bridge, ListingProvider, ImageRequestProvider, DynamicFilters, MigrationHandler);

impl MigrationHandler for Bridge {
    // Drop the former Suwayomi database row id from stable keys. Manga identity
    // is now only the source plus the canonical source URL.
    fn handle_manga_migration(&self, key: String) -> Result<String> {
        if let Some((key_src, _, url)) = suwayomi::split_key(&key) {
            if let Ok((_, _, src)) = self.cfg() {
                if key_src == src { return Ok(format!("sm|{src}|0|{url}")); }
            }
        }
        // Old numeric-only ids have no URL in the key and cannot be resolved
        // without reintroducing server-side manga records.
        Ok(key)
    }
    fn handle_chapter_migration(&self, _manga_key: String, chapter_key: String) -> Result<String> {
        Ok(chapter_key)
    }
}
