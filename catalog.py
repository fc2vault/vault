#!/usr/bin/env python3
"""
catalog.py — FC2 actress + works catalog as a NORMALIZED SQLite database,
with an embedded actress thumbnail, structured profile fields, and per-work
availability (do I own it or not).

Tables:
  actresses(id, archive_id, name_en, name_jp, name_kana, age, birthdate,
            height, bust, cup, waist, hip, measurements_raw, description,
            image_url, image_blob, censorship, source)
  aliases(actress_id, alias)
  links(actress_id, kind, url)
  works(code, actress_id, release_date, title, title_en, duration, producer,
        fav_count, censorship, in_library, availability, source)
  tags(code, tag)
  crawl(archive_id, actress_id, ok)          -- crawl bookkeeping / resume

availability: 'available' (in my library) | 'missing' (known, not owned).
censorship: flagged 'uncensored' for everything for now.
Folders use name_en (English). Scraping happens in the browser; this ingests JSON.
"""
import argparse
import base64
import csv
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rename_fc2 import identify  # noqa: E402

DB_DIR = os.path.join(HERE, "db")
DB_PATH = os.path.join(DB_DIR, "catalog.db")
LIBRARY = "/path/to/library"
TAG_EXCLUDE = {"douglas masser"}          # junk tags to drop

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS actresses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER UNIQUE,
  name_en TEXT UNIQUE, name_jp TEXT, name_kana TEXT, age INTEGER, birthdate TEXT,
  height INTEGER, bust INTEGER, cup TEXT, waist INTEGER, hip INTEGER,
  measurements_raw TEXT, description TEXT, image_url TEXT, image_blob BLOB,
  censorship TEXT DEFAULT 'uncensored', source TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
  actress_id INTEGER REFERENCES actresses(id), alias TEXT, UNIQUE(actress_id, alias));
CREATE TABLE IF NOT EXISTS links (
  actress_id INTEGER REFERENCES actresses(id), kind TEXT, url TEXT,
  UNIQUE(actress_id, kind, url));
CREATE TABLE IF NOT EXISTS works (
  code TEXT PRIMARY KEY, actress_id INTEGER REFERENCES actresses(id),
  release_date TEXT, title TEXT, title_en TEXT, duration TEXT, producer TEXT,
  fav_count INTEGER, censorship TEXT DEFAULT 'uncensored',
  in_library INTEGER DEFAULT 0, availability TEXT DEFAULT 'missing', source TEXT);
CREATE INDEX IF NOT EXISTS ix_works_actress ON works(actress_id);
CREATE TABLE IF NOT EXISTS tags (
  code TEXT REFERENCES works(code), tag TEXT, UNIQUE(code, tag));
CREATE TABLE IF NOT EXISTS crawl (
  archive_id INTEGER PRIMARY KEY, actress_id INTEGER, ok INTEGER DEFAULT 1);
