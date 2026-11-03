"""Shared fixtures. Everything here works on temp directories and temp databases —
no test may touch the real library or catalog.db."""
import importlib.util
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(relpath, name=None):
    """Import a module by path (the tree is scripts, not a package).

    The dev tree keeps the server under webui/ and everything else at the root; a release is
    flat. Accept either, so the shipped suite runs where it is shipped."""
    path = os.path.join(ROOT, relpath)
    if not os.path.exists(path):
        flat = os.path.join(ROOT, os.path.basename(relpath))
        if os.path.exists(flat):
            path = flat
    name = name or os.path.splitext(os.path.basename(relpath))[0] + "_t"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:          # serve.py's __main__ guard
        pass
    return mod


SCHEMA = """
CREATE TABLE actresses (id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER UNIQUE,
 name_en TEXT, name_jp TEXT, name_kana TEXT, age INTEGER, birthdate TEXT, height INTEGER,
 bust INTEGER, cup TEXT, waist INTEGER, hip INTEGER, measurements_raw TEXT, description TEXT,
 image_url TEXT, image_blob BLOB, censorship TEXT, source TEXT, fc2ppvdb_uuid TEXT);
CREATE TABLE works (code TEXT PRIMARY KEY, actress_id INTEGER, release_date TEXT, title TEXT,
 title_en TEXT, duration TEXT, producer TEXT, fav_count INTEGER, censorship TEXT,
 censorship_src TEXT, in_library INTEGER DEFAULT 0, availability TEXT DEFAULT 'missing',
 source TEXT, cover_url TEXT);
CREATE TABLE tags (code TEXT, tag TEXT, UNIQUE(code, tag));
CREATE TABLE tag_translations (raw TEXT PRIMARY KEY COLLATE NOCASE, display TEXT);
CREATE TABLE aliases (actress_id INTEGER, alias TEXT, UNIQUE(actress_id, alias));
CREATE TABLE links (actress_id INTEGER, kind TEXT, url TEXT, UNIQUE(actress_id, kind, url));
CREATE TABLE work_actresses (code TEXT NOT NULL, actress_id INTEGER NOT NULL,
 role TEXT DEFAULT 'costar', PRIMARY KEY(code, actress_id));
CREATE TABLE crawl (archive_id INTEGER PRIMARY KEY, actress_id INTEGER, ok INTEGER DEFAULT 1);
"""


def temp_db(tmpdir, translations=(("無修正", "Uncensored"), ("薄モ", "Light Mosaic"))):
    db = os.path.join(tmpdir, "catalog.db")
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for raw, disp in translations:
        con.execute("INSERT INTO tag_translations(raw, display) VALUES(?,?)", (raw, disp))
    con.commit()
    con.close()
    return db


def movie(root, code, name=None, size=2048, extra=()):
    """A release folder with one video (and optional companion files)."""
    d = os.path.join(root, name or code)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, code + ".mp4"), "wb") as f:
        f.write(b"\0" * size)
    for fn in extra:
        with open(os.path.join(d, fn), "w") as f:
            f.write("x")
    return d


def mkdtemp():
    return tempfile.mkdtemp(prefix="vaulttest-")
