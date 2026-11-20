# Where Vault's metadata comes from

Your **folders are the spine**: every movie folder becomes a card, whatever the web knows about
it. The catalog (`catalog.db`) is an overlay on top, filled from the sources below. Each source
fills what it is best at, and nothing you set by hand is overwritten.

## On your computer

| What | From |
|---|---|
| Duration, resolution, codec | `ffprobe` on the file itself — no web source beats it |
| Poster of a movie you own | `ffmpeg`, a frame from the movie |
| Scene markers | the scene model, run by Vault (Settings → Scene model) |
| English subtitles | Whisper + a local translator (Settings → Subtitles) |

## Scan metadata (Tools → Scan library, or ⟳ Scan metadata on one movie)

The sources are asked **in this order**, and each only fills what the ones before it left empty:

1. **fc2ppv‑db** — the primary source: the **cast**, Japanese title, release date, cover, tags,
   and full filmographies. It sits behind a Cloudflare check, so it needs a browser session
   pasted into Settings → Sources, or a FlareSolverr on the same public IP (see the README).
   Without either, scans simply skip it.
2. **fc2db.net** — fills what fc2ppv‑db left, often with the official cover and sale date, and
   often names a performer fc2ppv‑db lists as nobody.
3. **123av** (the English title — the only source that has one) and **ffjav** (Japanese title,
   cover, date) — anything still empty.

**Tags** from all of them are added together and routed through the tag table (Settings → Tags):
Japanese tags become their English label, merges stay merged, untranslated one‑offs are dropped.

**What a scan never overwrites:** censorship you set with 🔓/🔒, an actress you assigned by hand
(edit, reassign or bulk‑assign), and any field an earlier scan already filled — except the
player's ⟳ Scan metadata, which re‑reads one movie on purpose.

**The nightly run** (Settings → Nightly) scans only **newly added** movies, and asks again about a
movie the sources had nothing for after a week at the earliest, so the sites don't see the same
misses every night.

## Enrich from web (one actress)

**🌐 Enrich from web** in an actress's ✎ panel looks her up on **javdatabase** (age, height, cup,
measurements, Japanese name) and **jav.guru** (her FC2 filmography), trying her known names in
both word orders. Two different performers can share a romaji name, so Vault **verifies** first:
does your library contain any title from the filmography it found? Only then is the profile
applied (unverified results are shown, and applying them needs an explicit confirm). On apply,
codes you own are attributed to her and the rest of her filmography becomes **wishlist**
entries. Body fields only fill blanks.

## Names in folder names

A folder named `<CODE> - Name` links the movie to that actress **only if the name already exists**
in the catalog — spellings in folder names are too unreliable to create new records. Otherwise the
name is dropped when 🧹 Organize normalises the folder, and the actress comes from a scan instead.

## One person, one record

Typing a name matches an existing actress by English, Japanese or kana name, an alias, or the
**same name in the other word order** ("Sora Mikumo" ↔ "Mikumo Sora"), so editing never spawns a
duplicate. **Settings → Actresses → Find duplicates** merges records that share an identical
Japanese name or are a word‑order flip — works, aliases and body info go to the survivor; no movie
is ever deleted. Alias‑only matches are listed for you to decide.
