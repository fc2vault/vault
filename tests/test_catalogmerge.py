"""catalogmerge.py: a newer shipped catalog goes into the user's copy without touching their data."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

cm = helpers.load("webui/catalogmerge.py", "catalogmerge_under_test")

SCHEMA = """
CREATE TABLE actresses (id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER UNIQUE, name_en TEXT,
 name_jp TEXT, name_kana TEXT, age INTEGER, birthdate TEXT, height INTEGER, bust INTEGER, cup TEXT,
 waist INTEGER, hip INTEGER, measurements_raw TEXT, description TEXT, image_url TEXT, image_blob BLOB,
 censorship TEXT DEFAULT 'uncensored', source TEXT, fc2ppvdb_uuid TEXT);
CREATE TABLE aliases (actress_id INTEGER, alias TEXT, UNIQUE(actress_id, alias));
CREATE TABLE links (actress_id INTEGER, kind TEXT, url TEXT, UNIQUE(actress_id, kind, url));
CREATE TABLE works (code TEXT PRIMARY KEY, actress_id INTEGER, release_date TEXT, title TEXT, title_en TEXT,
 duration TEXT, producer TEXT, fav_count INTEGER, censorship TEXT DEFAULT 'uncensored',
 in_library INTEGER DEFAULT 0, availability TEXT DEFAULT 'missing', source TEXT, cover_url TEXT,
 censorship_src TEXT, fc2db_at TEXT, actress_src TEXT, fc2dbnet_at TEXT);
CREATE TABLE tags (code TEXT, tag TEXT, UNIQUE(code, tag));
CREATE TABLE tag_translations (raw TEXT PRIMARY KEY COLLATE NOCASE, display TEXT);
CREATE TABLE work_actresses (code TEXT NOT NULL, actress_id INTEGER NOT NULL, role TEXT DEFAULT 'costar',
 PRIMARY KEY(code, actress_id));
"""


def db(path, sql):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA + sql)
    con.commit()
    con.close()


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vaulttest-merge-")
        self.user, self.ship = os.path.join(self.dir, "user.db"), os.path.join(self.dir, "ship.db")
        db(self.user, """
          INSERT INTO actresses (id, name_en, name_jp, cup) VALUES (5, 'Sora Mikumo', '美雲そら', 'C');
          INSERT INTO actresses (id, name_en) VALUES (6, 'Renamed By User');
          INSERT INTO aliases VALUES (6, 'Old Name');
          INSERT INTO works (code, actress_id, title, in_library, availability, actress_src, censorship)
            VALUES ('FC2-PPV-1', 5, 'my title', 1, 'available', 'manual', 'censored');
          INSERT INTO works (code, in_library, availability) VALUES ('FC2-PPV-2', 1, 'available');
          INSERT INTO tag_translations VALUES ('巨乳', 'Big Tits');
        """)
        db(self.ship, """
          INSERT INTO actresses (id, name_en, name_jp, cup, height) VALUES (1, 'Mikumo Sora', '美雲そら', 'D', 150);
          INSERT INTO actresses (id, name_en, height) VALUES (2, 'Old Name', 160);
          INSERT INTO actresses (id, name_en, name_jp) VALUES (3, 'Brand New', '新人');
          INSERT INTO actresses (id, name_en) VALUES (4, 'Deleted By User');
          INSERT INTO aliases VALUES (3, 'Newbie');
          INSERT INTO works (code, actress_id, title, title_en, in_library, availability)
            VALUES ('FC2-PPV-1', 1, 'shipped title', 'English title', 0, 'available');
          INSERT INTO works (code, actress_id, title) VALUES ('FC2-PPV-2', 3, 'two');
          INSERT INTO works (code, actress_id, title) VALUES ('FC2-PPV-3', 3, 'three');
          INSERT INTO works (code, actress_id, title) VALUES ('FC2-PPV-4', 4, 'gone');
          INSERT INTO work_actresses VALUES ('FC2-PPV-1', 3, 'costar');
          INSERT INTO work_actresses VALUES ('FC2-PPV-3', 3, 'main');
          INSERT INTO tags VALUES ('FC2-PPV-1', '巨乳'), ('FC2-PPV-2', 'Amateur'), ('FC2-PPV-3', '素人'),
                                  ('FC2-PPV-2', 'Removed Tag');
          INSERT INTO tag_translations VALUES ('巨乳', 'Busty'), ('素人', 'Amateur');
        """)
        con = sqlite3.connect(self.user)
        cm.tombstone(con, "actress", "Deleted By User")
        cm.tombstone(con, "work", "FC2-PPV-4")
        cm.tombstone(con, "tag", "Removed Tag")
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def q(self, sql, *a):
        con = sqlite3.connect(self.user)
        try:
            return con.execute(sql, a).fetchall()
        finally:
            con.close()

    def test_merge(self):
        st = cm.merge(self.user, self.ship)
        # matched by Japanese name: empty field filled, own values kept, no duplicate
        self.assertEqual(self.q("SELECT name_en, cup, height FROM actresses WHERE id=5"), [("Sora Mikumo", "C", 150)])
        # matched through an alias
        self.assertEqual(self.q("SELECT name_en, height FROM actresses WHERE id=6"), [("Renamed By User", 160)])
        # new actress added with her alias; a deleted one stays deleted
        new = self.q("SELECT id, source FROM actresses WHERE name_en='Brand New'")
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0][1], "catalog")
        self.assertEqual(self.q("SELECT alias FROM aliases WHERE actress_id=?", new[0][0]), [("Newbie",)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM actresses WHERE name_en='Deleted By User'"), [(0,)])
        # own movie: title kept, empty English title filled, ownership/censorship/hand-set cast untouched
        self.assertEqual(self.q("SELECT title, title_en, in_library, actress_id, censorship FROM works WHERE code='FC2-PPV-1'"),
                         [("my title", "English title", 1, 5, "censored")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM work_actresses WHERE code='FC2-PPV-1'"), [(0,)])
        # own movie without a performer gets the shipped one
        self.assertEqual(self.q("SELECT actress_id, actress_src FROM works WHERE code='FC2-PPV-2'"), [(new[0][0], "catalog")])
        # new movie: a catalog entry, not owned, not wishlist; deleted movie stays deleted
        self.assertEqual(self.q("SELECT in_library, availability, source FROM works WHERE code='FC2-PPV-3'"),
                         [(0, "available", "catalog")])
        self.assertEqual(self.q("SELECT COUNT(*) FROM works WHERE code='FC2-PPV-4'"), [(0,)])
        # tags through the user's own translations; deleted tags and raw Japanese stay out
        self.assertEqual(self.q("SELECT tag FROM tags WHERE code='FC2-PPV-1'"), [("Big Tits",)])
        self.assertEqual(self.q("SELECT tag FROM tags WHERE code='FC2-PPV-2'"), [("Amateur",)])
        self.assertEqual(self.q("SELECT tag FROM tags WHERE code='FC2-PPV-3'"), [("Amateur",)])
        self.assertEqual(st["works_added"], 1)
        self.assertEqual(st["actresses_added"], 1)
        # a second run changes nothing
        self.assertEqual(set(cm.merge(self.user, self.ship).values()), {0})

    def test_meta(self):
        con = sqlite3.connect(self.user)
        self.assertIsNone(cm.get_meta(con, "shipped_sha"))
        cm.set_meta(con, "shipped_sha", "abc")
        self.assertEqual(cm.get_meta(con, "shipped_sha"), "abc")
        con.close()


if __name__ == "__main__":
    unittest.main()
