#![no_std]
use aidoku::{alloc::{String, Vec, format, borrow::Cow}, imports::{defaults::defaults_get, net::Request},
    prelude::*, Chapter, Filter, FilterKind, FilterValue, Listing, ListingProvider, Manga, MangaPageResult, MangaStatus,
    Page, PageContent, Result, Source, DynamicFilters, ImageRequestProvider, MigrationHandler};

mod suwayomi;
use suwayomi::SmManga;

struct Bridge { ids: core::cell::RefCell<Vec<(String, i32)>> }

impl Bridge {
    fn resolved_id(&self, base: &str, tok: &Option<String>, src: &str, key: &str) -> Result<i32> {
        if let Ok(id) = key.parse::<i32>() { return Ok(id); }
        if let Some((_, id)) = self.ids.borrow().iter().find(|(k, _)| k == key) { return Ok(*id); }
        let id = suwayomi::resolve_manga_id(base, tok, src, key)?;
        self.ids.borrow_mut().push((String::from(key), id));
        Ok(id)
    }    fn cfg(&self) -> Result<(String, Option<String>, String)> {
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
            cover: Some(format!("{base}/api/v1/manga/{}/thumbnail", m.id)),
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
    fn new() -> Self { Self { ids: core::cell::RefCell::new(Vec::new()) } }
    fn get_search_manga_list(&self, query: Option<String>, page: i32, f: Vec<FilterValue>) -> Result<MangaPageResult> {
        let (base, tok, src) = self.cfg()?;
        let q = query.unwrap_or_default();
        if q.trim_start().starts_with("http") {
            let r = suwayomi::add_by_url(&base, &tok, q.trim())?;
            if let Some(m) = r.manga {
                return Ok(MangaPageResult { entries: Vec::from([Self::manga(&m, &base, &src)]), has_next_page: false });
            }
            return Err(error!("add-by-url: {:?}", r.message.unwrap_or_default()));
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
        let (base, tok, s) = self.cfg()?;
        let id = self.resolved_id(&base, &tok, &s, &manga.key)?;
        if need_c {
            let cs = suwayomi::fetch_chapters(&base, &tok, id)?;
            manga.chapters = Some(cs.into_iter().map(|c| Chapter {
                key: format!("{}", c.id), title: Some(c.name.clone()),
                chapter_number: c.chapter_number,
                date_uploaded: c.upload_date.as_deref().and_then(|s| s.parse::<i64>().ok()).map(|ms| ms/1000),
                scanlators: c.scanlator.filter(|s| !s.is_empty()).map(|s| Vec::from([s])),
                url: c.real_url, ..Default::default() }).collect());
        }
        if need_d {
            manga.cover = Some(format!("{base}/api/v1/manga/{id}/thumbnail"));
            if let Ok(d) = suwayomi::fetch_manga_details(&base, &tok, id) {
                if !d.title.is_empty() { manga.title = d.title.clone(); }
                if let Some(u) = d.real_url.clone().or(d.url.clone()).filter(|s| !s.is_empty()) { manga.url = Some(u); }
                if d.description.as_deref().map(|s| !s.is_empty()).unwrap_or(false) { manga.description = d.description.clone(); }
                if let Some(a) = d.author.filter(|s| !s.is_empty()) { manga.authors = Some(Vec::from([a])); }
                if let Some(a) = d.artist.filter(|s| !s.is_empty()) { manga.artists = Some(Vec::from([a])); }
                if let Some(g) = d.genre.filter(|v| !v.is_empty()) { manga.tags = Some(g); }
                match d.status.as_deref() {
                    Some("ONGOING") => manga.status = MangaStatus::Ongoing,
                    Some("COMPLETED") => manga.status = MangaStatus::Completed,
                    Some("HIATUS") => manga.status = MangaStatus::Hiatus,
                    Some("CANCELLED") => manga.status = MangaStatus::Cancelled,
                    _ => {}
                }
            }
        }
        Ok(manga)
    }
    fn get_page_list(&self, _m: Manga, ch: Chapter) -> Result<Vec<Page>> {
        let (base, tok, _s) = self.cfg()?;
        let id: i32 = ch.key.parse().map_err(|_| error!("bad chapter key"))?;
        Ok(suwayomi::fetch_pages(&base, &tok, id)?.into_iter()
            .map(|x| Page { content: PageContent::url(suwayomi::abs(&base, &x)), ..Default::default() }).collect())
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
        if !base.is_empty() && url.starts_with(base.as_str()) {
            if let Some(t) = suwayomi::token_value(&tok) { req.set_header("X-Bridge-Token", t); }
            req.set_header("Referer", &base);
        }
        Ok(req)
    }
}

register_source!(Bridge, ListingProvider, ImageRequestProvider, DynamicFilters, MigrationHandler);

impl MigrationHandler for Bridge {
    // Key migration (numeric -> stable via live details, failures keep old key).
    // Only upgrade to a migrating build with valid numeric keys: after a DB
    // wipe, migrate manually in Aidoku first.
    fn handle_manga_migration(&self, key: String) -> Result<String> {
        if key.starts_with("sm|") { return Ok(key); }
        let id: i32 = match key.parse() { Ok(n) => n, Err(_) => return Ok(key) };
        let (base, tok, src) = match self.cfg() { Ok(c) => c, Err(_) => return Ok(key) };
        match suwayomi::fetch_manga_details(&base, &tok, id) {
            Ok(d) => {
                let m = SmManga { id, title: d.title.clone(), thumbnail_url: None,
                    description: None, author: None, artist: None, genre: None,
                    status: None, real_url: d.real_url.clone(), url: d.url.clone() };
                let nk = suwayomi::manga_key(&src, &m);
                if nk == key { Ok(key) } else { Ok(nk) }
            }
            Err(_) => Ok(key),
        }
    }
    fn handle_chapter_migration(&self, _manga_key: String, chapter_key: String) -> Result<String> {
        Ok(chapter_key)
    }
}
