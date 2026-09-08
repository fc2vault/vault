# Vault — User Guide

A feature‑by‑feature walkthrough. For install & setup see the main **[README](../README.md)**.

## Contents

1. [The main window](#1-the-main-window)
2. [Filtering & sorting](#2-filtering--sorting)
3. [The wishlist](#3-the-wishlist)
4. [Playing a movie](#4-playing-a-movie)
5. [Editing a movie](#5-editing-a-movie)
6. [Multi‑select: bulk‑assign an actress](#6-multi-select-bulk-assign-an-actress)
7. [Organize the library folder](#7-organize-the-library-folder)
8. [Scan metadata from the web](#8-scan-metadata-from-the-web)
9. [Enrich an actress (verified)](#9-enrich-an-actress-verified)
10. [Settings](#10-settings)
    - [Library & cache](#library--cache)
    - [Tags & translations](#tags--translations)
    - [Actresses & de‑duplication](#actresses--de-duplication)
    - [Sources & fc2ppv‑db](#sources--fc2ppv-db)
11. [Using Vault on your phone](#11-using-vault-on-your-phone)

---

## 1. The main window


- **Top bar:** search, the **Grid / By actress / List** view switcher, a sort dropdown, a shuffle die, **Reset** filters, **Scan library**, **Organize**, and **Settings**. The right side shows how many titles match and the total library size.
- **Left sidebar:** all your filters (see below).
- **Main area:** poster cards. Each shows the actress (or code if unidentified), the FC2 code + Japanese name, tags, and pills for size · age · cup. Badges mark resolution, duration, and multi‑part (`2×`).
- Hover a card for the ♥ favorite and ✓ played toggles.

Switch to **By actress** to group the wall under each performer (with her portrait and stats), or **List** for a dense, sortable table.

## 2. Filtering & sorting

Everything in the left sidebar narrows the view live:

- **Library:** all / identified / amateur‑unknown / favorites
- **Watched state, HD only, censored only**
- **Actress** and **Studio** type‑ahead boxes (actress matches aliases and Japanese names too)
- **Resolution, Duration, Cup size, Age** ranges

The **sort dropdown** offers Newest added, Release date, Size, Duration, Rating, Favorites, Actress, Code, and Shuffle. “Newest added” uses a **true first‑seen** timestamp, so a folder you just dropped in sits at the top even if its file dates are old.

**Search** (top bar or press `/`) matches actress, aliases, Japanese name, code, title, and tags — and when you search a name, her titles are floated to the top ahead of incidental keyword hits.

## 3. The wishlist

Tick **Show wishlist** in the sidebar to include titles you *don’t* own — “ghost” cards drawn from an actress’s known filmography. They’re only shown for performers you already own at least one title of, so the wishlist stays relevant. Ghosts show a cover (where known) and a `missing` badge; the rest of the app treats them as first‑class (they appear in her filmography rail, etc.).

In **By‑actress** view, click an actress’s portrait or name to open her **overview** — bio and measurements, everything you own, and everything missing. Each missing title has a **Search on sukebei** action that opens a search for its code in a background tab.


## 4. Playing a movie

Click any owned card to open the detail/player view.


- **Left rail:** her **Filmography** — every title (owned + `◇` wishlist), with the current one highlighted.
- **Center:** the video (range‑streamed for fast seeking; multi‑part titles auto‑advance).
- **Right panel:** actress name in a fixed order — **English → Japanese → code** — then her stats as pills (age · cup · height · B·W·H), technical **metadata**, favorite / played / **censored toggle**, external‑player & Finder buttons, **Scan metadata**, your star rating, and editable **tags**.

**Keyboard:** `space`/`k` play‑pause · `←`/`→` (or `j`/`l`) seek ±10s (hold `shift` for ±60s) · `↑`/`↓` volume · `m` mute · `f` fullscreen · `0–9` jump to % · `<`/`>` speed.

**On a phone** the three columns can't sit side by side, so the player restacks: the video pins to the top with a **×** over it, and the two side panels become tabs underneath —

- **Info** — everything from the right panel above.
- **Parts N** / **Related N** — the left rail. It's named after what it holds: the part list for a multi‑part title, otherwise her filmography. Opening an entry reloads the player and returns you to **Info**.

## 5. Editing a movie

Click the **✎** in the detail panel to edit. You can set the movie’s English & Japanese title and the **actress** (English + Japanese), **age, cup, height, bust/waist/hip**. The actress field autocompletes against your catalog — and matches by Japanese name **or reversed word order**, so “Sora Mikumo” resolves to an existing “Mikumo Sora” instead of creating a duplicate. Picking a known actress instantly fills her stored numbers so you can see what will be applied.

> **Only the actress identity is written when you *switch* a movie to an existing performer** — her body numbers are pulled from her record, never overwritten with the form’s stale values. Edit her profile only when you’re editing that same actress.

The **censorship** state is a one‑click toggle (🔓 Uncensored / 🔒 Censored) right in the detail panel — no edit mode needed.

## 6. Multi‑select: bulk‑assign an actress

In Grid or List view, **⌘‑click** (or Ctrl‑click) to toggle individual cards, or **shift‑click** to select a range. Selected cards get a pink outline and a ✓ badge, and a bar appears at the bottom:


Type an actress name (autocompletes) and hit **Assign actress** to attach her to every selected movie at once. Bulk edit touches **only the actress** — never per‑movie fields — and uses the same smart matcher, so it won’t spawn duplicates.

## 7. Import from your download folder

Point Vault at a **download folder** (Settings → Library), then hit **📥 Import** (top bar). It previews the whole pipeline first, then: renames & cleans the downloads (strips source prefixes, removes junk/spam clips), and moves each release into the library — **adding** new ones and **replacing** older copies with better versions.


- Library deletes (replacing an older copy) are gated behind a **confirmation checkbox** — leave it unticked to only add + clean. Nothing in the library is removed without it.
- The move runs in the background (large cross‑drive copies can take a while).

**Organize** (Settings → Library → *Organize library…*) tidies files already in the library — junk to `_vault_trash/`, duplicates to `_Duplicates/` (largest kept), names normalized to `…/<CODE>/<CODE>.ext`. It previews first and never deletes or overwrites. Rarely needed now that Import handles new files.


## 8. Scan metadata from the web

**Scan library** fetches missing English/Japanese titles, covers, dates, and tags from the shell sources (123av, ffjav).


- It re‑indexes the filesystem first (so newly added movies are found), then shows a checklist.
- Toggle **Missing titles** vs **All movies**; untick any code to skip it.
- **Scan N selected** runs in the background — progress shows on the toolbar button. You can “Run in background” and keep browsing.

Scanning is **fill‑if‑empty**: it never overwrites titles/tags you set yourself.

## 9. Enrich an actress (verified)

Open a movie by an identified actress → **✎** → **🌐 Enrich from web**. Vault scrapes javdatabase (body info, JP name) and jav.guru (her FC2 filmography).

The key idea is **verification**: it checks whether any code in her online filmography is one **you own**. If yes, it’s her — the profile is applied and her **wishlist** is filled from her filmography. If not, it warns that this might be a different person with the same name and asks before applying. Physical fields are fill‑if‑empty; English + Japanese names are stored as aliases.

## 10. Settings

Open **Settings** from the top bar. Four tabs:

### Library & cache


- **Display:** switch measurements between **Metric (cm)** and **Imperial (in)**.
- **Library source folder:** change where Vault scans (triggers a full re‑scan).
- **Cache:** see and clear the scan index, video probes, and thumbnails (all safe to clear — they regenerate). **Rebuild index now** re‑reads the catalog.

### Tags & translations


- **In use:** every tag in your library with its count. **Rename** a tag (renaming to an existing one **merges** them — e.g. fix a typo), or remove it.
- **Translations:** a JP→EN dictionary (seeded with common FC2 tags). Set the **English label** a raw/Japanese tag displays under — e.g. `中出し → Creampie`. Japanese tags coming from fc2ppv‑db show under these labels automatically; leave a mapping blank to show the raw tag.

### Actresses & de‑duplication

- Rename any actress (English + Japanese); the old spelling is kept as an alias.
- Tick two or more rows and **Merge** them (the record you own most titles under is kept; works, aliases, links, and body‑info fold in — no movie is ever deleted).
- **Find duplicates** scans the whole catalog for same‑person records (identical Japanese name or reversed romaji) and offers to merge them all.

### Sources & fc2ppv‑db


- Toggle the built‑in scrapers on/off.
- **fc2ppv‑db.com** — the richest source (actress faces, aliases, JP titles, dates, tags, full filmographies). It’s behind a human‑check (Cloudflare Turnstile), so the server can’t log in on its own. Instead:
  1. Log in to fc2ppv‑db.com **in a browser on the same machine as Vault** and clear the human‑check.
  2. DevTools → **Network** → click any fc2ppv‑db request → **Request Headers** → copy the whole **`cookie:`** value (it contains `cf_clearance` + your session) and the **`user‑agent:`** value.
  3. Paste both into Settings, **Save session** (stored in the macOS Keychain), then **Test connection**.
  4. On success, **Scan all →** sweeps your library, writing JP titles, actress faces/aliases, dates, and tags as the top‑priority source.

  The session is bound to your browser’s IP + user‑agent and expires after a while — re‑paste it when the test stops passing.

---

## 11. Using Vault on your phone

Vault is one responsive page — there is no separate mobile site and nothing to install. Open the machine's LAN address from any device on the same network, or over a VPN back to it:

```
http://<this-machine-ip>:8730/
```

On macOS `ipconfig getifaddr en0` prints the address; on Linux use `hostname -I`. `127.0.0.1` only works on the machine itself.

> Vault has **no login and no HTTPS**. Anyone who can reach that address gets your whole library, and `Open in external player` / `Reveal in Finder` act on the host machine. Keep it to networks you trust, and don't port‑forward it.

**What changes on a narrow screen**

| | Desktop | Phone |
|---|---|---|
| Poster wall | 5–7 columns | 2 columns |
| Filters | sidebar, always visible | **☰ Filters** slide‑over drawer; picking one closes it so you see the results |
| Card buttons (♥ / ✓) | appear on hover | always visible — there is no hover on touch |
| Player | video between two side rails | video on top, **Info** / **Parts · Related** tabs beneath |
| Long titles on cards | shown | hidden below 430 px, where they'd swamp the card |

It's driven by window width, not by device detection, so a narrow window on a desktop gets the same treatment — and an iPad reporting itself as a Mac still gets the right layout.

**Adding it to your home screen (iOS):** Share → *Add to Home Screen*. It opens full‑screen like an app.

**Performance.** The grid renders in batches of 120 as you scroll rather than building every card up front — that is what keeps a multi‑thousand‑item library inside mobile Safari's per‑tab memory limit. Scrolling all the way through a very large unfiltered library still accumulates cards; filtering or searching first is both faster and lighter.
