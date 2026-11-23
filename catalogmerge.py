"""Bring a newer shipped catalog into your own copy, without touching what is yours.

The installers (and a release folder) ship data/catalog.db: titles, covers, actresses with their
profiles, aliases and filmographies. On first start Vault copies it into your data folder and from
then on works on that copy. When a later version ships a newer catalog, merge() folds it in:

  * actresses you don't have are added; ones you have (matched by fc2ppv-db id, archive id,
    Japanese name, English name or an alias, in that order) only get their EMPTY fields filled,
    plus any new aliases and links;
  * movies you don't have in the catalog are added as catalog entries (not owned, not wishlist);
    ones you have only get empty fields filled, and a performer only where none is set;
  * cast rows, tags (through your own tag translations and merges) and tag translations are
    added where missing.

Never touched: what you own (in_library / availability), favourites, ratings and watched state
(those live in userdata.json anyway), censorship, any field that already has a value, a cast you
set by hand. Things you deleted (a movie, an actress, a tag) are remembered as tombstones from
1.11 on and never come back with a merge. With Settings → Tags → Standardize tags on, the
shipped tags go through the same rules as a scan's."""
import hashlib
import os
import re
import sqlite3

try:
    import tagrules as _tagrules   # Settings → Tags → Standardize tags (optional module)
except ImportError:
    _tagrules = None

TOMBSTONES = "CREATE TABLE IF NOT EXISTS catalog_tombstones (kind TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(kind, key))"
META = "CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT)"

_ACTRESS_FIELDS = ("name_jp", "name_kana", "age", "birthdate", "height", "bust", "cup", "waist", "hip",
                   "measurements_raw", "description", "image_url", "image_blob", "fc2ppvdb_uuid")
_WORK_FIELDS = ("release_date", "title", "title_en", "duration", "producer", "fav_count", "cover_url",
                "fc2db_at", "fc2dbnet_at")
_JP = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tombstone(con, kind, *keys):
    """Remember that the user deleted this (kind: work | actress | tag), so merges skip it."""
    con.execute(TOMBSTONES)
    for k in keys:
        k = (k or "").strip().lower() if isinstance(k, str) else k
        if k:
            con.execute("INSERT OR IGNORE INTO catalog_tombstones VALUES (?, ?)", (kind, str(k)))


