"""tagrules.py: Settings → Tags → Standardize tags — off by default, reversible, and applied to
what scans and catalog merges store while it is on."""
import os
import shutil
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

sys.path.insert(0, helpers.ROOT)        # scan.py / catalogmerge.py import tagrules by name
tr = helpers.load("tagrules.py", "tagrules_under_test")
scan = helpers.load("scan.py", "scan_tagrules_t")
cm = helpers.load("webui/catalogmerge.py", "catalogmerge_tagrules_t")


def tags(con, code=None):
    q = "SELECT code, tag FROM tags" + (" WHERE code=?" if code else "")
    return sorted(con.execute(q, (code,) if code else ()).fetchall())


class RulesTest(unittest.TestCase):
    def test_rule_table(self):
        self.assertEqual(tr.rule("Raw"), "Bareback")
        self.assertEqual(tr.rule("raw"), "Bareback")              # case-insensitive
        self.assertEqual(tr.rule("Cleanup Blowjob"), "Blowjob")
        self.assertEqual(tr.rule("Standing Doggy"), "Standing")
        self.assertEqual(tr.rule("Convinced of Raw"), "Convinced of Raw")   # kept on purpose
        self.assertEqual(tr.rule("Big Nipples"), "Big Nipples")             # nipple tags kept
        self.assertIsNone(tr.rule("Uncensored"))
        self.assertIsNone(tr.rule("Sold Out"))
        self.assertEqual(tr.rule("中出し懇願"), "Creampie")
        self.assertIsNone(tr.rule("未知のタグ"))                             # untranslated JP
        self.assertEqual(tr.rule("Creampie"), "Creampie")
        self.assertEqual(tr.rule("Some New Tag"), "Some New Tag")

    def test_rules_are_one_step(self):
        for src, tgt in tr._SRC.items():
            self.assertNotIn(tgt.lower(), tr._SRC, src)              # a target is never a source
            self.assertNotIn(tgt.lower(), tr._DROP, src)             # ... nor removed
        self.assertFalse(set(tr._SRC) & tr._DROP)


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.d = helpers.mkdtemp()
        self.db = helpers.temp_db(self.d)
        con = sqlite3.connect(self.db)
        con.executemany("INSERT INTO works(code) VALUES(?)", [("A",), ("B",)])
        con.executemany("INSERT INTO tags VALUES(?,?)", [
            ("A", "Raw"), ("A", "Bareback"), ("A", "Sold Out"), ("A", "Creampie"),
            ("B", "Raw"), ("B", "未知のタグ"), ("B", "Loli")])
        con.commit()
        self.con = con

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_off_by_default_and_preview(self):
        self.assertFalse(tr.is_on(self.con))
        p = tr.preview(self.con)
        self.assertEqual(p["tags_before"], 6)
        self.assertEqual(p["tags_after"], 2)                        # Bareback, Creampie
        self.assertEqual(p["movies"], 2)
        self.assertEqual(tags(self.con, "A")[0], ("A", "Bareback"))  # preview wrote nothing
        self.assertEqual(len(tags(self.con)), 7)

    def test_on_then_off_restores(self):
        before = tags(self.con)
        tr.apply(self.con)
        self.assertTrue(tr.is_on(self.con))
        self.assertEqual(tags(self.con), [("A", "Bareback"), ("A", "Creampie"), ("B", "Bareback")])
        self.assertEqual(tr.preview(self.con)["tags_before"], 5)     # the original, minus Loli
        tr.apply(self.con)                                           # idempotent catch-up
        self.assertEqual(len(tags(self.con)), 3)
        tr.restore(self.con)
        self.assertFalse(tr.is_on(self.con))
        self.assertEqual(tags(self.con), [t for t in before if t[1] != "Loli"])   # minors stay gone
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM tag_rules_undo").fetchone()[0], 0)

    def test_restore_keeps_a_tag_you_deleted_meanwhile(self):
        tr.apply(self.con)
        cm.tombstone(self.con, "tag", "Raw")
        tr.restore(self.con)
        self.assertNotIn("Raw", [t for _, t in tags(self.con)])
        self.assertIn(("A", "Sold Out"), tags(self.con))

    def test_minors_always_removed(self):
        self.assertEqual(tr.cleanup_always(self.con), 1)
        self.assertNotIn("Loli", [t for _, t in tags(self.con)])
        self.assertIsNone(tr.canon(self.con, "Loli"))
        self.assertEqual(tr.canon(self.con, "Raw"), "Raw")           # off: everything else as is

    def test_scan_follows_the_switch(self):
        self.assertEqual(scan._canon_tag(self.con, "Raw"), "Raw")
        self.assertEqual(scan._canon_tag(self.con, "無修正"), "Uncensored")   # translations as before
        tr.apply(self.con)
        self.assertEqual(scan._canon_tag(self.con, "Raw"), "Bareback")
        self.assertIsNone(scan._canon_tag(self.con, "無修正"))       # translated, then removed
        self.assertEqual(scan._canon_tag(self.con, "Raw", drop_jp=False), "Raw")   # typed by hand

    def test_ship_puts_originals_back(self):
        tr.apply(self.con)
        self.con.close()
        import subprocess
        subprocess.run([sys.executable, os.path.join(helpers.ROOT, "tagrules.py"), "ship", self.db],
                       check=True, capture_output=True)
        self.con = sqlite3.connect(self.db)
        self.assertFalse(tr.is_on(self.con))
        self.assertIn(("A", "Raw"), tags(self.con))
        self.assertIsNone(self.con.execute(
            "SELECT name FROM sqlite_master WHERE name='tag_rules_undo'").fetchone())


class MergeTest(unittest.TestCase):
    def test_merge_standardizes_when_on(self):
        d = helpers.mkdtemp()
        try:
            user, shipped = helpers.temp_db(os.path.join(d)), None
            sd = os.path.join(d, "s")
            os.makedirs(sd)
            shipped = helpers.temp_db(sd)
            for path in (user, shipped):
                c = sqlite3.connect(path)
                c.executescript("CREATE TABLE IF NOT EXISTS actresses (id INTEGER PRIMARY KEY);")
                for col in ("fc2db_at", "actress_src", "fc2dbnet_at"):
                    try:
                        c.execute("ALTER TABLE works ADD COLUMN %s TEXT" % col)
                    except sqlite3.OperationalError:
                        pass
                c.commit()
                c.close()
            c = sqlite3.connect(shipped)
            c.execute("INSERT INTO works(code) VALUES('C')")
            c.executemany("INSERT INTO tags VALUES('C', ?)", [("Raw",), ("Sold Out",)])
            c.commit()
            c.close()
            c = sqlite3.connect(user)
            tr.apply(c)
            c.close()
            cm.merge(user, shipped)
            c = sqlite3.connect(user)
            self.assertEqual(tags(c, "C"), [("C", "Bareback")])
            c.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
