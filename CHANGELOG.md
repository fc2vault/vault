# Changelog

## 1.6.0 — Free and Pro, the scene model, run at login

### The scene model downloads itself

**Settings → License** has a new **Scene model** section. With a Pro key active it shows what is
installed and what the newest build is, and one button fetches it: Vault sends the key to the
project's key server, which checks the signature and the expiry and answers with a short-lived
download link; the zip is unpacked into a per-user folder (`~/Library/Application Support/Vault/models`
on macOS, `%LOCALAPPDATA%\Vault\models` on Windows, `~/.local/share/vault/models` on Linux).
`vault-scene-tagger` looks in that folder on its own, so a Pro user runs the tagger with no model
flag. New module `vault_model.py`, standard library only; the zip is checked before unpacking (one
`vault-r<N>` folder with a `config.json`, no paths that leave it) and a half-finished download never
replaces an installed model. `GET /api/model`, `POST /api/model`, `GET /api/modelprogress`.

### Website and key server

Vault has a website (https://fc2vault.vaultkey.workers.dev) and a key server
(https://vault-key.vaultkey.workers.dev): supporters log in with Patreon there and get their Pro
key on the spot, no e-mail, nothing stored. Both are in the repo (`docs/index.html`; the worker
lives beside the app source, not in this build). Settings → License links to both.

### vault-scene-tagger is published

The tagger is its own repository now (github.com/fc2vault/vault-scene-tagger): `pip install` it,
add `torch` and `transformers` for the model engine, point it at a folder. The user guide has the
three commands.

### Run at login: `--install-service`

`python3 serve.py --install-service` registers Vault as a per‑user login service and starts it:
a launchd agent on macOS, a `systemd --user` unit on Linux, a Task Scheduler task on Windows
(`pythonw.exe`, no console window). `--service-status` and `--uninstall-service` go with it.
New module `vault_service.py`, standard library only. On macOS the installer reads the log after
starting and says so when the system refused access to a folder under Documents, Desktop,
Downloads or a removable volume, with the two ways out.

### Free and Pro

Vault is free for up to 250 movies. A bigger library shows the 250 movies Vault saw first; the
count at the top reads "Free: 250 of N" and opens the new **Settings → License** tab. A Vault Pro
key unlocks the whole library, fc2ppv‑db lookups and Enrich from web. The key is checked offline
with an Ed25519 signature (`vault_license.py`, standard library only): no license server, and a
key holds only its tier, last valid day and a random id. Unlocking applies at once from the cached
library walk; removing the key goes back to the free view. Two signers are accepted
(`PUBLIC_KEYS`): keys issued by hand, and keys the supporter key server hands out after a
Patreon login; dropping a signer from the list revokes every key it signed.

### Multi-part movies: markers were clamped to the wrong part's length

The first marker placed in a part without a sidecar took its duration from the movie's primary
file (the biggest part), not from that part. Every marker in the part was then clamped to that
length: placed or dragged past it, the marker snapped back to the end. The part's own file is now
probed. Six sidecars had a wrong duration stored and were corrected (backups kept); two markers
had already been clamped (FC2-PPV-4536489-1 creampie at 1:03:31, FC2-PPV-4996510_5 blowjob at
17:05) and need placing again.

### Confidence next to the current scene

The label in the player's top-left corner now shows how sure the model was about an unconfirmed
suggestion: **AI 81%** instead of just **AI**. Confirmed and hand-placed markers show no badge,
as before.

### Marker chips no longer pile up

On a long movie with many markers the chips above the seek bar sat on top of each other and were
hard to click. A chip that would overlap its neighbour now moves up a row (up to 3), with its stem
stretched down to its exact spot on the bar. If that still isn't enough (40 markers on a 2½ h
movie), the chips shrink to 28 px and use up to 4 rows. Movies with few markers look as before.

### "To review" sees markers written by the tagger

The library list is cached and was only rebuilt after a change made in Vault or a rescan.
vault-scene-tagger writes its sidecars from outside, so freshly tagged movies never showed up
under "To review" or "AI pre-tagged" until the server restarted. The list now carries a
fingerprint of every sidecar (mtime and size) and is rebuilt when one changes. The check is a stat
per video: about 20 ms for the whole library once the NAS has the folders cached.

### Creampie markers jump in 10 s early

A creampie marker sits at the pull-out, where the event is visible. That is also where its
training clip starts. Jumping straight there skips the build-up, so clicking the chip, ⏮ and ⏭ now
land **10 s before** it. Not in training mode: while reviewing, a jump lands exactly on the marker,
so you see what the model saw.

- Display only: the stored `t` doesn't move. Storing it 10 s earlier would turn every confirmed
  creampie into a training clip of plain missionary.
- The lead-in is per label (`LEAD_IN` in `setupScenes`); every other label stays at 0.
- ⏮/⏭ step through the jump times in order. A creampie 5 s after another marker now jumps *before*
  it, and stepping by marker order would have skipped it.

## 1.5.2

### Sources run in priority order: fc2ppv-db → fc2db.net → 123av/ffjav

The intended priority was always fc2ppv-db first, fc2db.net second, the shell scrapers last.
For title, cover and date it was not what happened. fc2ppv-db overwrites, but fc2db.net and
ffjav both only fill **empty** fields — so whichever runs first wins, and the shell pass ran
first. ffjav's cover and date (scraped from a search-results page, the source of the
wrong-date bug) always beat fc2db.net's official thumbnail and sale date. The real priority
was fc2ppv-db > ffjav > fc2db.net.

- The pipeline now runs the passes in priority order. 123av is the only English-title source and
  tags are a union, so neither is affected by the reorder.
- The player's **Scan metadata** runs the same order, and its shell pass no longer uses
  `force=True`: forcing the lowest-priority source to overwrite would undo the other two.
- Running the titles pass last exposed a counter bug in waiting: it reset `hits/absent/failed`
  and would have wiped the cast passes' totals from the final summary. A held pass now keeps the
  job's totals (the pipeline resets them once), and creates any it finds missing rather than
  raising `KeyError` mid-scan.
- The scan dialog and Settings describe the passes as **1 · 2 · 3** in the order they run, and the
  summary says "not found on fc2ppv-db / fc2db.net" now that `absent` covers both sites.

**Limit:** this decides who fills an *empty* field. A field an earlier scan already filled stays.
Vault does not record which source wrote each field, so it cannot tell an ffjav cover it may
replace from one you would want kept.

4 tests (142 total), all failing against 1.5.1.

## 1.5.1

### Settings → Sources, regrouped

fc2ppv-db.com and fc2db.net lived in two different places, and the resolver's setup was baked
into the fc2ppv-db section as if it belonged to that one site. The pane is now four groups:

1. **FlareSolverr** — URL, save, and a **Check resolver** that asks it directly and reports its
   version (new `/api/solver/check`, probing fresh rather than trusting the 30 s cache).
2. **Sites behind Cloudflare** — a card each for fc2ppv-db.com (its access choice and cookie box
   live here now) and fc2db.net (its on/off and a new **Test**, `/api/fc2dbnet/test`).
3. **Other sources** — the four plain scrapers.
4. **Scan the library** — see below.

Source toggles now save on click. A switch that sits in its own card cannot wait for a Save
button at the bottom of a different section.

**A coupling this exposed:** fc2db.net only fell back to the resolver when *fc2ppv-db's* access
mode happened to be "FlareSolverr". With the resolver now its own setting, any site behind it
uses it whenever a URL is saved.

### Every scan button checks every source

The toolbar and the player already ran titles → fc2ppv-db → fc2db.net. The Settings buttons ran
one site each. Now there is **one pipeline**, `_start_pipeline`, and every library scan goes
through it; the player runs the same three passes for one code. The two single-source
handlers are gone, and their routes (`/api/fc2db/scan`, `/api/fc2dbnet/scan`) are aliases of
`/api/scanall`, so nothing that called them breaks.

Settings' **Scan missing** takes titles only for movies with a gap, and each cast source only
where its own stamp is empty; **Re-read everything** asks both cast sites again. Every check
that can refuse runs before the scan slot is claimed, so the old leak has nowhere to come back.

Verified on the live server: the resolver check reports FlareSolverr 3.5.2, the fc2db.net test
returns まりか for FC2-PPV-4988506, and a one-movie run through the pipeline skipped both
already-read sites and published cleanly.

