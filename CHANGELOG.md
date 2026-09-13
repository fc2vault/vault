# Changelog

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