"""


def db():
    os.makedirs(DB_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def norm_code(s):
    c = identify(str(s))
    if c:
        return c
    d = re.sub(r"\D", "", str(s))
    return f"FC2-PPV-{d}" if d else None


def parse_age(birthdate):
    m = re.search(r"(\d{1,2})\s*歳", birthdate or "")
    return int(m.group(1)) if m else None


def parse_meas(s):
    s = s or ""
    g = lambda pat: (re.search(pat, s) or [None, None])[1]
    return {"height": _int(g(r"T\s*(\d{2,3})")), "bust": _int(g(r"B\s*(\d{2,3})")),
            "cup": g(r"B\s*\d{2,3}\s*\(?\s*([A-Za-z])"), "waist": _int(g(r"W\s*(\d{2,3})")),
            "hip": _int(g(r"H\s*(\d{2,3})"))}


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def norm_tag(t):
    t = (t or "").strip()
    if not t or t.lower() in TAG_EXCLUDE:
        return None
    return t[:1].upper() + t[1:] if t[:1].isascii() and t[:1].isalpha() else t


def upsert_actress(con, act):
    name_en = (act.get("name_en") or "").strip()
    name_jp = (act.get("name_jp") or "").strip()
    if not (name_en or name_jp):
        return None
    meas = parse_meas(act.get("measurements", ""))
    age = parse_age(act.get("birthdate", ""))
    blob = None
    if act.get("image_b64"):
        try:
            blob = base64.b64decode(act["image_b64"])
        except Exception:
            blob = None
    row = con.execute("SELECT id FROM actresses WHERE (name_en!='' AND name_en=?) "
                      "OR (archive_id IS NOT NULL AND archive_id=?)",
                      (name_en, act.get("archive_id"))).fetchone()
    if row:
        aid = row["id"]
        con.execute("""UPDATE actresses SET archive_id=COALESCE(archive_id,?),
            name_en=COALESCE(NULLIF(name_en,''),?), name_jp=COALESCE(NULLIF(name_jp,''),?),
            name_kana=COALESCE(NULLIF(name_kana,''),?), age=COALESCE(age,?),
            birthdate=COALESCE(NULLIF(birthdate,''),?), height=COALESCE(height,?),
            bust=COALESCE(bust,?), cup=COALESCE(cup,?), waist=COALESCE(waist,?),
            hip=COALESCE(hip,?), measurements_raw=COALESCE(NULLIF(measurements_raw,''),?),
            description=COALESCE(NULLIF(description,''),?), image_url=COALESCE(NULLIF(image_url,''),?),
            image_blob=COALESCE(image_blob,?) WHERE id=?""",
            (act.get("archive_id"), name_en, name_jp, act.get("name_kana", ""), age,
             act.get("birthdate", ""), meas["height"], meas["bust"], meas["cup"],
             meas["waist"], meas["hip"], act.get("measurements", ""),
             act.get("description", ""), act.get("image_url", ""), blob, aid))
    else:
        aid = con.execute("""INSERT INTO actresses(archive_id,name_en,name_jp,name_kana,
            age,birthdate,height,bust,cup,waist,hip,measurements_raw,description,
            image_url,image_blob,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (act.get("archive_id"), name_en or None, name_jp, act.get("name_kana", ""), age,
             act.get("birthdate", ""), meas["height"], meas["bust"], meas["cup"],
             meas["waist"], meas["hip"], act.get("measurements", ""),
             act.get("description", ""), act.get("image_url", ""), blob,
             act.get("source", "bigboobs.pink"))).lastrowid
    for al in act.get("aliases", []):
        if al and al.strip():
            con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) VALUES(?,?)",
                        (aid, al.strip()))
    for lk in act.get("links", []):
        if lk.get("url"):
            con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) VALUES(?,?,?)",
                        (aid, lk.get("kind", ""), lk["url"]))
    if act.get("archive_id"):
        con.execute("INSERT OR REPLACE INTO crawl(archive_id,actress_id,ok) VALUES(?,?,1)",
                    (act["archive_id"], aid))
    return aid


def _load_json(path):
    d = json.load(open(path, encoding="utf-8"))
    return json.loads(d) if isinstance(d, str) else d


def cmd_init(_a):
    db().close(); print(f"schema ready at {DB_PATH}")


def cmd_migrate(_a):
    con = db()
    roster = {}
    rp = os.path.join(DB_DIR, "actresses.csv")
    if os.path.exists(rp):
        for r in csv.DictReader(open(rp, encoding="utf-8")):
            roster[r["actress"]] = r
    mp = os.path.join(DB_DIR, "fc2_actresses.csv")
    n = 0
    if os.path.exists(mp):
        for r in csv.DictReader(open(mp, encoding="utf-8")):
            meta = roster.get(r["actress"], {})
            aid = upsert_actress(con, {"name_en": r["actress"],
                                       "name_jp": meta.get("actress_jp", ""), "source": "csv"})
            con.execute("""INSERT INTO works(code,actress_id,source) VALUES(?,?,?)
                ON CONFLICT(code) DO UPDATE SET actress_id=excluded.actress_id""",
                (r["code"], aid, r.get("source", "csv")))
            n += 1
    for name, meta in roster.items():
        upsert_actress(con, {"name_en": name, "name_jp": meta.get("actress_jp", ""), "source": "csv"})
    con.commit()
    a = con.execute("SELECT COUNT(*) c FROM actresses").fetchone()["c"]
    w = con.execute("SELECT COUNT(*) c FROM works").fetchone()["c"]
    con.close(); print(f"migrated {n} works; DB now {a} actresses, {w} works")


def _library_codes():
    have = set()
    for e in os.listdir(LIBRARY):
        p = os.path.join(LIBRARY, e)
        if not os.path.isdir(p):
            continue
        c = identify(e)
        if c:
            have.add(c)
        else:
            for sub in os.listdir(p):
                cc = identify(sub)
                if cc:
                    have.add(cc)
    return have