Two buttons are deliberately *not* this pipeline: **Wishlist covers** (javfc2.xyz, for movies you
don't own) and **Enrich from web** (an actress profile, not a movie). Both honour their source
switches.

138 tests.

## 1.5.0 — review 3, and fc2db.net

### fc2db.net: a second source for the cast

`docs/REVIEW-3.md` asked for it, and the reason is one movie: FC2-PPV-4988506, which fc2ppv-db
lists as "情報がありません" — no performer. fc2db.net's page for the same code names her
(まりか), plus seller, sale date, runtime, cover and ten tags. Verified against the live page
before writing a line of parser.

- **Plain urllib, like 123av and ffjav.** Cloudflare fronts the site but does not challenge an
  ordinary GET. FlareSolverr is only the fallback for a challenge or the 18+ wall, and runs on
  its **own** browser session (`vault-fc2dbnet`) — one session name is one Chromium, and every
  fc2ppv-db scan destroys `vault-fc2db` when it finishes. The 18+ flag
  (`mdb_age_gate_confirmed=1`, domain `.fc2db.net`) is sent only to this site.
- **Parsed from the JSON-LD `VideoObject`**: cover is `thumbnailUrl` and nothing else (the
  recommendation carousel reuses the same `wp-post-image` class for *other* movies); title is
  the page heading, since `og:title` is just `FC2PPV-<digits>`; tags from the `/work-tags/`
  links. No identifying field → `{}`, the lesson of fc2ppv-db's "FC2PPV Database" shell.
- **A trap worth recording.** Every real work page carries a Turnstile widget on its comment
  form and a `challenge-platform` script. The old detector cleared the page only because those
  sit ~50 KB in and it reads the first 4 KB. The new check clears it because the VideoObject
  is present; a test puts the markers at the top to prove it.
- **It fills gaps and never overrides.** Title, cover, date, seller, runtime: only when empty.
  Censorship: only when unset and not hand-set. Cast: only into an **empty** lead not marked
  `manual` — and fc2ppv-db's own stamp (`fc2db_at`) is deliberately *not* a reason to skip,
  since "fc2ppv-db lists nobody" is exactly what this source is for. Exact-name match before
  insert, the `/actress/<id>/` URL stored as a link, no aliases grafted, nothing removed.
- **Its own stamp**, `fc2dbnet_at`. A real 404 is stamped; a challenge, age wall or network
  failure is not.
- **Wired into the existing buttons**, not a fourth scan: the toolbar runs it as a third pass
  with its own checkbox and count; the player looks it up on every click; Settings gets two
  buttons *named after the site*, because "Scan missing" next to them means a different stamp.

### The five things the review found still wrong

All reproduced in source before fixing.

- **The grid reloaded before the index did.** Both scan workers lowered `running`, the poller
  reloaded `/api/data` at once, and *then* the index was rebuilt — so the toast said "N updated"
  and the grid showed the old cast. The worker now owns the flag for the whole job: every pass
  honours `hold`, and `running` drops only after `rebuild_index()` has published.
- **The title pass still swallowed failures** (`except Exception: pass`). It now rolls back
  and records `code: repr(exc)` like the fc2ppv-db pass.
- **The javdatabase and jav.guru boxes did nothing.** `enrich_actress` takes `sources` now.
  Every checkbox under Sources is a real switch.
- **The player still overlapped a library scan** — it skipped only the fc2ppv-db half and
  wrote the shell pass alongside. It now claims the scan slot before writing anything, and
  returns the same 409 as every other button. The toast carries what was refused or empty
  alongside what changed, instead of "Updated: title" swallowing it.
- **The dialog's counts ignored the ticks** and `truncated` was never read. Counts now follow
  the ticked rows; past the 4,000-row cap the dialog says so instead of silently scanning the
  newest 4,000.

Also: a saved `sources.json` now picks up built-ins added since — without that, fc2db.net would
have been missing, and therefore off, for every existing install.

26 tests (136 total), all failing against 1.4.2.

### Where things live now

The project folders were renamed to say what they hold: `Renamers/` → **`vault/`** (this app —
it outgrew the rename scripts it started as), `sceneanalyzer/` → **`vault-tagger/`**, and every
generated build moved under **`builds/`**. Nothing in the code hard-coded a folder name;
`serve.py` finds everything relative to itself. What changed is where the build scripts write:

- `make_release.sh --rc` / `--public` → `../builds/vault-release` (the shareable release)
- `make_release.sh` (your own flags kept) → `../builds/vault-full`
- `make_app.sh` → `../builds/Vault.app`, packaging `builds/vault-full`

## 1.4.2

### the-snake folded into doggy

Prone bone is doggy with her lying flat — the same entry from the same side, and the camera
often cannot tell them apart either. It had 11 manual examples in 10 movies, which is not a
class, it is noise with a name. Folded the way reverse-cowgirl was folded into cowgirl.

Three places, so nothing can re-introduce it:

- `moments.py` maps P-HAR's `thesnake` / `snake` / `pronebone` to `doggy`. (P-HAR's own class
  list in `phar.py` is untouched — that is the model's output vocabulary, not ours.)
- `SCENE_LABELS` in `serve.py` and the two vocabulary lists in `index.html` drop it, so the
  marker editor can no longer set it.
- Every sidecar was rewritten in place: **110 markers across 83 files**, plus 4 entries in
  `rejected`. `source` is never touched, no sidecar is deleted, each write is atomic through
  a temp file, and every touched file is backed up under
  `~/vault-tagger-runs/snake-fold-backup-<timestamp>/`.

The label set is now **10**: 5 acts (fingering, blowjob, titjob, toy, creampie) and 5
positions (missionary, doggy, cowgirl, standing, scoop-up). doggy gained those 11 markers.

The 100-movie breadth batch was mid-run; it invokes a fresh interpreter per folder, so every
folder after the edit writes `doggy` directly, and the sweep caught the ones already done.

## 1.4.1 — review 2

Every item below was reproduced before it was touched. `docs/REVIEW-2.md` is the source.

### The 18+ branch returned two values from a three-value function

`solver_get()` gained a `status` in 1.3.48 and this one branch was never updated:

```python
return None, ("...18+ confirmation...", code)      # two values
html, err, status = solver_get(...)                # three expected
```

Every call raised `ValueError` **before** `_error` was set, so the abort condition
(`"18+" in data["_error"]`) never fired. Reproduced on seven codes: `done 7/7, failed 7,
error ""` — the scan walks the entire library behind a wall it already hit, one solver round
trip per code, stamping nothing, and the next run repeats it. `Test connection` turned the
same `ValueError` into a 500 and said "Request failed" instead of naming the age gate.

Fixed, and the message now says what is actually true: the `age-verified` cookie was not
accepted. (The first fix attempt rewrote the message and left the parentheses — the new test
caught it.)

### The Settings scan could wedge every scan until restart

`_fc2db_scan` called `claim_scan()` and then returned 400/503 for an empty URL, an
unreachable resolver or a missing cookie, without releasing. `PROGRESS["running"]` stayed
true forever and every later scan, wishlist backfill and scan preview answered "a scan is
already running". All early exits now go through a `_bail()` that releases first. Verified
live: a scan pointed at a dead resolver returns 503 and leaves `running: false`.

### A page with no cast was thrown away

The hit test was `title_jp or actresses`. A page carrying a date, cover, tags and seller but
no performer failed it, so those fields were discarded, a stub row was written, and
`fc2db_at` was stamped — meaning "Scan missing" would never come back for it. The test is now
`_has_fields()`: anything the parser actually returned counts. `{}` and `_missing` stay absent.

### One progress dict, two passes

`scan_library()` reset `hits` but not `absent`/`failed`, so a titles-only run reported the
*previous* fc2ppv-db run's numbers as its own. `scan_fc2db()` then reset `hits`, so a combined
scan's "Done — N updated" dropped everything the titles pass wrote. Both now reset what they
own, and a `hold` flag keeps `hits` additive and `running` true across the hand-off — closing
the window where a 1.2 s poll painted "Done" mid-scan.

### The player scan no longer steals the resolver

`scan_fc2db`'s `finally` calls `solver_release`, which destroys the shared `vault-fc2db`
session. A player click during a library pass therefore cost that pass a cold Chromium, and
both wrote the catalog uncoordinated. A one-code lookup is now refused while a library scan
is running, with the reason shown.

### Fold by actress applied rows the dialog never showed

Organize was switched to an allow-list; fold was not. It applied every move whose id was
absent from `skip`, and the client can only send ids it rendered — so past `ORG_CAP` the
unrendered rows moved anyway. Fold now takes the same `allow` list, and both dialog footers
stop claiming hidden rows will run.

### Smaller

- **The scan checkbox counted the wrong set.** It showed rows with an empty actress name; the
  server selects on `fc2db_at`. A folder-named movie with no stamp *is* fetched and a nameless
  one with a stamp is *not* — the label promised the opposite of both. The preview now carries
  `fc2db_read` and the count matches what will run.
- **Saving a cookie wiped the User-Agent.** The form always posts `ua`, and `secret_set("")`
  deletes the Keychain item. Blank is now skipped.
- **The source toggles did nothing.** Settings wrote `sources.json`; nothing read it.
  `scan_code(sources=...)` honours it, `enabled_sources()` supplies it, and the stale comment
  promising a "generic title fetch" for custom rows is gone — there is no such fetch.
- **A failed code no longer rides along.** `scan_fc2db` rolls back and records `code: repr(exc)`
  instead of swallowing it, and portraits download *after* the commit rather than holding
  SQLite's write lock through a 20 s HTTP call.
- **`make_release.sh` shipped REVIEW-2.md** — it removed only `REVIEW.md`. Now `REVIEW*.md`.
- The user guide's §8 claim that the toolbar's **All movies** re-reads every fc2ppv-db page
  was wrong; that is **Settings → Scan all**. Both are now described accurately, along with
  how the player button differs.

### Left alone, deliberately

The review's "one pipeline for the three scan buttons" is a refactor, not a defect; the
semantics are now aligned (`missing` = never read, everywhere) and the remaining differences
are documented rather than hidden. `rebuild_index()` still runs on the preview request — that
walk is what makes newly-dropped movies appear, which is the point of the button. `ORG_CAP`
(4,000) is no longer dangerous now that both apply paths are allow-lists; it remains a
discovery limit, and the library is 3,533.

14 tests (110 total); all 14 fail against the previous build.

## 1.4.0 — release

**FlareSolverr is documented as what it is: optional, and yours to install.** The README now has
an *Optional: FlareSolverr* section under Requirements saying plainly that Vault neither bundles
nor starts it, that it exists to serve exactly one source (fc2ppv-db, which supplies the cast),
and that pasting a browser session is the no-extra-software alternative. It links upstream for
install instructions rather than reproducing them, and states the two constraints that actually
bite: it has to egress through the same public IP as Vault, and Vault only accepts a resolver on
loopback or a private address because the server has no authentication. Three troubleshooting
rows and a pointer in the user guide match.

Collects everything since 1.3.46 into a shippable build. `serve.py` now carries
`VAULT_VERSION`, the boot banner states it, and `make_release.sh` writes a `VERSION` file from
that one source.

**The catalog gained two columns**, so the release script had to learn about them. `actress_src`
marks a hand-set performer and `fc2db_at` records when a page was read — both are personal
signal, so an RC build now clears the hand-edit flags and replaces the scan timestamps with a
flat `'catalog'` marker (keeping "this page has been read", which saves a recipient hours).
Two new verify probes fail the build if either survives.

**Also in the build:**

- The 96-test suite ships. `helpers.load()` accepts the dev layout (`webui/serve.py`) and a flat
  release, so the shipped suite runs where it is shipped — verified: 96 pass inside the RC.
- `refolder_fc2.py` ships; the cleaner tests exercise it and it was missing.
- The anonymizer covered only `"$OUT"/*.py`, so a path written into the README or a test walked
  past it and only the final verify gate caught it. It now covers the shipped `.md`, `.sh`,
  `docs/` and `tests/` files too — and the two real leaks it was hiding (a library path in
  `README.md`, a resolver address in `test_serve.py`) are fixed at the source.
- `docs/REVIEW.md` no longer ships: working notes, not user documentation.

**The manual is current again** — `docs/USER_GUIDE.md` covers the a/s/d shortcuts, layered Esc,
amber unconfirmed markers and ✓ Confirm, the fc2ppv-db pass in both Scan buttons, what the
updated / not-in-fc2ppv-db / could-not-be-read counts mean, the source-precedence rules, the
"no performer listed" vs "not checked yet" line, and the open-file rule when deleting.

Verified by building and running it: RC verify clean, 96 tests pass inside the build, and the
server boots from the bundled catalog and identifies a movie dropped into an empty library.

## 1.3.56

### An empty page is an answer, and now it is remembered

The 450-movie backlog scan finished `341 updated, 109 not in fc2ppv-db, 0 failed` — and then
113 movies were still marked "never checked". `fc2db_at` was stamped inside `_apply_fc2db()`,
which only runs on a hit, so every code the site has no page for stayed unstamped and every
future scan would re-read all of them. Five minutes of resolver time per run, growing with the
library.

`_stamp_fc2db()` is now its own helper and the absent branch calls it too. A transport failure
is deliberately **not** stamped — a resolver that was down has to be retried, unlike a page
that simply is not there. The "All movies" scope still re-reads everything on demand.

Two tests pin both halves (96 total). The 114 already-asked codes in this catalog were stamped
by hand, so nothing is left falsely pending.

### What the scan actually found

341 pages read, and the library gained **3** performers. The residue really was the residue:
of 513 movies still without an actress, **400 are confirmed to have nobody listed on
fc2ppv-db** and the rest are new arrivals. Automatic identification is essentially done here —
what is left is hand work, and the player now says which is which.

## 1.3.55

### Saying why a movie has no performer

FC2-PPV-4988506 looked like a resolver failure. It was not: the page was read at 15:48:48, and
its title, 14 tags, cover and date all landed. fc2ppv-db's own page for that code says, in so
many words, **"Actresses — No information"**. There is nobody to fetch.

The problem was that Vault could not say so. "fc2ppv-db lists no performer" and "nobody has
ever asked fc2ppv-db" rendered as the same blank, so every unexplained blank looked like
broken plumbing. `fc2db_at` now rides along on each item and the player prints one of:

- `fc2ppv-db · no performer listed` — read on <date>, the site has nobody. Hand-identify or
  leave it.
- `fc2ppv-db · not checked yet` (amber) — never read; Scan metadata will fetch it.

On this library that splits the 519 unidentified movies into **64 genuinely unknown to
fc2ppv-db** and **455 simply not looked up yet**.

## 1.3.54

### fc2ppv-db is the primary source for the cast, in fact and not just in intent

Audited every field. For **title, release date, producer and cover** fc2ppv-db already wins —
`_apply_fc2db()` overwrites them, so running it after the shell scan always corrects 123av and
ffjav. Tags are additive and always land. Censorship obeys the page unless you set it by hand.
Two places did *not* match the intent:

**1. The lead performer was first-writer-wins.** `UPDATE works SET actress_id=COALESCE(actress_id,?)`
— so a lead guessed from a folder name could never be corrected by the page that actually
knows. Now fc2ppv-db replaces it, except where `actress_src='manual'`.

**2. Worse: scans asked "has no actress?" to decide what to look up.** That quietly made this
source the *last* resort — a movie named by its folder or an import was never offered to
fc2ppv-db at all, so it never got that page's co-stars, tags, cover or date either. "Still to
do" now means **"page never read"**, tracked per movie.

Two columns, added by `_ensure_works_schema()`:

- `fc2db_at` — when the page was last read. Stamped even when the page has no cast, or every
  scan would re-read the same empty pages forever.
- `actress_src` — who set the lead. `'manual'` is never overwritten.

The migration backfills so the first run afterwards stays proportionate: a movie with a cast
row, or one fc2ppv-db itself created, counts as already read. On this catalog it marked
**3,765 rows read and protected 93 hand-set actress links**, leaving **509 to look up** —
against 507 under the old rule, so the change costs 2 extra lookups rather than 3,000.

Catalog backed up to `catalog.db.before-authority` before the migration ran.

Seven tests (94 total); nine fail against the previous build.

## 1.3.53

### Where "not-a-list" came from, and why it could happen

`_scan_library()` took the posted `codes` and did `cl = list(codes)`. Hand that a **string**
and `list()` walks it character by character — so `scan_code()` was called with `"n"`, then
`"o"`, then `"t"`… and each one did `INSERT OR IGNORE INTO works`, creating a catalog row per
letter. `INSERT OR IGNORE` swallowed the repeats, which is why `not-a-list` left exactly eight
rows: `n o t - a l i s`.

The titles on them (`JURA-215 …`, `Caribbeancom …`) came from the old `fetch_ffjav()`, before
the card-scoping fix: a one-letter search returned a page of unrelated hits and it took the
first title and the first date it found. That half is already fixed; the string-exploding was
not.

Three guards, outermost to innermost:

- `/api/scanlib` rejects a non-list `codes` with 400 (and releases the scan slot).
- `scan_library()` raises `TypeError` on a string rather than quietly iterating it.
- **`scan_code()` refuses anything shorter than 3 characters or without two alphanumerics** —
  the row is the actual damage, so the last guard sits where the row is written, not only at
  the callers.
- The walk also skips a folder whose name is under 3 characters; it is a stray directory, not
  a release. (Real non-FC2 codes in the library — `N0627`, `MIFD-711` — are longer, and a test
  pins that.)

The eight rows and their 7 orphaned tags are deleted, backed up to `junk-code-rows-backup.json`
first.

### "Scan metadata" in the player looks up the actress too

Same gap as the toolbar button in 1.3.50, one movie at a time. `_scan_one()` now runs the
fc2ppv-db lookup after `scan_code()`, through the configured transport, using a **local**
progress dict so it cannot disturb a scan running in the background. Because it is one
deliberate click it looks up every time, not only when the performer is missing.

On a hit the client reloads from the server instead of patching the four title fields — the
cast is written straight to the catalog and the item has to come back whole. A miss says
*"not in fc2ppv-db"*; an unreachable resolver says so instead of silently doing nothing.

Verified both ways: FC2-PPV-4972300 came back with Kugaya Megumi as `main`, 8 tags, date and
censorship; FC2-PPV-3096419 (one of the soft-404 codes) reported `not in fc2ppv-db` and wrote
nothing.

Four tests (89 total); two fail against the previous scanner.

## 1.3.52

### _needs_review is a holding pen, not a movie

It sits at the library root with nine loose, un-coded files in it. The walk saw a directory
that directly holds video, so it indexed the whole thing as one release whose code was
literally `_needs_review` — a catalog row, a card in the grid, and a wasted fc2ppv-db lookup on
every scan.

- Added to `FOLD_RESERVED` in `serve.py`, so the walk never descends into it.
- Added to `SPECIAL_DIRS` in `organize.py`, so the organizer leaves it alone too. The two sets
  stay separate on purpose: `FOLD_RESERVED` also lists staging folders the organizer is
  supposed to tidy.
- Dropped the stale `works` row (no tags, no cast, nothing else referenced it).

The files themselves are untouched — all nine are still there. The index went 3,521 → 3,520.

Three tests (85 total), all failing against the previous skip list.

## 1.3.51

### fc2ppv-db answers "not found" with HTTP 200, and we were storing it

Chasing why the toolbar scan "returned no results", the scan log turned up something worse than
no results: **453 hits on 511 codes, but only +7 movies identified**. Fetching one of those
pages settled it. For an id it does not have, fc2ppv-db serves its bare site shell with
**status 200** — `og:title` is `FC2PPV Database`, `og:image` is one shared placeholder, no video
link, no cast, no `mosaicStatus`.

`parse_fc2ppvdb_video()` read that as a movie. `title_jp` came back truthy, `scan_fc2db()`
counted a hit, and `_apply_fc2db()` wrote it. **52 catalog rows were titled "FC2PPV Database"**
and 63 carried the placeholder cover. (The 1.3.48 404 handling never fired here — nothing ever
returns 404.)

- The parser now rejects the shell: a site-name `og:title` is dropped, the `opengraph-image`
  placeholder is never stored, and a page with no title, cast, date, cover or `mosaicStatus`
  returns `{}` — so it is counted **absent** instead of found. `mosaicStatus` counts as proof
  the page was real, since the shell has none.
- The 52 titles and 63 covers are cleared. Rows backed up to
  `placeholder-rows-backup.json` first. Censorship was left alone: `censorship_src` was NULL on
  all 52 and the "uncensored" came from the title harvest, not from these pages.
- Verified on FC2-PPV-1088107, which produced a bogus hit before: now `hits 0, absent 1`, and
  the row stays empty.

### "Test connection" was doing more than testing — you were right

In `solver_get()`, an exception from `_solver_ensure()` returned a hard failure instead of
retrying, and `sessions.create` had a 30 s timeout. A freshly started FlareSolverr can take
longer than that to spawn its first Chromium: the call times out, but the resolver finishes the
session anyway — so pressing **Test connection** absorbed the cold start and the next scan found
the session already there. That is exactly what you observed. The create timeout is now 90 s and
a failed ensure retries instead of giving up.

### A skipped fc2ppv-db pass no longer skips quietly

`_fc2db_ready()` is probed fresh at scan start rather than from the 30 s reachability cache (the
modal can sit open while the resolver comes up or goes down), and when the pass is skipped the
reason lands in `PROGRESS.note`, shown in the panel and as a toast. Before, a scan started while
the resolver was down ran titles-only and reported plain success.

Four tests (82 total); three fail against the previous parser.

## 1.3.50

### "Scan library" now fetches the actress too

The toolbar button ran `scan_code()`, which only knows 123av (English title) and ffjav (JP
title, cover, date). fc2ppv-db — the only source that has the **performer** — lived solely
behind Settings, so the obvious button never filled the field people actually want.

`/api/scanlib` now runs a second pass after the titles pass, through whichever fc2ppv-db
transport is configured (FlareSolverr or the saved cookie):

- It visits **only the selected codes that still have no performer**, resolved from the
  catalog rather than the in-memory index, so re-pressing the button stays cheap.
- It is skipped, not failed, when fc2ppv-db is not reachable — 123av and ffjav never needed
  it, and the scan must not become dependent on a resolver being up.
- The modal shows it as a checkbox with the count it would visit (*"Also look up fc2ppv-db for
  the actress — 89 still without one"*), disabled with the reason when it cannot run. The
  lookup is ~3 s per movie, so that number is the honest cost estimate, and it is opt-out.
- `PROGRESS` carries a `phase`, so the bar says `Titles · 12/40` then `fc2ppv-db · 3/9`
  instead of appearing to restart. `running` is held across the hand-off; otherwise the poller
  saw the gap between the two passes and reported the whole scan finished.

Verified end to end on one movie: FC2-PPV-4977133 came back with its title, release date,
censorship, 12 tags and Takase Kasumi linked as `main`.

Three tests (78 total) cover the second pass's code selection, including the chunking a
whole-library run needs — SQLite caps host parameters per statement and this list can be 4,000
codes long.

## 1.3.49

### Deleting the movie you are watching no longer copies it

Deleting FC2-PPV-4973873 hung the dialog for minutes and left **two** 2.72 GB copies of it in
`_vault_trash`. The chain:

1. The player was still streaming the file, so the server held an open handle on it.
2. This SMB share refuses to rename a folder while a file inside it is open — `os.rename`
   returns `EACCES`. (Measured on the share: a plain folder rename takes 0.375 s; the same
   rename with one open file inside fails instantly with errno 13.)
3. `shutil.move()` swallows that and **copies** instead. 2.7 GB over SMB, with the HTTP
   request held open the whole time — hence the stuck "deleting" modal.
4. A second click found the source still there and the destination already created, so it
   timestamped the name and copied the whole movie a second time.

Three fixes:

- **`_rename_or_explain()` replaces `shutil.move()`.** It renames, falls back to a copy only on
  `EXDEV` (a genuinely different volume), and turns `EACCES`/`EPERM`/`EBUSY` into
  *"… is still open — close the player and try again."* A trash move is now instant or it is an
  error; it is never a silent multi-minute copy.
- **The player lets go of the file before the request.** `deleteMovie()` pauses the video and
  drops its `src` first, so the handle is gone before the rename is attempted.
- **One delete at a time.** `_TRASH_LOCK` makes a second click return `409` instead of starting a
  parallel move, and the target list is re-checked inside the lock.

The delete itself had completed, for the record: the catalog rows and the library folder were
gone, and only the browser's cached `/api/data` still showed the movie.

Four tests (75 total); they fail against the previous `serve.py`.

## 1.3.48

### "Not in fc2ppv-db" is not "Cloudflare failed"

A scan reported `457/457, 21 updated` and said nothing about the other 436, which reads like
the Cloudflare bypass silently gave up. It had not. Two changes so the next scan answers the
question itself.

**A 404 is now a miss, not a failure.** `solver_get()` accepted only `solution.status` of 200 or
`None`; anything else became *"Cloudflare check was not solved"*, destroyed the FlareSolverr
session and retried. So a code the site simply does not carry cost a fresh challenge, and three
such codes in a row hit the `streak >= 3` guard and aborted the whole scan — on a resolver that
was working perfectly. A 404/410 with no challenge in the body now returns cleanly, keeps the
session, and does not count toward the abort streak. `_get_cf()` returns `(html, status)` for the
same reason on the cookie path, where a 404 used to surface as *"no response from fc2ppv-db
(network, or Cloudflare refused)"*.

**The scan counts the three outcomes apart.** `PROGRESS` gained `absent` and `failed` beside
`hits`, and the scan panel now ends with *"Done — 21 updated, 436 not in fc2ppv-db, 0 could not
be read."* The connection test says `fc2ppv-db has no page for <code>` instead of implying the
resolver is at fault.

Three tests cover it (71 total); all three fail against the previous scanner.

## 1.3.47

### Esc closes one layer, not all of them

Esc did already close the player and the actress sheet — but it closed *both*, in one keystroke.
Open a movie, click the actress to see her filmography, press Esc, and you were back on the grid
instead of back on the movie. `escTop()` now walks the stack and consumes exactly one level:

confirm dialog → marker editor → fullscreen (left to the browser) → column menu → actress sheet →
settings / organize / enrich / scan → import (never while it is writing) → player.

Two things fell out of it:

- Settings, Organize, Enrich and Scan had no Esc at all; they do now.
- Esc inside the metadata editor called `openPlayer()` to discard the edit, and the old global
  handler then closed the player it had just reopened. The editor now stops the event.

### Unconfirmed markers look unconfirmed

An AI pre-tag was a 1 px dashed grey border — invisible at a glance on a 38 px chip. It is now an
amber dashed ring with a faint amber glow, and the same amber shows on the scrubber tick, in the
marker editor's source line, and as a small `AI` badge next to the label overlay.

Amber rather than red: red is this app's destructive colour (delete, danger), and an AI suggestion
is *unreviewed*, not *wrong* — most of them are right. Put a red ring on every pre-tag and within a
day you stop seeing red. A white fill was the other option; it would have fought the per-label icon
colours, which are picked for a dark chip. The bottom edge keeps its label colour either way, so a
chip still tells you what it thinks it is while telling you nobody has agreed yet.

## 1.3.46

### A test suite

`tests/` — 68 tests, standard library `unittest`, no dependencies:

```
python3 -m unittest discover -s tests
```

Everything runs on temp directories and temp databases; no test touches the real library,
`db/catalog.db` or `webui/cache/`. Coverage is deliberately narrow: every case is something that
actually broke — the age gate stored as metadata, censorship read from stray page text, the ffjav
date taken from another movie's hit, an ambiguous alias merging two performers, `_Duplicates`
walked back in, a short walk replacing a good index, one code in two folders sharing an identity,
a merge dropping the portrait and orphaning cast rows, the cleaner deleting `.scenes.json`
sidecars, an unreadable file making a folder look smaller, and organize applying rows the preview
never showed.

Validated by running the suite against the pre-fix copy of each module: **24 of the 68 fail on the
old code**, and the failures match the bugs they describe.

## 1.3.45

### Toolbar "Scan library" writes the database the UI reads

`scan_library` was the only scraper that opened its own connection from `scan.DB`, the path that
module resolved when it was imported. Every sibling — `scan_code`, `scan_fc2db`,
`backfill_wishlist`, `retag_all` — takes the caller's connection.

In this dev layout both paths are the same file, so nothing was visibly wrong. In the packaged
app they are not: `serve.py` copies the catalog into the writable support folder under
`VAULT_DATA` and serves that, while `scan.DB` still points inside the read-only bundle. A toolbar
scan would show progress, swallow its per-code exceptions, and change nothing the UI would ever
read.

`scan_library(codes, ..., con=None)` now accepts a connection and only closes one it opened
itself; `serve.py` hands over its own. Reproduced with two databases: without a connection the
write landed in the bundle copy and the served copy stayed empty; with it, the write lands where
the UI reads.

## 1.3.44

### No more third availability value

Two inserts wrote `availability='owned'`: the folder-actress capture and the organize modal's
manual row. Nothing else in the app knows that value — disk reconcile and the wishlist query only
handle `available` and `missing` — so a row left at `owned` was neither a library item nor a
wishlist ghost until some later `enrich` happened to repair it.

Both now write `'available'`, which is what they mean (they already set `in_library=1`). Any row
still stuck at `owned` — from an older build or another machine's catalog — is normalized at
startup: on disk becomes `available`, otherwise `missing`, with a log line.

Your catalog had none left; verified against a copy with 5 planted rows, which normalized
correctly in both directions.

## 1.3.43

### ffjav no longer takes the release date from someone else's movie

`fetch_ffjav` searches ffjav.com and reads each field with a page-wide regex. The cover and title
were checked against the code, but the date was simply the **first `YYYY-MM-DD` anywhere on the
page** — and it was returned even when the title had been rejected as unrelated. `scan_code`
writes that date whenever `release_date` is empty, so a wrong date could be stored for a movie
that is not even listed there.

Each hit lives in its own `<div class="card mb-3">`. The scraper now finds the one card
containing this code and reads cover, title and date from that card only; no matching card means
no data. If the site's markup changes, it falls back to the whole page but still requires the code
to appear.

Same inputs, two results with ours second: **old → `2019-01-01` (the other movie's date, with no
title); new → `2024-06-30` with the right title.** With our code absent entirely: old still
returned `2019-01-01`, new returns nothing. Live pages are unchanged (`FC2-PPV-3184182` →
2023-02-13, `FC2-PPV-1668475` → 2021-02-04).

## 1.3.42

### Censorship comes from the site's own tag

With `mosaicStatus` absent from real pages, the tag list is the dependable signal — and it checks
out: of 518 works whose Japanese title says 無修正, **every single one** carries the Uncensored
tag, and none lacks it.

Order of trust: `mosaicStatus` when the page has it → the canonical tags (`Light Mosaic` →
censored, `Uncensored` → uncensored) → otherwise the column is **left as it is**. Deriving the
tags through `_canon_tag` means all twelve raw spellings the translation table knows (無修正, モ無,
藻無し, 薄モ, Uncut …) work, and new ones work automatically.

A value you set in the editor still wins over everything.

**Backfill:** the 13 rows where the tag and the column provably disagreed are corrected — 6 works
tagged `Light Mosaic` were stored as uncensored, and 7 tagged `Uncensored` were stored as censored
(two of them say 【無】 or （無）in their own titles). Tag and column now disagree nowhere. The
2,247 works with no tag either way keep whatever they had, rather than being invented one way or
the other.

## 1.3.41

### Censorship is read from the page data, and your own setting sticks

`parse_fc2ppvdb_video` decided a movie was uncensored if the **word** "Uncensored" — or the token
`"NONE"` — appeared anywhere in the HTML. Both occur in ordinary page chrome, tag lists and
translation strings, so a censored release could be stored as uncensored. `_apply_fc2db` then
overwrote `works.censorship` on every scan, including over a value set by hand in the editor.

- Censorship now comes **only from `mosaicStatus`**: `FULL`/`THIN` → censored, `NONE` →
  uncensored, field absent → the column is left alone.
- A value set in the editor is recorded as `censorship_src='manual'` (new column, added
  automatically at startup) and a scrape will not overwrite it. Without that mark the scrape
  still wins, as before.

Old behaviour, same inputs: a censored page that merely contained the word was already right by
luck, but a page with **no** `mosaicStatus` was stored as *uncensored* — that one is now left
untouched.

## 1.3.40

### Filters coalesce instead of re-rendering on every keystroke

Search, the actress box, the studio box and the duration/age sliders each called `render()` on
every input event. `render()` filters, sorts, clears the grid and rebuilds the first 120 cards —
so a nine-letter search did that nine times, and each wipe threw away every card the chunked
renderer had mounted during a long scroll.

Those five inputs now share a 150 ms debounce. Slider labels still update on the same frame you
drag them; only the grid rebuild waits.

Measured: typing nine characters at 30 ms intervals → **1 render instead of 9**; dragging the
duration slider through five positions → 1 render, with the label tracking every step. Typing
slower than the window still renders per keystroke, so nothing feels delayed.

## 1.3.39

### Studio search filters as you type

`#studioInput` wrote every keystroke into the filter, but the test was `it.producer !== F.studio`
— an exact, case-sensitive match. So the grid went empty the moment you typed the first letter and
only came back if what you typed ended up equal to a producer's full name. The actress box has
always matched partially; the studio box now does the same, case-insensitively and anywhere in the
name.

Typing "K" → 246 matches, "KIN" → 202, "KING P" → 199; "power" (a word from the middle) also
finds those 199, and lower case works.

## 1.3.38

### A failed ffprobe is no longer cached forever

`probe_code` stored whatever it got, including the all-`None` dict a timeout, a missing `ffprobe`
or an unreadable file produces. The next request hit that cache and never retried, so the movie
showed no duration or resolution until the probe cache was cleared by hand. Nothing invalidated an
entry when the file changed either, so a replaced encode kept the old duration.

- **A failed probe is not cached** — the code stays absent and is retried next time.
- **Entries carry the size and mtime of the file they measured** and are re-probed when either
  changes. Entries written before this (no signature) are still honoured, so nothing re-probes
  needlessly.
- **Failed entries already in the cache are dropped on load** — 4 were found here.

Verified end to end on a generated clip: probe → cached with a signature; re-encode the file at a
different size and length → re-probed (4 s / 640x480, not the stale 2 s / 320x240); corrupt the
file → not cached, so it retries.

## 1.3.37

### A web page you visit can no longer drive this server

The server has no auth and binds `0.0.0.0` — a deliberate choice for a LAN tool — but two details
were wider than that. `do_OPTIONS` answered **every** path with `Access-Control-Allow-Origin: *`
and allowed POST, so any site open in your browser could preflight and then call delete, organize,
import or setlibrary. And `/api/open` / `/api/reveal` launch an application on the Mac; the
endpoint list said localhost, the handler never checked.

- **CORS is limited to `/api/stash`**, the one endpoint meant for another origin. Every other
  preflight gets a plain 404 with no CORS headers.
- **A POST whose `Origin` is not this host is refused with 403.** Browsers send `Origin` on
  same-origin POSTs too, so the page itself is unaffected, and tools without an `Origin` (curl,
  scripts) still work.
- **`open` / `reveal` require a loopback or private-network client.** The phone keeps working;
  anything routed in from outside does not.

Verified: preflight on `/api/deletemovie` → 404 with no allow-origin header; cross-origin POST to
`/api/deletemovie` and `/api/setlibrary` → 403; the page's own POSTs and `curl` → unaffected.

## 1.3.36

### A failed history migration no longer deletes the only copy

Favorites, played marks and ratings live on the server, and a browser holding legacy
`localStorage` history uploads it once and then forgets it. That upload was never checked:
`fetch` does not throw on HTTP 500 and the `catch` swallowed network errors, so the code set
`vault_ud_migrated` and removed `vault_fav`, `vault_seen`, `vault_afav` and `vault_rating`
regardless. If the post failed, the local copy was gone and the server never had it.

- The keys are deleted and the flag set **only after the server confirms** (`res.ok` and no
  `{"ok": false}`). Otherwise everything is kept and the migration retries on the next load,
  with a toast saying so.
- **`udPost` reads its response too.** Every later favorite, played mark and rating was
  fire-and-forget, so a rejected write left the UI showing a change the server never recorded.
  A failure now says "Not saved — the server did not record that change".

Verified in the browser with a stubbed 500: keys kept, flag unset, retry pending — where the old
code, run side by side on the same failure, deleted both the favorites and the ratings.

## 1.3.35

### Two jobs can no longer start, and the index is published in one step

- **"Already running" was checked before the flag was set.** Import, the fc2ppv-db scan, the
  wishlist backfill and the metadata scan all read `PROGRESS["running"]` in the request and only
  set it inside the worker they then started, so two quick clicks both passed and two scrapers
  wrote to SQLite through one shared progress dict. The slot is now claimed in the request thread
  under a lock, and released in a `finally` so a worker that dies early cannot wedge it.
  Verified: 8 simultaneous scan requests → **1 started, 7 refused with 409**.
- **`STATE` was published in five separate assignments**, so a request arriving mid-swap could see
  the new grid with the previous path map. It is now one `STATE.update()` under a lock.
- **Overlapping rebuilds raced.** Boot, `/api/rescan`, organize, import and delete can all rebuild
  at once, and whichever finished last won — even if it started first and was reading an older
  filesystem. Each rebuild takes a sequence number and a result older than what is already
  published is discarded. Verified: 6 overlapping rebuilds with 419 concurrent reads — no torn or
  backwards state, and the log shows the stale results being dropped.

## 1.3.34

### /api/data stops redoing work it already did

Three wastes on the hottest endpoint (6 MB of JSON, rebuilt from a stat of every sidecar on the
share):

- **The gzip was recomputed for every request.** The payload is byte-identical between requests,
  so compressing it again cost ~70 ms of CPU each time for the same 1 MB. The compressed bytes are
  now cached next to the raw ones and dropped together.
- **Any POST threw the cache away**, including ones that cannot change what the projection says —
  opening a file in the player, revealing it in Finder, testing the fc2ppv-db connection, reading
  progress, previewing organize or dedupe. Those paths now leave the cache alone; anything that
  really changes state (markers, tags, actress edits, deletes, rescan) still clears it.
- **Two overlapping cache misses both did the full build.** One thread builds now and the rest
  wait for it.

Measured on a 4,107-item library: a warm gzipped request went **71 ms → 2 ms**, and a request
after a no-op POST **121 ms → 0 ms**. Eight concurrent cold requests complete in 0.41 s total
(one build, not eight). The gzipped body decodes byte-for-byte identical to the plain one.

## 1.3.33

### Organize applies what you saw, and only what you saw

The preview shipped at most 4,000 rows for selection, but apply rebuilt the plan and ran every
action whose id was **not** in the skip-list. Rows past the cap were never sent to the browser, so
they were never in the skip-list, so they ran unseen. Apply also walked the library again, so the
plan could differ from the one on screen.

- Apply now takes an **allow-list**: only the rows that were displayed and left ticked run.
- The preview returns a **plan fingerprint** and apply sends it back. If the library changed in
  between, nothing runs and the request is refused with **409** and "run the preview again" —
  the modal reopens with a fresh plan.
- When the plan is larger than the display cap the preview says so: *"Showing the first N of M
  changes — only what is listed here will run; re-open afterwards for the rest."*

`apply_plan(library, skip=...)` still behaves as before for the old call style.

## 1.3.32

### Import can no longer delete the library copy and then fail to replace it

`REPLACE TARGET` deleted the library folder and *then* moved the bigger source in. Any failure in
between — a full disk, an SMB hiccup, a destination that already existed (which `_move` swallowed
silently) — left **nothing**. Reproduced against the old code: with the move failing, the library
folder was gone and only the source remained.

- **Copy, verify, then swap.** The source is staged beside the target as
  `<name>.incoming-<pid>`, its byte count is checked against the source, and only then is the old
  copy deleted and the staged folder renamed into place. The source is removed last. If the staged
  copy is short, or the old copy will not delete, nothing is changed and the row fails loudly.
- **`_move` raises on a destination collision** instead of returning quietly — that silent return
  also marked the row successful, so the catalog claimed a movie was in the library when it was not.
- **An unreadable file makes a folder's size unknown**, not smaller. A short total used to select
  `REPLACE TARGET` and delete the good library copy; such a row is now skipped and reported.
- **A code found in two library folders is skipped** rather than compared against whichever folder
  the index happened to keep last.

## 1.3.31

### The download cleaner can no longer eat sidecars — or the library

`rename_fc2.process_folder` deleted every file that was not a video and every sub-directory,
unconditionally. That is fine on a fresh download full of ads, but `refolder_fc2.py` calls the same
function **and defaulted its root to the live library**, so running it with no argument would have
deleted all 237 `.scenes.json` sidecars — 1,123 hand-placed markers — plus subtitles and posters.

- **Companion files are kept**, mirroring `organize.py`'s KEEP_EXT and adding `.json` for the scene
  sidecars: subtitles, nfo, posters and `.scenes.json` all stay.
- **A sub-directory is only removed after a walk proves** it holds no video and no companion file.
  A real `part2/` folder now survives; an `ads/` folder still goes.
- **`refolder_fc2.py` has no default root.** The path must be given, and pointing it at the
  configured Vault library is refused unless `--i-know-this-is-the-library` is passed.

## 1.3.30

### Orphaned catalog rows cleaned up, and the leak closed

Deleting an actress removed her aliases and her record but **left her links behind**, and a dead
`fc2ppvdb` link is not harmless: the scanner resolves a uuid through that table, so it could hand
back the id of an actress who no longer exists — which is then written onto a movie, leaving a
title pointing at nobody.

- `_delete_actress` now removes `links` and `crawl` rows too.
- The scanner's uuid lookup joins `actresses`, so a stale link can never resolve.
- Cleaned the existing damage: **59 orphaned links, 1 orphaned alias**, and one work
  (`FC2-PPV-4967569`) whose `actress_id` pointed at deleted actress 1464 — now cleared, so it
  shows as unidentified instead of silently blank. `PRAGMA integrity_check: ok`.

## 1.3.29

### The scanner now records co-stars, and will not fold two people together

Three gaps in how a scraped page becomes actress data:

- **The cast table was never written.** Only the lead reached `works.actress_id`; every other
  performer on the page was parsed and thrown away, so no scan could ever add a co-star credit.
  A page naming **several** performers now replaces that code's cast (it is authoritative);
  a page naming **one** only adds, because it cannot tell "she is the only one" from "only the
  lead is listed" — and about 4,100 cast rows here came from elsewhere.
- **`actresses.fc2ppvdb_uuid` was never filled.** The uuid only went into `links`, while the
  sidebar uses the column to tell apart two performers with the same name. It is now back-filled
  on a uuid match and set on a newly created record, and a uuid is matched against both places.
- **An ambiguous alias could merge two people.** Stage names like りん, ほのか or ゆき are held by
  several performers; matching on one folded a new person into whoever had it, and the page's
  aliases were then copied onto that row, making it permanent. An alias now only matches when it
  belongs to exactly one actress, and the page's aliases are grafted on only when identity is
  certain (a uuid match or a record we just created).

Note what was NOT the problem: the matcher does find an existing performer by name — verified
against the live catalog. The 126 `source='costar'` stub records came from a one-off script that
is no longer in the tree, not from the scanner.

## 1.3.28

### Merging two actress records no longer loses the portrait or the co-star credits

`dedupe.merge_group` copied text columns onto the target and then deleted the source row. Three
things fell through:

- **`image_blob` was never copied**, so folding in the record that happened to hold the portrait
  threw the photo away. (It also could not have worked as written: the column list used
  `NULLIF(col,'')`, which is a text test, not a blob one.)
- **`work_actresses` was never repointed.** Those rows kept the deleted id, and since the schema
  has no enforced foreign keys the delete succeeded quietly — the co-star credit then resolved to
  nothing and vanished from the UI.
- **`name_en`, `fc2ppvdb_uuid`, `archive_id`, `censorship` and `source` were not carried over**,
  though the sidebar uses the uuid to tell apart two people with the same name.

All of these now transfer, empty-target-only so nothing is overwritten. The cast table is moved
with `UPDATE OR IGNORE` and the leftovers deleted, so a work both records appear on keeps exactly
one row — promoted to `main` if either record was the lead. Columns are filled **after** the source
row is deleted, because `archive_id` is UNIQUE and copying it while both rows existed would raise.

Verified against a copy of the live catalog: merging record 371 into 18 previously lost a
13,911-byte portrait and left 1 orphaned cast row; it now keeps the portrait and moves the credit,
with no orphans.

## 1.3.27

### One code is no longer assumed to be one file

When the same code existed in two folders, both cards stayed in the library but everything that
touched a file looked the code up once: playback followed the folder the walk saw first, the side
panel followed the last one, the catalog overlay reached only the first (so the other card showed
tags but no title or actress), and deleting either card trashed **both** copies.

Every item now carries a **uid**, a stable id for one folder on disk. It is simply the code when a
code has one folder — which is the whole library today — so URLs, bookmarks and the thumbnail cache
are unchanged. Only duplicated codes get distinct ids (`CODE~hash`).

- Streaming, thumbnails, open/reveal in Finder, markers and delete all resolve through one helper
  that takes a uid or a bare code.
- The works overlay now reaches **every** copy of a code, not just the first.
- Deleting one copy trashes that folder only, and keeps the catalog row while another copy is still
  on disk. Deleting the last copy purges the row as before.
- The browser keeps a `BYUID` map beside `BYCODE`, and cards carry both ids: files follow the uid,
  while per-code data (favorites, played, ratings, tags, actress) stays keyed by code.
- A uid carries its `~suffix` only while the code is duplicated, so a stale link falls back to the
  bare code instead of 404ing.

## 1.3.26

### A bad walk can no longer erase the library

`os.walk` silently skips directories it cannot read, so an unmounted share, a permission error or
a half-finished walk returned a SHORT list — and that list was written straight to
`cache/index.json` and swapped into the live state. Movies disappeared from the grid until some
later walk happened to succeed, and because availability updates are one-way they did not even
come back as wishlist ghosts.

The walk now reports what went wrong, and a result is only allowed to replace the index when it is
believable. A scan is **refused** when:

- the library folder is not there at all (share unmounted),
- any directory failed to read, or
- it found fewer than half the items we already knew about (and we knew about at least 20).

A refused scan keeps the last good index, writes nothing to the cache, logs
`[scan] REFUSED to replace the index - <reason>`, and the page shows the reason as a toast instead
of quietly losing movies. A genuine deletion still goes through.

## 1.3.25

### Five fixes from the code review

- **The player no longer leaks per open.** `setupScenes` added a `ResizeObserver` on the seek bar
  and a document `fullscreenchange` listener, and `closePlayer` removed neither — every opened
  movie stayed reachable (Safari also kept the decoder). Both now live on one session object and
  are torn down on close.
- **`_Duplicates` is skipped again.** The walk compared raw directory names against a lowercase
  set, so the capital-D folder organize creates was scanned. A quarantined copy could become the
  one that plays. Now compared case-insensitively.
- **`do_POST` answers with JSON 500.** Only `json.loads` was wrapped; a locked database or a bug
  dropped the socket. It now matches `do_GET`.
- **Cookie mode sends `age-verified` and aborts on the 18+ page.** Without it every page parsed to
  `{}` and counted as a normal miss, so a scan walked the whole library recording nothing.
  A challenge or age gate now returns an error and stops the loop, as the solver path already did.
- **The NUL byte in index.html is gone** (`a.code||""`), so grep and ripgrep can read the file
  again instead of calling it binary.

## 1.3.24

### A reload no longer loses sight of a running scan

The scan runs in its own thread on the server, so reloading the page (or closing the browser, or
switching device) never interrupted it — but nothing re-attached to it afterwards, so the counter
vanished and the button looked idle while work continued. The page now asks `/api/scanprogress` on
load and resumes showing progress if a scan is running.

## 1.3.23

### The tagging filters say what they mean

- **To review** now means what you would assume: *any* AI marker still outstanding, including the
  one or two suggestions left behind in a movie you otherwise finished. Previously it only caught
  parts you had never opened, so leftovers hid under "AI pre-tagged".
- **AI pre-tagged** is renamed **Not started** — a release where some part has AI markers and none
  of yours, i.e. nobody has opened it yet.

## 1.3.22

### FlareSolverr path tuned

- **The session is created once, not per page.** `sessions.create` ran before every fetch; it is
  now remembered and only re-issued after a failure cleared it.
- **No courtesy sleep in resolver mode.** The 0.4 s pause between codes was sized for the cookie
  transport. Through FlareSolverr each page already costs ~2 s of real browser time, so the pause
  only slowed scans by ~16%. Cookie mode keeps it.
- **The Chromium is handed back when a scan ends** (`solver_release`), instead of being parked on
  the resolver indefinitely.
- **Settings no longer hangs on a sleeping resolver.** The reachability probe (up to 4 s) is cached
  for 30 s instead of running on every Settings open.
- **The resolver URL must be on your own network.** Vault has no auth and listens on the LAN, so a
  public URL would have let any visitor use the server as a relay. localhost and private ranges
  only.

Measured after the change: ~2 s per page, 10 s for 4 pages, session gone afterwards.

## 1.3.22

### FlareSolverr as an alternative to the pasted session

Settings → Sources → fc2ppv-db now has two ways through the human-check:

- **Browser session** — the existing cookie and user-agent paste, stored in the Keychain.
- **FlareSolverr** — paste the resolver's URL (`http://host:8191`, the `/v1` suffix is optional). Vault sends each video page to that browser and parses the HTML it returns. The clearance stays on the resolver, so it has to run on a machine that leaves through the same public IP as Vault.

Test connection and Scan missing / Scan all follow whichever option is saved. Three failed page loads in a row stop the scan and say why. An 18+ confirmation stops it on the first page, so that gate is never stored as a title. Saving one option does not erase the other.

The site's 18+ button only sets a cookie named `age-verified`. Vault sends that cookie with each FlareSolverr request, so the confirmation happens in the same browser that cleared Cloudflare.

## 1.3.21

### Co-star matches say so, and a/s/d control the player

- **Actress filter:** a card whose match came from a **co-star** now carries a "with &lt;name&gt;"
  badge under the lead's name. Filtering by るりか and getting a card that says "Ai Takanashi" was
  correct — she is in the cast — but it read like a bug.
- **Player keys:** `a` / `s` / `d` = back / pause / forward, replacing `j` / `k` / `l`. Space,
  arrows, `m`, `f`, `t`, `0–9` and `<`/`>` are unchanged.

## 1.3.20

### "To review" now counts parts, not releases

A multi-part release counted as reviewed as soon as **any** part had your markers, so a 6-parter with
four untouched parts vanished from the list. The filter now flags a release when **any single part**
still has AI markers and none of yours: 11 releases became **32**, surfacing 29 untouched parts that
were hidden — mostly later parts of Sora Mikumo releases.

"AI pre-tagged" and "Corrected manually" keep their release-level meaning.

## 1.3.19

### Pictogram icons for the positions

`missionary`, `doggy`, `cowgirl`, `blowjob` and `standing` now use the set2 pictograms from
`webui/icons/`, served by the new `/icons/<label>.png` route (basename-only, so nothing can escape
that folder). They are drawn as a CSS mask rather than an `<img>`, so they take the label's colour
and stay readable in both light and dark themes.

Labels without a PNG keep their built-in SVG. To add one, drop `<label>.png` into `webui/icons/`
and add the label to the list next to `markerIcon()` in index.html.

## 1.3.18

### "Add marker" always means the playhead

All three right-click menus now add the new marker at the **current frame**, and say so:
*＋ Add marker at playhead (12:34)*. Before, only the menu on the video itself used the playhead —
right-clicking a marker chip used that chip's time, and right-clicking the seek bar used the spot
you clicked, which made the placement depend on where the cursor happened to be.

The editor keeps its **⤓ Playhead** button for moving an existing marker to the current frame.

## 1.3.17

### 11 labels: cowgirls folded, three more dropped, cumshot re-used as creampie

- **reverse-cowgirl folded into cowgirl** — 31 markers relabelled, nothing lost. The distinction was
  too fine to train reliably.
- **spooning, sideways and 69 dropped** — 59 markers removed (17 hand-made, 42 AI). Spooning and
  sideways were 3 and 6 examples, far too few, and near-identical to each other.
- **A detected `cumshot` now becomes a `creampie` marker.** The detector is good at finding the
  climax moment even when it names it wrong, and in this library that moment almost always is a
  creampie — so it is no longer suppressed, just relabelled. A `facial-cumshot` or cum-in-mouth is
  not a creampie and stays dropped.

Label set: **5 acts** (fingering, blowjob, titjob, toy, creampie) + **6 positions** (missionary,
doggy, cowgirl, standing, the-snake, scoop-up). Sidecars backed up to
`~/vault-tagger-runs/sidecar-backup-20260925-151409.tar.gz`.

## 1.3.16

### Four labels dropped: anal, cumshot, cunnilingus, handjob

The label set is now **10 positions + 5 acts = 15 labels**. The four were removed everywhere:

- gone from the editor and from the server's accepted labels;
- **690 existing markers deleted** from the sidecars (599 AI suggestions, 91 hand-made) and 165 rows
  from the marker cache. Every sidecar was backed up first to
  `~/vault-tagger-runs/sidecar-backup-20260925-123531.tar.gz`;
- the tagger maps them (and their aliases) to nothing, and P-HAR suppresses all four, so no future
  run proposes them. `creampie` is now the only climax label.

They scored worst in review by a wide margin — cumshot 12%, handjob 18%, cunnilingus 27%, anal 0% —
so this removes roughly 40% of the review queue.

## 1.3.15

### Trash moves are verified and logged

Moving a movie to the trash now counts the files before and after the move. If anything fails to
arrive, the delete aborts with an error instead of reporting success. Every move is appended to
`cache/trash.log` with the source, the destination and both file counts, so a deletion can always
be traced afterwards.

## 1.3.14

### Two more positions: the-snake and scoop-up

**the-snake** (lying flat on the stomach, entered from behind) and **scoop-up** join the label list,
which is now 10 acts + 9 positions = **19 labels**. Both are classes P-HAR already detects, so the
tagger can pre-tag them — until now it threw those detections away.

## 1.3.13

### Unreadable videos fail cleanly instead of hanging the player

The video endpoint sent its `206` headers before opening the file. When the open failed — seen
when macOS revoked a long-running server's access to the network volume (`Operation not
permitted`) — the error was written into a response that had already started, and the player just
spun. The file is now opened first; a failure returns **503 "cannot read video: …"** right away.
If every movie suddenly shows that, restart the server.

## 1.3.12

### "To review" filter (training mode)

The **Scene tagging** group gained a **To review** filter at the top: movies that carry AI
suggestions but not a single marker of your own — the ones you have not touched yet.

"AI pre-tagged" still lists every movie with analyzer markers left, including ones you are halfway
through, so the two together tell you what is untouched versus in progress.

## 1.3.11

### Sidebar filters for tagging progress (training mode)

A **Scene tagging** group appears in the sidebar while training mode is on, with counts:

- **AI pre-tagged** — movies that still carry analyzer markers waiting for review.
- **Corrected manually** — movies where you already made or confirmed markers.
- **No markers yet** — untouched movies, for picking the next one to work on.

The flags are read from the sidecars themselves (cached by file timestamp), so they are accurate
even for movies that were never opened in Vault.

## 1.3.10

### Fewer labels, and the seek bar no longer collapses

- **Dropped from the label list:** kissing, foreplay, deepthroat, masturbation, facial (facial is
  covered by cumshot). With "sex" gone earlier, the list is now **10 acts + 7 positions = 17 labels**.
  Existing markers with a dropped label still display.
- **Narrow player windows:** when the scrubber got squeezed below ~170 px (e.g. a 1024 px window
  without theater mode) it collapsed to 0 px and seeking was impossible. It now moves to its own
  full-width row, the same as on phones, and returns to one row when there's space again.

## 1.3.9

### Scrub while dragging a marker; no more "sex" label

- **Dragging a marker chip now scrubs the video with it** — the player pauses and follows the marker
  frame by frame, so you can drop it exactly where the action starts.
- **"sex" is gone from the label list.** It contradicted the positions (every position *is* sex) and
  would have needed two markers for one moment. Use the position labels instead. Existing markers
  that still say "sex" keep working; only the editor no longer offers it.

## 1.3.8

### Smaller marker vocabulary

The built-in label list is now the **16 acts and 7 positions** only — the state labels (clothed,
undressing, topless, nude) and structure labels (scene, intro, outro, talk) are gone from the editor
and the server's list. Existing markers that use them still show on the timeline; custom labels are
unaffected.

## 1.3.7

### Double-click to skip, scene-to-scene buttons

- **Double-click / double-tap** the video, YouTube-style: **left half −10 s**, **right half +30 s**.
  More quick taps on the same side add up (−20, −30… / +60, +90…), with a "« 10 s" / "30 s »" bubble.
  A single click still plays/pauses (a single tap on a phone still reveals the controls); it now waits
  250 ms to rule out a double click. Double-tap zoom on the video is disabled on iOS.
- **⏮ / ⏭** jump from scene marker to scene marker: ⏮ to the start of the current scene (or the
  previous one when you're right at a start, like chapters), ⏭ to the next one. On a movie without
  markers they skip −10 s / +30 s instead of doing nothing.

## 1.3.6

### Flat player icons

The player's control buttons (play/pause, previous/next scene, volume/mute, fullscreen, theater,
close, ＋) are flat single-colour SVG icons again instead of emoji, which iOS drew as colourful
pictures. The volume icon now also follows the **m** key and hardware mute.

### Player fixes for phones / iPhone

The custom player from 1.3.2 was hard to use on a phone:

- **Seek bar was 0 px wide** — ten controls shared one row. On narrow screens the scrubber now gets
  its own full-width row, and the theater / ✕ buttons are hidden (✕ is the overlay button).
- **A tap paused the video** just to reveal the controls. On touch, the first tap now only shows the
  controls; tap again to play/pause. (Pointer events are used so iOS's synthetic mouse moves don't count.)
- **Fullscreen did nothing on iPhone** (no element fullscreen in iPhone Safari) — it now falls back to
  the video's native fullscreen.
- **Blocked autoplay** showed a stale ⏸ — the button now shows ▶ and the controls stay visible.
- **Empty Info tab** on phones once theater mode had been used — theater mode no longer hides the
  phone's Info / Related tabs.

## 1.3.5

### Confirm AI markers (training mode)

- **✓ Confirm** an AI suggestion that's correct: in its right-click menu or in the marker editor.
  **✓ Confirm all AI markers (N)** in the seek-bar / video right-click menu does the whole video part.
- A confirmed marker becomes `manual` at the same time and label, and keeps the AI original under
  `was` — so it counts as a verified training label, and untouched AI markers stay clearly
  "not reviewed". The chip turns from dashed to solid.

## 1.3.4

### Right-click menu and custom labels for scene markers (training mode)

- **Right-click** a marker chip, the seek bar or the video for a context menu: **＋ Add marker** at
  that exact spot (bar) or at the playhead (video), and **✎ Edit** / **🗑 Delete** for the marker
  under the cursor.
- **Custom labels:** pick **＋ New label…** in the marker editor's label list, or add/remove them in
  **Settings → Library** under training mode. They're stored as `custom_labels` in `config.json`,
  lowercase words joined by hyphens (max 32 chars), accepted by the server like the built-in list,
  and shown in amber on the timeline. Removing one only takes it off the list.

## 1.3.3

### Edit scene markers in training mode

- New **Settings → Library → Scene markers — training mode**, **off by default** (and off in the
  shipped `config.example.json`). While it's off, markers are view‑only and the server refuses edits.
- With it on, in the player: **＋** adds a marker at the playhead, **drag** a chip to move it, and
  **right‑click** a marker to pick its label from the fixed list or delete it (−1s / +1s / "playhead"
  to fine‑tune). Free‑text labels are gone — only the SCENES_SPEC vocabulary is accepted.
- Markers are **start points only**: the seek bar shows a tick per scene and the "▶ now" badge names
  the last scene that started. AI suggestions have a dashed chip outline.
- Every edit is saved as a `manual` marker. For training, a changed AI marker keeps its original
  under `was`, and a deleted AI marker is recorded in the sidecar's `rejected` list — the analyzer
  won't suggest either again.
- Multi‑part movies now show and edit the markers of the part that's playing (previously always part 1).

## 1.3.2

### Scene markers moved onto the player timeline

Scenes are no longer a side list — they're on the **video's own seek bar**, tube-site style. The
native controls are replaced by a custom control bar with:

- **Clickable icon chips above the bar** at each scene (a placeholder icon for now — action-type
  icons come later), packed into rows so they don't overlap; click one to jump exactly there (on a
  cramped/narrow layout the chips step aside for the buttons below). Hovering a chip shows its label.
- **⏮ / ⏭ previous / next scene** buttons — a persistent way to step through scenes without having
  to hit a tiny target.
- Colored **segments on the scrubber** (positions purple, acts pink, states blue, cuts grey), a
  hover **label + time** tooltip, and a "▶ now" badge naming the current scene.
- **＋** adds a marker at the playhead (type a label, Enter); **right-click** a marker deletes it.

Play/pause, mute and fullscreen live in the same bar; every keyboard shortcut still works.

Each chip carries a **symbolic icon** (a clean, original glyph set — lips for oral, a droplet for
cumshot, `»` for doggy, and so on; unknown types get a neutral dot). Icons inherit the category
color and can be refined per action type later.

### Theater mode

A **▭ theater toggle** (also the **`t`** key) hides the filmography rail and info panel so the
video — and the scene bar with it — go **full-width**, which is where the chips have room to spread
out. It's remembered per browser. A close (**✕**) button now lives in the control bar too, since
the panel that used to hold it is hidden in theater.

## 1.3.1

### Scene sidecars: play nicely with vault-scene-tagger

- Adding or deleting a manual marker now **keeps the sidecar's other top-level fields and its
  `generator`** instead of rewriting the file from scratch. The analyzer stores which phases it
  already ran there, so a manual edit no longer makes it re-decode the whole movie on the next run.
- Sidecar write-back uses a hidden temp file (`.<name>.scenes.json.tmp`) and UTF-8 without escaping, as the spec requires.
- The contract now ships as [`SCENES_SPEC.md`](SCENES_SPEC.md) (identical to the analyzer repo's copy).

## 1.3

### Scene markers (timeline + jump-to-scene)

The player now has a **Scenes** panel: a color-coded marker bar under the metadata, a
click-to-seek jump-list (label · confidence · timecode), and a live "▶ now playing" label that
tracks the current scene. Click the bar or a list entry to jump; add your own marker at the
playhead from a label vocabulary, or delete one.

Markers come from **`<video>.scenes.json` sidecar files** (spec `vault-scenes/1`) written next to
each video by an external analyzer — so the heavy, optional scene-detection/AI work lives in a
**separate project** and this server just reads and displays the result. The sidecar is the source
of truth; Vault keeps a queryable `markers` cache in the catalog and re-syncs a movie's markers
whenever its sidecar changes. Your **manual** markers are written back into the same sidecar and
are never overwritten when the analyzer re-runs (it owns only its own `cut`/`ml` markers).

## 1.2

### Delete movies and actresses

You can now remove things from your library, always behind a confirmation dialog:

- **Delete a movie** — from the player (**🗑 Delete movie**) or by selecting several and using
  **🗑 Delete** in the multi-select bar. The video file(s) are **moved to a recoverable trash**
  and the catalog rows removed. A wishlist entry (no file) just leaves the catalog.
- **Delete an actress** — the **🗑** in her overview removes her profile and trashes the movies
  where she is the **only** performer; titles she merely **co-stars** in are **kept**, with her
  removed from the cast and a co-star promoted to lead.

Nothing is hard-deleted. Files on the system disk go to the macOS **Trash**; files on a network
or external volume (where macOS can't use the Trash) are moved to a **`_vault_trash/`** folder at
the library root — same volume, so it is instant even for large files, and fully recoverable. The
library scan skips that folder, so trashed movies never re-appear.

## 1.1

### Your history is saved on the server now

Played state, favorites and ratings used to live only in the browser's `localStorage`, so a
cache wipe or a different browser lost them. They now live in a **`userdata.json` sidecar next
to the catalog** — the single source of truth — kept deliberately **separate from `catalog.db`**
so they also survive a catalog rebuild or reimport. The browser keeps no history of its own; it
just mirrors the server. Anything already saved in a browser is **migrated up automatically** the
first time it connects, then those local keys are cleared. The sidecar is per‑user state and is
never part of a release build.

### Favorite actresses

You can now favorite a **performer**, not just a movie — the **♥** sits on her group header in
By‑actress view and in her overview. A new **♥ Favorite actresses** sidebar filter narrows the
wall to every title she leads *or* co‑stars in. The old **Favorites** row is now **Favorite
movies** for clarity.

### Uncensored‑only filter

The **Censored only** sidebar toggle is now **Uncensored only** — the more useful direction for
an overwhelmingly uncensored library.

### Cleaner poster overlays

The controls and badges on each poster were reorganised so the two never fight for the same
corner:

- **Top‑left**, left→right: a **select** box (for multi‑select — visible on every card once a
  selection is active, so you no longer need ⌘/shift to start one), the **✓ played** toggle, and
  the **♥ favorite** toggle.
- **Bottom**: the informational badges — multi‑part (`2×`), resolution, duration, and a red
  **CENSORED** flag — in that order. Resolution now shows for every file (SD included), not only HD.

## 1.0

First public release.

### Works on phones

The UI is now fully responsive — **one page, no separate mobile site and no user‑agent
sniffing**. Everything keys off window width and pointer capability, so a narrow desktop
window gets the compact layout too, and an iPad that reports itself as a Mac still gets the
right one.

- Two‑column poster wall on a phone; filters move into a **☰ Filters** slide‑over drawer that
  closes once you pick one.
- The card ♥ / ✓ controls are always visible on touch devices (they were hover‑only, so on a
  phone they could never be reached), the full‑thumb play scrim is hidden, and the resolution
  and duration badges moved to the bottom of the poster — the ♥ button sat exactly on top of
  them.
- The player restacks: video pinned on top with an overlay close button, and the two side rails
  become **Info** / **Parts · Related** tabs beneath it. The list tab is named after its
  contents (`Parts 3` vs `Related 41`).
- Heights use `100dvh`, so the iOS URL bar no longer clips the player.

### The grid no longer kills mobile Safari

`render()` built a DOM node for every item up front — roughly **80,000 nodes** for a
4,000‑movie library, plus an `IntersectionObserver` per thumbnail. iOS kills a tab that
crosses its per‑tab memory ceiling, so large libraries simply crashed the browser.

The grid now renders in batches of 120 driven by a sentinel at the end of the list: **~6,300
nodes at first paint instead of ~80,000**. Grid, list and by‑actress views all chunk; a
collapsed actress group builds no cards until it is expanded; and the thumbnail observer is
created once and only observes new cards instead of re‑registering every thumbnail on each
render. Desktop benefits too — the grid used to rebuild all 4,000 cards on every filter
keystroke.

Scrolling to the bottom of a very large *unfiltered* library still accumulates cards. Filter
or search first if you have thousands of titles.

### Availability actually tracks reality

A movie you added could keep showing Status **missing** forever. Two causes, both fixed:

- Every catalog write in `serve.py` was `INSERT OR IGNORE`, which is a no‑op against an
  existing wishlist row.
- `migrate_fc2.py` never touched the catalog at all.

Now any scan or rescan reconciles `works.availability` against what is actually on disk —
logged as `[db] N work(s) marked available` — and the import pipeline marks rows as it moves
files in. Reconciliation is deliberately **one‑way** (`missing` → `available`): the reverse
would let a failed scan, such as an unmounted network share, silently flag the whole library
as missing.

### Access from other devices

`serve.py` now binds `0.0.0.0` instead of `127.0.0.1`, so Vault is reachable from your phone
or another computer on the same network, or over a VPN back to it.

> **This is a real change in exposure.** There is still **no login and no HTTPS**, and
> `/api/open` and `/api/reveal` act on the host machine's desktop. Keep Vault on networks you
> trust and do not port‑forward it. To restore local‑only behaviour, set the bind back to
> `127.0.0.1` in `serve.py`. See **Privacy & safety** in the README.

### Safer release builds

`make_release.sh --rc` produces a shareable build that keeps the full metadata catalog while
removing every signal about what you own or want. Beyond the existing `in_library` /
`availability` reset it now also clears `works.source='fs'` (which by itself re‑identified the
entire owned library), normalises hand‑curated `manual` / `user` / `csv` rows, drops works rows
that carry no metadata at all, empties the `crawl` bookkeeping table, and `VACUUM`s so deleted
rows can't be recovered from the freelist.

The build then **verifies itself and fails rather than shipping a leak**, re‑checking every one
of those plus personal paths, hostnames, `.DS_Store`, `config.json` and `cache/`.