def get_meta(con, key):
    con.execute(META)
    r = con.execute("SELECT value FROM catalog_meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def set_meta(con, key, value):
    con.execute(META)
    con.execute("INSERT OR REPLACE INTO catalog_meta VALUES (?, ?)", (key, value))


def _empty(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _cols(con, table, schema="main"):
    return [r[1] for r in con.execute("PRAGMA %s.table_info(%s)" % (schema, table))]


def merge(user_db, shipped_db):
    """Fold shipped_db into user_db. -> {"actresses_added", "actresses_filled", "works_added",
    "works_filled", "tags_added", "aliases_added"}."""
    con = sqlite3.connect(user_db, timeout=60)
    try:
        con.execute(TOMBSTONES)
        con.execute("ATTACH DATABASE ? AS s", (shipped_db,))
        st = dict(actresses_added=0, actresses_filled=0, works_added=0, works_filled=0,
                  tags_added=0, aliases_added=0)
        dead = {(k, v) for k, v in con.execute("SELECT kind, key FROM catalog_tombstones")}

        # ---- actresses: map every shipped id to one of ours (existing or new)
        ucols = set(_cols(con, "actresses"))
        by = {"uuid": {}, "arch": {}, "jp": {}, "en": {}, "alias": {}}
        for aid, arch, uuid, en, jp in con.execute(
                "SELECT id, archive_id, fc2ppvdb_uuid, name_en, name_jp FROM actresses"):
            if uuid:
                by["uuid"].setdefault(uuid, aid)
            if arch is not None:
                by["arch"].setdefault(arch, aid)
            if jp and jp.strip():
                by["jp"].setdefault(jp.strip(), aid)
            if en and en.strip():
                by["en"].setdefault(en.strip().lower(), aid)
        for aid, alias in con.execute("SELECT actress_id, alias FROM aliases"):
            if alias and alias.strip():
                by["alias"].setdefault(alias.strip().lower(), aid)
        used_arch = set(by["arch"])
        scols = [c for c in _cols(con, "actresses", "s") if c in ucols and c != "id"]
        amap = {}
        s_alias = {}
        for aid, alias in con.execute("SELECT actress_id, alias FROM s.aliases"):
            s_alias.setdefault(aid, []).append(alias)
        for row in con.execute("SELECT id, %s FROM s.actresses" % ", ".join(scols)).fetchall():
            sid, rec = row[0], dict(zip(scols, row[1:]))
            en, jp, uuid = (rec.get("name_en") or "").strip(), (rec.get("name_jp") or "").strip(), rec.get("fc2ppvdb_uuid")
            if any(("actress", k) in dead for k in (en.lower(), jp.lower(), (uuid or "").lower()) if k):
                continue
            uid = (by["uuid"].get(uuid) if uuid else None) \
                or (by["arch"].get(rec.get("archive_id")) if rec.get("archive_id") is not None else None) \
                or (by["jp"].get(jp) if jp else None) or (by["en"].get(en.lower()) if en else None) \
                or next((by["alias"].get(a.strip().lower()) for a in [en, jp] + s_alias.get(sid, [])
                         if a and by["alias"].get(a.strip().lower())), None)
            if uid:
                cur = dict(zip(_ACTRESS_FIELDS, con.execute(
                    "SELECT %s FROM actresses WHERE id=?" % ", ".join(_ACTRESS_FIELDS), (uid,)).fetchone()))
                fill = {k: rec[k] for k in _ACTRESS_FIELDS if k in rec and _empty(cur.get(k)) and not _empty(rec[k])}
                if fill:
                    con.execute("UPDATE actresses SET %s WHERE id=?" % ", ".join("%s=?" % k for k in fill),
                                list(fill.values()) + [uid])
                    st["actresses_filled"] += 1
            else:
                rec = dict(rec, source="catalog")
                if rec.get("archive_id") in used_arch:
                    rec["archive_id"] = None
                keys = [k for k in rec if k in ucols]
                uid = con.execute("INSERT INTO actresses (%s) VALUES (%s)" % (", ".join(keys), ", ".join("?" * len(keys))),
                                  [rec[k] for k in keys]).lastrowid
                st["actresses_added"] += 1
                if rec.get("archive_id") is not None:
                    used_arch.add(rec["archive_id"])
                for k, key in (("jp", jp), ("en", en.lower()), ("uuid", uuid)):
                    if key:
                        by[k].setdefault(key, uid)
            amap[sid] = uid
        for sid, alias in con.execute("SELECT actress_id, alias FROM s.aliases").fetchall():
            if sid in amap and alias:
                st["aliases_added"] += con.execute("INSERT OR IGNORE INTO aliases VALUES (?, ?)",
                                                   (amap[sid], alias)).rowcount
        for sid, kind, url in con.execute("SELECT actress_id, kind, url FROM s.links").fetchall():
            if sid in amap:
                con.execute("INSERT OR IGNORE INTO links VALUES (?, ?, ?)", (amap[sid], kind, url))

        # ---- works
        wcols = set(_cols(con, "works"))
        fields = [f for f in _WORK_FIELDS if f in wcols and f in set(_cols(con, "works", "s"))]
        have = {r[0]: r[1:] for r in con.execute(
            "SELECT code, actress_id, actress_src, %s FROM works" % ", ".join(fields))}
        manual_cast = {c for c, v in have.items() if v[1] == "manual"}
        for row in con.execute("SELECT code, actress_id, %s FROM s.works" % ", ".join(fields)).fetchall():
            code, said, vals = row[0], row[1], dict(zip(fields, row[2:]))
            if ("work", code.lower()) in dead:
                continue
            uid = amap.get(said)
            if code in have:
                cur = dict(zip(fields, have[code][2:]))
                fill = {k: v for k, v in vals.items() if _empty(cur.get(k)) and not _empty(v)}
                if uid and have[code][0] is None and code not in manual_cast:
                    fill["actress_id"] = uid
                    fill["actress_src"] = "catalog"
                if fill:
                    con.execute("UPDATE works SET %s WHERE code=?" % ", ".join("%s=?" % k for k in fill),
                                list(fill.values()) + [code])
                    st["works_filled"] += 1
            else:
                rec = dict(vals, code=code, actress_id=uid, in_library=0, availability="available",
                           source="catalog", actress_src="catalog" if uid else None)
                keys = [k for k in rec if k in wcols]
                con.execute("INSERT INTO works (%s) VALUES (%s)" % (", ".join(keys), ", ".join("?" * len(keys))),
                            [rec[k] for k in keys])
                have[code] = (uid, rec["actress_src"]) + tuple(vals.get(f) for f in fields)
                st["works_added"] += 1
        for code, said, role in con.execute("SELECT code, actress_id, role FROM s.work_actresses").fetchall():
            if code in have and code not in manual_cast and said in amap:
                con.execute("INSERT OR IGNORE INTO work_actresses (code, actress_id, role) VALUES (?, ?, ?)",
                            (code, amap[said], role or "costar"))

        # ---- tag translations first (yours win), then tags through them
        con.execute("INSERT OR IGNORE INTO tag_translations SELECT raw, display FROM s.tag_translations")
        tx = {r[0].lower(): r[1] for r in con.execute("SELECT raw, display FROM tag_translations") if r[0]}
        for code, tag in con.execute("SELECT code, tag FROM s.tags").fetchall():
            if code not in have or not tag:
                continue
            t = (tx.get(tag.strip().lower()) or tag).strip()
            if t and _tagrules:
                t = _tagrules.canon(con, t)          # Standardize tags, when switched on
            if not t or _JP.search(t) or ("tag", t.lower()) in dead:
                continue
            st["tags_added"] += con.execute("INSERT OR IGNORE INTO tags (code, tag) VALUES (?, ?)", (code, t)).rowcount
        con.commit()
        con.execute("DETACH DATABASE s")
        return st
    finally:
        con.close()
