# Vault

**A self‑hosted, filterable “Plex for FC2 / JAV.”** Point it at a folder of movies and it becomes a fast, private web app: a poster wall with streaming playback, deep metadata filters, an actress catalog with faces and body stats, a wishlist of titles you don’t own yet, and one‑click tools to tidy, tag, and enrich your library from the web.

Runs entirely on your machine. No cloud, no accounts, no telemetry — a single Python file serves a single HTML page. The UI is fully responsive, so the same page works on a phone over your LAN or VPN.

> **Read [Privacy & safety](#privacy--safety) before exposing it.** Vault listens on **all interfaces** and has **no login and no HTTPS**. On a trusted home network that is the point; it is not safe to port‑forward to the internet.

![Vault — grid view](docs/images/grid.png)

---

## Contents

- [Features](#features)
- [Free and Pro](#free-and-pro)
- [Requirements](#requirements)
  - [Optional: FlareSolverr](#optional-flaresolverr)
- [Install](#install)
- [Configure](#configure)
- [Run](#run)
- [A quick tour](#a-quick-tour)
- [User guide](#user-guide)
- [How metadata works](#how-metadata-works)
- [Project layout](#project-layout)
- [Building a release](#building-a-release)
- [Privacy & safety](#privacy--safety)
- [Troubleshooting](#troubleshooting)

---

## Features

**Browse & find**
- Grid, **By‑actress**, and **List** views over your whole library
- Sidebar filters: identified / amateur, **favorite movies**, **favorite actresses**, **wishlist**, watched state, HD, uncensored, actress, studio, resolution, duration, cup size, age
- Full‑text search across actress (incl. **aliases** and Japanese names), code, title, and tags — matches surface to the top
- Sort by **date added** (true first‑seen, not the copied file date), release, size, duration, rating, favorites, actress, code, or shuffle

**Use it anywhere**
- One responsive page — no separate mobile site and no UA sniffing; the layout keys off window width and pointer type, so a narrow desktop window behaves like a phone
- Phone layout: two‑column poster wall, filters in a slide‑over **☰ Filters** drawer, and a player that stacks the video above **Info** / **Parts · Related** tabs
- Reachable from any device on your LAN or over a VPN back to it (**no authentication** — see [Privacy & safety](#privacy--safety))
- The grid renders in batches as you scroll, so a multi‑thousand‑item library stays inside mobile Safari's memory budget

**Watch**
- In‑browser streaming with HTTP range requests + `sendfile` (fast seeks over SMB)
- Multi‑part playback with auto‑advance
- Keyboard shortcuts (space/k, ←→ / j l seek, ↑↓ volume, m, f, 0–9, `< >` speed)
- HEVC handled natively where the browser supports it, with a clear fallback panel where it doesn’t

**Curate**
- Rich edit panel: actress (EN + JP, with autocomplete), age, cup, height, measurements, censorship toggle, EN/JP titles
- **Reassign** a single misgrouped movie to another actress straight from the player (**⇄**) — relinks just that title, never renames the actress it left
- **Multi‑select** (shift / ⌘‑click, or the card's select box) → bulk‑assign one actress to many movies, or bulk‑delete them
- **Delete** a movie (from the player), several at once (multi‑select), or a whole actress and her movies (from her overview) — always confirmed; files are **moved to a recoverable trash**, never hard‑deleted. Deleting an actress trashes only titles where she is the sole performer; shared co‑star titles are kept
- **Scene markers**: the player draws scenes (e.g. *blowjob*, *doggy*, *cumshot*) as icon chips and ticks **right on the seek bar**, tube‑site style — hover for the label, click to jump. With **training mode** on (Settings → Library) you can add, move, relabel and delete markers via **＋** and a **right‑click** menu, and create custom labels. Markers come from `<video>.scenes.json` sidecars produced by an external analyzer. See **Scene markers** below
- **Organize** the folder on disk: normalize `…/<CODE>/<CODE>.ext`, quarantine junk, flag/relocate duplicates — preview first, nothing deleted
- **Organize by actress**: fold every movie into `…/<Actress>/<CODE>/` to match the catalog (so the disk follows reassignments) — preview first, only moves, never deletes
- **Settings** hub: change the library folder, clear caches, manage tags (rename/merge + **JP→EN translation**), manage actresses (rename, merge, **find duplicates**), and connect sources

**Enrich**
- **Scan metadata** from the web (English/Japanese titles, covers, dates, tags) across your library
- **Enrich from web** per actress: body info, JP name, aliases — **verified by FC2‑code overlap** so you don’t attach the wrong same‑name person, and it fills her **wishlist** from her filmography

## Free and Pro

Vault is free for libraries of up to **250 movies**. A bigger library shows the 250 movies Vault
saw first, and the count at the top says how many there are in all.

**Vault Pro** (a yearly key for supporters) unlocks:

- **your whole library**, with no movie limit
- **fc2ppv‑db** lookups: actress, face, aliases, filmographies
- **Enrich from web** for actresses (javdatabase + jav.guru, verified by code overlap)
- the trained **scene model** for automatic markers, and every improved model after it

Everything else (player, filters, scene markers, tags, organizing, importing) is the same in
both. Paste the key in **Settings → License**. It is checked on your machine: Vault never
contacts a license server, and a key holds no name or email address, only its expiry date and a
random id.

**Getting a key:** support the project on [Patreon](https://www.patreon.com/c/ForfunAdventures),
then open the [key page](https://vault-key.vaultkey.workers.dev/) and log in with Patreon: the key
appears on the spot, nothing is stored and no e-mail is sent. The project
[website](https://fc2vault.vaultkey.workers.dev/) has the details.

**The scene model** arrives the same way: with a key active, Settings → License shows the newest
model build and a **Download** button. Vault sends the key to the key server, which answers with
a short-lived download link, and unpacks the model into a per-user folder that
[vault-scene-tagger](https://github.com/fc2vault/vault-scene-tagger) finds on its own. See
[Generating markers](#generating-markers-with-vault-scene-tagger).

The terms are in [LICENSE.md](LICENSE.md): the free version never expires, the source may be
read and modified for your own use, and a build that removes the Pro check may not be published.

## Requirements

- **Python 3.8+** (standard library only — no `pip install`)
- **ffmpeg / ffprobe** on your `PATH` (posters + duration/resolution/codec)
- A modern browser (Chrome, Brave, Safari, Firefox)
- macOS, Linux or Windows (developed on macOS; Windows support is new, run it with `py serve.py`; the optional fc2ppv‑db session store uses the macOS Keychain, with a file fallback elsewhere)

Install ffmpeg:

```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian/Ubuntu
```

### Optional: FlareSolverr

Vault works fully without it. It matters for exactly one source — **fc2ppv‑db**, which supplies
the **cast** (and JP titles, dates, covers, tags). That site sits behind a Cloudflare human
check, so Vault needs one of two ways through it:

1. **Paste a browser session** — no extra software. Log in, copy the `cookie:` and
   `user-agent:` headers from DevTools into Settings → Sources. It works, but the cookie is
   bound to that browser's IP and expires, so you re‑paste it every so often.
2. **Run FlareSolverr** — a small service that keeps a headless Chromium and clears the check
   for you. Nothing to re‑paste.

**FlareSolverr is a separate, third‑party program. Vault does not bundle, install or update
it**, and nothing here is affiliated with the project. Install it yourself only if you want
option 2 — see [FlareSolverr on GitHub](https://github.com/FlareSolverr/FlareSolverr) for
current instructions. It is commonly run as a container:

```bash
docker run -d --name flaresolverr -p 8191:8191 --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest
```

Then put its URL in **Settings → Sources → FlareSolverr** (`http://host:8191`; `/v1` is
optional) and press **Test connection**.

Two constraints worth knowing before you set it up:

- It must leave through the **same public IP** as Vault, or the clearance it obtains will not
  validate for Vault's requests. Running it on the same machine or the same LAN is the simple
  case; a VPN on one side and not the other is the usual reason a working resolver still fails.
- Vault only accepts a resolver on **loopback or a private address** (`127.0.0.1`, `10.x`,
  `192.168.x`, `172.16–31.x`). The server has no authentication, so it will not ship your page
  requests to an address on the public internet.

If no resolver is configured and no session is saved, every other source still works and scans
simply skip the fc2ppv‑db pass, saying so rather than failing.

## Install

```bash
git clone <your-repo-url> vault
cd vault
```

There’s nothing to build or compile.

## Configure

On first run Vault copies `config.example.json` to `config.json`. Edit it to point at your movies:

```json
{
  "library": "/path/to/your/movies",
  "port": 8730
}
```

You can also set the library from the CLI or in **Settings → Library** later:

```bash
python3 serve.py --library "/path/to/your/movies"
```

## Run

```bash
python3 serve.py
```

Then open **http://127.0.0.1:8730/**. First launch walks the library (slower over a network share); after that it boots instantly from a cached index and refreshes in the background.

To run detached:

```bash
python3 serve.py > vault.log 2>&1 &
```

### Run at login (as a service)

```bash
python3 serve.py --install-service      # starts at login, restarts if it stops
python3 serve.py --service-status
python3 serve.py --uninstall-service
```

This registers a per‑user service with no administrator rights: a **launchd** agent on macOS
(`~/Library/LaunchAgents/local.vault.server.plist`), a **systemd --user** unit on Linux
(`~/.config/systemd/user/vault.service`; run `loginctl enable-linger` on a headless box so it
runs without a login session), and a **Task Scheduler** task named *Vault* on Windows that runs
at log on with `pythonw.exe`, so no console window. The service runs the same `serve.py` with the
same Python that ran the install, and logs to `server.log` next to the config.

macOS note: background jobs get no access to **Documents, Desktop, Downloads or removable
volumes** (the system cannot show the permission prompt). Keep the Vault folder somewhere else,
for example `~/vault`, or grant `python3` Full Disk Access in System Settings → Privacy &
Security. `--install-service` checks the log after starting and tells you if this happened.

### On your phone or another computer

Vault listens on every interface, so from any device on the same network (or connected to your LAN over VPN) open:

```
http://<this-machine-ip>:8730/
```

Find the address with `ipconfig getifaddr en0` (macOS) or `hostname -I` (Linux). The layout adapts on its own — no separate mobile site, no app to install:

- **Phone:** two‑column poster wall; filters live behind a **☰ Filters** drawer; the player stacks the video on top with **Info** and **Parts / Related** tabs beneath it.
- **Tablet / small window:** the same rules key off window width, so a narrow desktop window gets the compact layout too.

The grid renders in batches as you scroll, which is what keeps a multi‑thousand‑item library from exhausting mobile Safari's per‑tab memory.

Nothing here is authenticated — see [Privacy & safety](#privacy--safety).

---

## A quick tour

**Grid** is the home view — a poster wall with actress, code, tags, and size/age/cup pills. Click a card to play; **shift/⌘‑click** to select several.

**Detail / player** shows the movie with the actress up top (English → Japanese → code), her stats as pills, full technical metadata, tags, rating, and a filmography rail of her other titles (owned + wishlist).


**Settings** is where you manage everything — library folder & cache, tags, actresses, and sources.

![Settings — Sources & fc2ppv‑db](docs/images/settings_sources.png)

See the **[full user guide](docs/USER_GUIDE.md)** for a screenshot walkthrough of every feature.

## User guide

The detailed, screenshot‑driven guide lives in **[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)** and covers:

- Browsing, filtering, sorting, and the wishlist
- Playback and keyboard shortcuts
- Editing a movie’s actress & metadata
- Reassigning a misgrouped movie to another actress
- Multi‑select bulk actress assignment
- Organizing the library folder (by `<CODE>` and **by actress**)
- Scanning & enriching from the web (and the verification model)
- Settings: library, cache, **tags + translations**, **actresses + dedup**, sources
- Connecting the premium **fc2ppv‑db** source

## How metadata works

Vault treats your **filesystem as the spine** and the catalog database (`data/catalog.db`) as an enrichment overlay. Each source contributes only the fields it’s best at, and Vault never overwrites a value you edited by hand. See **[`SOURCES.md`](SOURCES.md)** for the per‑field priority table and the exact scraping model.

Highlights:
- **Local first** — duration/resolution/codec from `ffprobe`, posters from `ffmpeg`.
- **Shell scrapers** — 123av (English titles), ffjav (JP title, cover, date).
- **Verified actress enrichment** — javdatabase + jav.guru, gated by FC2‑code overlap with your library.
- **fc2ppv‑db** (optional) — actress faces, aliases, JP titles, dates, tags, full filmographies. It sits behind a human‑check. In Settings → Sources, either paste a browser session from this machine, or point Vault at a FlareSolverr on the same public IP and let that browser load the pages.
- **Canonical tags** — every scraped tag is routed through the JP→EN / merge table before it’s stored: Japanese tags become their English label, merged tags stay merged (a merge you make is remembered for future scans), and untranslated Japanese one‑offs are dropped instead of polluting the set. English tags without a mapping are kept as‑is.

## Scene markers (sidecars)

The player draws scene markers **on the seek bar** from **`<video>.scenes.json`** sidecar files
placed next to each video. Producing them (shot detection, AI act/position tagging) is deliberately
**out of scope for this server** — that runs in a separate analyzer project. Vault only reads the
files and caches them in a `markers` table. Markers are shown as start points; editing them is
only possible in **training mode** (see below).

Format (`schema: "vault-scenes/1"`), timestamps in seconds, relative to that one video file:

```json
{
  "schema": "vault-scenes/1",
  "video": "FC2-PPV-1234567.mp4",
  "duration": 1800.0,
  "markers": [
    { "t": 120.0, "end": 300.0, "label": "blowjob", "confidence": 0.82, "source": "ml" },
    { "t": 900.0, "label": "cumshot", "source": "manual" }
  ]
}
```

Merge rule: the analyzer owns `source` values `cut` / `heuristic` / `ml` and may rewrite them on
every run; markers with `source: "manual"` (added in Vault) are always preserved. Drop a sidecar
next to any movie and open it — the markers appear. (Vault generates its own scrub‑bar thumbnails;
a sidecar sprite sheet is not required.)

The full, versioned contract is **[`SCENES_SPEC.md`](SCENES_SPEC.md)**. It's a verbatim copy of the one in the
analyzer repo; keep the two identical. When Vault writes manual markers back, it keeps the
sidecar's other top-level fields and its `generator`. That way the analyzer can still tell which
phases already ran and doesn't re-decode a movie just because you added a marker.

### Training mode (editing markers)

**Settings → Library → Scene markers — training mode** is **off by default**, so markers are
view‑only and `POST /api/marker` answers 403. Turn it on to curate markers in the player:

- **＋** adds a marker at the playhead; **right‑click** the seek bar or the video for a menu to add
  one at that spot; **drag** a chip along the bar to move it;
- **right‑click** an AI marker and **✓ Confirm** it if it's correct (or **✓ Confirm all AI markers**
  from the bar's menu) — only reviewed markers count as training labels;
- **right‑click** a marker (or the bar next to it) to pick its label from the fixed list — 16 acts
  and 7 positions, single choice — nudge it (−1s / +1s / playhead) or delete it.

Every change is saved into the sidecar as a `manual` marker (start only), which the analyzer never
overwrites. For future model training, a changed AI marker keeps its original under `was`, and a
deleted AI marker is appended to the sidecar's top‑level `rejected` list; the analyzer also uses
both to avoid re‑suggesting the same moment. The setting is stored as `"training_mode"` in
`config.json`. Extra label types can be created from the editor's label list (**＋ New label…**) or
in Settings; they're kept as `"custom_labels"` in `config.json` and are ordinary labels in the
sidecar (the spec allows labels outside §4). Vault has no login, so anyone who can reach it can flip the switch — keep it on a
trusted network.

### Generating markers with vault-scene-tagger

The analyzer is its own project, [vault-scene-tagger](https://github.com/fc2vault/vault-scene-tagger)
(standard library + ffmpeg; the model engine adds `torch` and `transformers`). It walks a folder
and writes a `<video>.scenes.json` sidecar next to each movie with the scene markers it found:
positions (missionary, doggy, cowgirl, …) and acts (blowjob, fingering, creampie, …), each with a
confidence. Install once:

```bash
pip install "git+https://github.com/fc2vault/vault-scene-tagger"
pip install torch transformers numpy        # the model engine; see pytorch.org for a CUDA build
```

Then download the scene model in **Settings → License** (Pro) and run the tagger; it finds the
downloaded model on its own:

```bash
vault-scene-tagger "/path/to/library" --dry-run                      # what would run
vault-scene-tagger "/path/to/library/<Actress>" --engine phar        # one actress
vault-scene-tagger "/path/to/library" --engine phar --log-file tagger.log
```

It's resumable: re-runs skip videos whose sidecar is current, and Ctrl+C is safe. On an M1 Mac
it runs at about 3.5× realtime; a CUDA GPU is several times faster. Nothing else to do on the
Vault side: freshly tagged movies show up under **To review** by themselves, and a movie's
sidecar is synced into the markers cache the next time you open it. Without the model (free
version) the tagger can still write shot cuts (`--phase a`) or use skier233's AI server
(`--engine ai-server`); its README explains both.

## Project layout

```
serve.py         # the app + HTTP API (Python stdlib, single file)
index.html       # the entire front‑end (vanilla JS, no build step)
scan.py          # web scrapers + enrichment (123av, ffjav, javdatabase, jav.guru, fc2ppv‑db)
organize.py      # preview‑then‑apply library tidy (never deletes/overwrites)
dedupe.py        # find & merge duplicate actress records
data/catalog.db  # preloaded catalog (SQLite): actresses, works, tags, aliases, links
config.json      # your library path + port (gitignored)
docs/            # this guide + screenshots
```

## Building a release

`make_release.sh` assembles a clean, self‑contained, GitHub‑ready copy (app + scrapers + a fresh DB snapshot + a `start.command` launcher):

```bash
./make_release.sh            # full build — ships YOUR ownership flags. Personal.
./make_release.sh --rc       # shareable release candidate (see below)
./make_release.sh --public   # --rc, plus sanitized imagery. This is what's on GitHub.
```

All three strip the author's absolute paths from the shipped scripts. They differ in what the bundled catalog says about *you*, and in what the screenshots show.

### `--public` — the published build

Everything `--rc` does, plus:

- **Actress portrait blobs are cleared** from the catalog (text metadata is untouched).
- **Screenshots are swapped for a sanitized set** in `docs/images_public/`:
  - Library views (grid, multi‑select) keep their layout with the **poster art pixelated**.
  - The settings / scan / organize / import dialogs are **cropped to the dialog** — nothing needs censoring once the library behind them is out of frame.
  - The player and actress‑overview shots are omitted, and the docs that referenced them are pruned so no image link dangles.

Regenerate the sanitized set after taking new screenshots:

```bash
python3 tools/censor_shots.py docs/images /tmp/px     # pixelate poster art
```

`tools/censor_shots.py` finds photographic regions structurally rather than by hand‑placed coordinates — Vault's chrome is flat and near‑black while poster art is bright, textured and coloured — then boxes each region so a whole poster is covered even where detection only caught part of it. UI text stays sharp.

### `--rc` — the shareable build

Use this for anything public. It keeps the catalog as a **prebuilt metadata library** — every title, cover, tag, actress profile, portrait, alias and filmography link survives — while removing every signal about what you personally own or want:

| Removed | Why |
|---|---|
| `works.in_library` → `0` | marks the files you hold |
| `works.availability` → `available` | `missing` is your wishlist |
| `works.source` `fs`/`manual`/`user` → `catalog` | `fs` means "found on my disk" — that flag alone re‑identifies the whole owned library |
| `actresses.source` `manual`/`csv` → `catalog` | same, for hand‑curated actress rows |
| works rows with **no** metadata at all | their only content is "this code was on my disk" |
| `crawl` table | scraper bookkeeping, not movie or actress data |

`VACUUM` runs last so deleted rows can't be recovered from the freelist. External provenance (`bigboobs.pink`, `jav-forum:*`, `javfc2`, `jav.guru`) is deliberately kept — that's attribution, documented in `SOURCES.md`.

The build then **verifies itself and fails rather than shipping a leak**, re‑checking each item above plus: no personal paths or hostnames in any shipped text file, and no `.DS_Store` / `config.json` / `cache/`. A clean run ends with:

```
RC verify: clean (no ownership flags, no personal paths, no runtime cruft).
```

Watched state, favorites (movies **and** actresses) and ratings never enter this picture — they live in a **`userdata.json` sidecar** next to the DB, the single source of truth, kept deliberately **separate from `catalog.db`** so they survive a catalog rebuild/reimport and a browser cache wipe. They are your personal state and are never part of the shipped catalog.

**What a recipient gets:** their own scan populates the grid; the bundled catalog enriches whatever codes they happen to own. Since nothing is flagged `missing`, they get no wishlist ghosts until they enrich an actress themselves.

## Privacy & safety

- **No cloud, no telemetry.** Vault talks to the internet only when *you* press a scrape/enrich button. It has no accounts and phones home to nothing. A Pro key is checked offline.
- **It is not access‑controlled.** The server binds `0.0.0.0`, so anyone who can reach the port gets the whole library — there is **no login and no TLS**. Two endpoints act on the host machine rather than just returning data:
  - `POST /api/open` — opens a file in your desktop video player
  - `POST /api/reveal` — reveals it in Finder

  Treat reachability as the only control. Keep it on a network you trust (LAN or a VPN back to your LAN); do **not** port‑forward it or put it on a public/coworking Wi‑Fi. To pin it back to this machine only, change the bind in `serve.py`:

  ```python
  srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)   # was "0.0.0.0"
  ```
- **Non‑destructive by design.** Organize *moves* junk/duplicates into recoverable folders and never deletes or overwrites; enrichment is fill‑if‑empty and never clobbers your edits.
- **Secrets** (the optional fc2ppv‑db session) are stored in the **macOS Keychain** (encrypted), or a `0600` file elsewhere — never in the repo.
- Web scraping honors each site’s access model; the one human‑checked source is only ever reached through a session **you** authenticate.

## Troubleshooting

| Symptom | Fix |
|---|---|
| “no valid library folder” on start | Set `library` in `config.json`, or `--library "…"`, or **Settings → Library**. |
| No posters / durations | Install `ffmpeg`/`ffprobe` and ensure they’re on `PATH`. |
| Slow first boot | Normal over SMB — it caches after the first walk and boots instantly next time. |
| A movie didn’t appear after copying it in | **Scan library** re‑indexes the filesystem first; new folders show up then. |
| A movie you own still shows Status **missing** | Its catalog row is stale. Any scan/rescan reconciles it — the log prints `[db] N work(s) marked available`. The import pipeline now marks them as it goes. |
| Can’t reach it from your phone | Use the machine’s LAN IP, not `127.0.0.1`; check both devices are on the same network (or the VPN is up) and that a firewall isn’t blocking the port. |
| Phone tab reloads or dies on a huge library | Should not happen — the grid renders in batches. If it does, narrow the view with a filter and file it as a bug. |
| Newly added folder not on top of “Newest added” | Vault sorts by **first‑seen**; a freshly‑dropped `<CODE> - Name` folder is treated as new. |
| Wrong port after launch | `serve.py` remembers the last `--port` in `config.json`; edit it back or pass `--port 8730`. |
| fc2ppv‑db **Test connection** fails with a fresh cookie | Vault tries the request over both IPv4 and IPv6 (a `cf_clearance` cookie is bound to the exact address that solved Cloudflare's check, and dual‑stack machines don't always use the same one). If it still fails, the cookie has expired — re‑copy it — or your browser is on a different public IP than the server (e.g. a VPN). |
| fc2ppv‑db **Test connection** fails in FlareSolverr mode | The resolver must be running, and Vault must be able to reach its URL. It has to egress through the same public IP as Vault. A URL without `http://` is accepted; `/v1` is added if you leave it off. FlareSolverr is a [separate program you install yourself](#optional-flaresolverr) — Vault neither bundles nor starts it. |
| A scan says **“fc2ppv‑db pass skipped”** | No resolver and no saved session, or the resolver didn't answer. The titles pass still ran; set up [one of the two routes](#optional-flaresolverr) and scan again. Everything else works without it. |
| A movie stays unidentified after a scan | Open it: the panel says **“fc2ppv‑db · no performer listed”** (the site genuinely has nobody — scanning again won't help) or **“not checked yet”** (it will). |

---

## A note on this build

This is the **public build**. Its screenshots are sanitized — poster art in the library views
is pixelated, and the settings/scan/import dialogs are cropped to the dialog itself — and the
bundled catalog carries no actress portraits, only text metadata (names, aliases, measurements,
titles, tags, dates, cover URLs). Vault itself is unchanged: **Enrich from web** fetches
portraits into your own local catalog, and posters are generated from your own files, so your
install looks like the real thing.
