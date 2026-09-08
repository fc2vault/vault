# FC2 metadata sources — reliability & priority (the "universal scraper" model)

For each field, fetch from the **highest-priority source that has it**, and **never overwrite
a better source or a value the user edited** (fill-if-empty, except an explicit force-rescan).

## Priority per field (best first)

| Field | Priority order | Notes |
|---|---|---|
| **Duration / Resolution / Codec** | **local ffprobe** | Authoritative for owned files; no web source beats it. |
| **Poster (owned)** | **local ffmpeg** frame (Part 1) | Generated from the actual file. |
| **Cover image (wishlist / fallback)** | **ffjav** → onejav → bigboobs per-work thumb | For items with no local file. |
| **Actress name + profile** (age, measurements, aliases, portrait, kana) | **bigboobs** → **javdatabase** + **jav.guru** (verified) → forum seeds (2760/217) → **manual edit** | bigboobs is the fullest source. javdatabase/jav.guru enrich by known name — but **only after code-overlap verification** (see below). NEVER auto-parse a name out of a title. |
| **Studio / producer** | **bigboobs (🎥)** only | 123av/ffjav/onejav all report a generic "FC2" — useless. |
| **Favorites / popularity (登録数)** | **bigboobs** only | |
| **Japanese title** | **bigboobs** (identified) → **ffjav** (any code) | ffjav covers the amateur majority bigboobs doesn't. |
| **English title** | **123av** (clean) → javhdporn → jav.sb (awkward) | 123av ~95% hit rate on FC2, cleanest phrasing. |
| **Release date** | bigboobs → ffjav → 123av → onejav | |
| **Tags / genres** | **derived from the JP/EN title** (keyword map) → manual | Site "genres" are just Amateur/FC2 — not useful. |

## Reachability (how each is fetched)

- **Shell-direct** (server-side `scan.py`, no browser): **123av**, **ffjav**, **onejav**, **javdatabase** (`/idols/<slug>` — DOB→age, height, cup, B-W-H, JP name), **jav.guru** (`/actress/<slug>` — her FC2 filmography, used to verify identity).
- **Browser-only** (Cloudflare / JS-rendered): **bigboobs** (also needs archive_id or name-search — no code→actress lookup), **javhdporn**, **jav.sb**, **projectjav**, **141ppv**, **ijavtorrent**.
- **Local**: ffprobe / ffmpeg.
- **Curated / index**: jav-forum threads 2760 & 217 (actress↔code), minnano-av (FC2 actress name index for the bigboobs sweep).

## What the scanner (`scan.py`) does per code
1. **123av** → English title.
2. **ffjav** → Japanese title + cover image + release date.
3. Derive **genre tags** from whichever title we now have.
4. Writes **only empty fields** (force-rescan overrides). Never touches actress / producer / fav
   (bigboobs-owned) or duration/resolution (local ffprobe) or anything hand-edited.

Actress identification stays the **browser bigboobs sweep + manual ✎ edit** — there is no reliable
code→actress source, and parsing names from titles is what created junk records before.

## Actress enrichment (javdatabase + jav.guru) — verified by library overlap
`scan.enrich_actress(con, actress_id, library_codes=...)` (UI: **🌐 Enrich from web** in the ✎
panel) fetches javdatabase (body info + JP name) and jav.guru (her FC2 filmography) by trying
romaji-slug variants of the actress's known names in both word orders. **It verifies by asking:
does your library contain any title from her scraped filmography?** — no prior linkage needed, so
she's recognised even before her movies are attributed. This guards against two different
performers sharing a romaji name. Unverified results are reported, never applied (the UI offers an
explicit *force* confirm). On apply it also folds her whole filmography into the catalog: a code
**you own** is attributed to her (owned), a code you **don't** becomes a `missing` wishlist entry —
so enriching her fills her wishlist automatically. Physical fields are fill-if-empty (COALESCE) so
curated values are never clobbered; EN+JP names become aliases and both source URLs are stored.

> Worked example — `Sora Mikumo` (美雲そら, DOB 2000-03-30, debut 2023, 150 cm, B, 80-61-89): her
> jav.guru filmography lists 11 titles; you own **6** of them → **verified** (no force needed). Apply
> attributes those 6 owned files to her and adds the other 5 as wishlist. The old folder code
> `1668475` simply isn't in her online filmography — which is why verification must look at the whole
> library, not just that one code.

Actress names carried in a folder (`<CODE> - Name`) are **not** trusted to create records —
those spellings are unreliable. On **🧹 Organize**, `capture_folder_actresses` links the work to
an actress **only if that name already exists** in the catalog; otherwise the name is dropped when
the folder is normalized to a bare `<CODE>`, and the actress is resolved online instead.

## One person = one record (dedup)
`_find_actress()` resolves a typed name to an existing record by exact name (en/jp/kana), alias, **or
the same name in a different word order** ("Sora Mikumo" ↔ "Mikumo Sora") — so setting/​linking a name
never spawns a duplicate the way it used to. To clean up dupes that already exist, `dedupe.py` scans
the whole catalog and merges records that share an **identical Japanese name** or are a **romaji
word-order flip**, folding works + aliases + links + body-info into the survivor (the one you own
titles under) and deleting the emptied duplicate — **never** deleting a movie. Alias-only matches are
reported for manual review, not auto-merged (alias lists carry co-stars/scraped noise).
Run: `python3 dedupe.py data/catalog.db` (dry run) then `--merge`.

## Not worth scraping (for FC2)
- Studio/genres from 123av/ffjav/onejav/javhdporn — all generic.
- jav.sb & javhdporn English titles — worse phrasing than 123av, and both now Cloudflare-gated from the shell.
- projectjav / 141ppv / ijavtorrent — torrent aggregators, same title/size/cover already covered by ffjav+onejav, and browser-only.