def cmd_ingest(a):
    recs = _load_json(a.json)
    if isinstance(recs, dict) and "records" in recs:
        recs = recs["records"]
    recs = recs if isinstance(recs, list) else [recs]
    lib = _library_codes() if getattr(a, "only_library", False) else None
    con = db(); tot = 0; kept = 0
    for r in recs:
        # accept works either as [{code,...}] or a bare codes:[...] list
        works = r.get("works") or [{"code": c} for c in r.get("codes", [])]
        if lib is not None and not any(norm_code(w.get("code")) in lib for w in works):
            continue
        kept += 1
        act = dict(r.get("actress", {})); act.setdefault("source", "bigboobs.pink")
        aid = upsert_actress(con, act)
        if aid is None:
            continue
        for w in works:
            code = norm_code(w.get("code"))
            if not code:
                continue
            con.execute("""INSERT INTO works
                (code,actress_id,release_date,title,producer,fav_count,source)
                VALUES(?,?,?,?,?,?, 'bigboobs.pink')
                ON CONFLICT(code) DO UPDATE SET actress_id=excluded.actress_id,
                  release_date=COALESCE(NULLIF(works.release_date,''),excluded.release_date),
                  title=COALESCE(NULLIF(works.title,''),excluded.title),
                  producer=COALESCE(NULLIF(works.producer,''),excluded.producer),
                  fav_count=COALESCE(works.fav_count,excluded.fav_count)""",
                (code, aid, w.get("date", ""), w.get("title", ""), w.get("producer", ""),
                 w.get("fav")))
            tot += 1
    con.commit(); con.close()
    if lib is not None:
        print(f"ingested {kept} library-relevant actress(es) (of {len(recs)}), {tot} works")
    else:
        print(f"ingested {len(recs)} actress(es), {tot} works")


def cmd_set_meta(a):
    data = _load_json(a.json); con = db(); n = 0
    for code, m in data.items():
        c = norm_code(code)
        con.execute("""INSERT INTO works(code,title_en,duration,source) VALUES(?,?,?,'jav.sb')
            ON CONFLICT(code) DO UPDATE SET
              title_en=COALESCE(NULLIF(excluded.title_en,''),works.title_en),
              duration=COALESCE(NULLIF(excluded.duration,''),works.duration)""",
            (c, m.get("title_en", ""), m.get("duration", "")))
        for t in m.get("tags", []):
            nt = norm_tag(t)
            if nt:
                con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (c, nt))
        n += 1
    con.commit(); con.close(); print(f"metadata merged for {n} works")


def cmd_mark_library(_a):
    con = db()
    con.execute("UPDATE works SET in_library=0, availability='missing'")
    have = set()
    for e in os.listdir(LIBRARY):
        p = os.path.join(LIBRARY, e)
        if not os.path.isdir(p):
            continue
        c = identify(e)
        if c:
            have.add(c)
        else:
            for sub in os.listdir(p):
                cc = identify(sub)
                if cc:
                    have.add(cc)
    n = 0
    for c in have:
        n += con.execute("UPDATE works SET in_library=1, availability='available' "
                         "WHERE code=?", (c,)).rowcount
    con.commit(); con.close()
    print(f"marked {n} works available; {len(have)} library codes total")


def cmd_stats(_a):
    con = db(); q = lambda s: con.execute(s).fetchone()["c"]
    n_act = q("SELECT COUNT(*) c FROM actresses")
    n_icon = q("SELECT COUNT(*) c FROM actresses WHERE image_blob IS NOT NULL")
    n_age = q("SELECT COUNT(*) c FROM actresses WHERE age IS NOT NULL")
    n_w = q("SELECT COUNT(*) c FROM works")
    n_avail = q("SELECT COUNT(*) c FROM works WHERE availability='available'")
    n_miss = q("SELECT COUNT(*) c FROM works WHERE availability='missing'")
    n_al = q("SELECT COUNT(*) c FROM aliases")
    n_lk = q("SELECT COUNT(*) c FROM links")
    n_tg = q("SELECT COUNT(*) c FROM tags")
    n_cr = q("SELECT COUNT(*) c FROM crawl")
    n_named = q("SELECT COUNT(*) c FROM works WHERE in_library=1 AND actress_id IN "
                "(SELECT id FROM actresses WHERE name_en!='')")
    print(f"actresses={n_act} (icons {n_icon}, age {n_age})")
    print(f"works={n_w}  available={n_avail}  missing={n_miss}")
    print(f"aliases={n_al}  links={n_lk}  tags={n_tg}  crawled={n_cr}")
    print(f"library works with a named actress: {n_named}")


def cmd_show(a):
    con = db()
    act = con.execute("SELECT * FROM actresses WHERE name_en LIKE ? OR name_jp LIKE ? LIMIT 1",
                      (f"%{a.name}%", f"%{a.name}%")).fetchone()
    if not act:
        print("not found"); return
    print(f"{act['name_en']} / {act['name_jp']} ({act['name_kana']})  age={act['age']}")
    print(f"  H{act['height']} B{act['bust']}({act['cup']}) W{act['waist']} H{act['hip']}  "
          f"icon={'yes' if act['image_blob'] else 'no'}  censorship={act['censorship']}")
    al = [r["alias"] for r in con.execute("SELECT alias FROM aliases WHERE actress_id=?", (act["id"],))]
    if al:
        print(f"  aliases: {', '.join(al)}")
    w = con.execute("SELECT availability,COUNT(*) c FROM works WHERE actress_id=? GROUP BY availability",
                    (act["id"],)).fetchall()
    print("  works: " + ", ".join(f"{r['availability']}={r['c']}" for r in w))
    con.close()


def cmd_export_icon(a):
    con = db()
    act = con.execute("SELECT name_en,image_blob FROM actresses WHERE name_en LIKE ? "
                      "AND image_blob IS NOT NULL LIMIT 1", (f"%{a.name}%",)).fetchone()
    if not act:
        print("no icon"); return
    out = os.path.join(DB_DIR, f"icon_{act['name_en'].replace(' ','_')}.jpg")
    open(out, "wb").write(act["image_blob"]); print(f"wrote {out} ({len(act['image_blob'])} bytes)")
    con.close()


def cmd_folder(a):
    con = db()
    m = {r["code"]: r["name_en"] for r in con.execute(
        "SELECT w.code,a.name_en FROM works w JOIN actresses a ON a.id=w.actress_id "
        "WHERE a.name_en!='' AND w.in_library=1")}
    con.close()
    print(f"FOLDER by actress (SQLite)  Mode: {'APPLY' if a.apply else 'DRY RUN'}")
    planned = [(e, m[identify(e)]) for e in sorted(os.listdir(LIBRARY))
               if os.path.isdir(os.path.join(LIBRARY, e)) and identify(e) in m]
    for e, name in (planned[:a.limit] if a.limit else planned):
        print(f"      {'MOVE' if a.apply else '[dry] MOVE'}  {e}  ->  {name}/{e}")
        if a.apply:
            d = os.path.join(LIBRARY, name); os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, e)
            if not os.path.exists(dst):
                os.rename(os.path.join(LIBRARY, e), dst)
    print(f"\nFoldered: {len(planned)}")
    if not a.apply:
        print("DRY RUN — re-run with --apply.")


def cmd_frontier(a):
    """Print archive_ids not yet crawled from a candidate list (JSON array),
    for the browser crawler to fetch next."""
    con = db()
    done = {r["archive_id"] for r in con.execute("SELECT archive_id FROM crawl")}
    cand = _load_json(a.json)
    todo = [i for i in cand if int(i) not in done]
    con.close()
    print(json.dumps(todo[:a.limit] if a.limit else todo))


def main():
    ap = argparse.ArgumentParser(description="FC2 actress catalog (SQLite).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    for name, fn in [("ingest", cmd_ingest), ("set-meta", cmd_set_meta), ("frontier", cmd_frontier)]:
        p = sub.add_parser(name); p.add_argument("json")
        if name == "frontier":
            p.add_argument("--limit", type=int, default=0)
        if name == "ingest":
            p.add_argument("--only-library", dest="only_library", action="store_true")
        p.set_defaults(fn=fn)
    sub.add_parser("mark-library").set_defaults(fn=cmd_mark_library)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    p = sub.add_parser("show"); p.add_argument("name"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("export-icon"); p.add_argument("name"); p.set_defaults(fn=cmd_export_icon)
    p = sub.add_parser("folder"); p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=0); p.set_defaults(fn=cmd_folder)
    a = ap.parse_args(); a.fn(a)


if __name__ == "__main__":
    main()
