"""dedupe.merge_group: folding two actress records must not lose the portrait,
the co-star credits, or the ids the UI disambiguates by."""
import os
import shutil
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

dedupe = helpers.load("dedupe.py", "dedupe_under_test")


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = sqlite3.connect(helpers.temp_db(self.tmp))
        self.con.row_factory = sqlite3.Row
        # target: has the films, no photo. source: the profile with the photo.
        self.con.execute("INSERT INTO actresses(id,name_en,name_jp) VALUES(1,'Target','なまえ')")
        self.con.execute("INSERT INTO actresses(id,archive_id,name_en,name_jp,image_blob,"
                         "fc2ppvdb_uuid) VALUES(2,555,NULL,'なまえ',?,'uuid-src')",
                         (b"PORTRAIT" * 10,))
        self.con.execute("INSERT INTO works(code,actress_id) VALUES('CODE-A',1)")
        self.con.execute("INSERT INTO works(code,actress_id) VALUES('CODE-B',2)")
        self.con.execute("INSERT INTO work_actresses VALUES('CODE-A',1,'costar')")
        self.con.execute("INSERT INTO work_actresses VALUES('CODE-A',2,'main')")
        self.con.execute("INSERT INTO work_actresses VALUES('CODE-B',2,'main')")
        self.con.commit()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def target(self, col):
        return self.con.execute("SELECT %s FROM actresses WHERE id=1" % col).fetchone()[0]

    def test_portrait_survives(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        self.assertEqual(self.target("length(image_blob)"), 80)

    def test_cast_rows_are_repointed_not_orphaned(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        orphans = self.con.execute(
            "SELECT COUNT(*) FROM work_actresses wa LEFT JOIN actresses a "
            "ON a.id=wa.actress_id WHERE a.id IS NULL").fetchone()[0]
        self.assertEqual(orphans, 0)
        codes = {r[0] for r in self.con.execute(
            "SELECT code FROM work_actresses WHERE actress_id=1")}
        self.assertEqual(codes, {"CODE-A", "CODE-B"})

    def test_a_shared_credit_collapses_to_one_row(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        n = self.con.execute("SELECT COUNT(*) FROM work_actresses WHERE code='CODE-A'"
                             ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_lead_credit_is_not_demoted(self):
        """The source was 'main' on CODE-A; the surviving row must stay 'main'."""
        dedupe.merge_group(self.con, [1, 2], target=1)
        role = self.con.execute("SELECT role FROM work_actresses WHERE code='CODE-A'"
                                ).fetchone()[0]
        self.assertEqual(role, "main")

    def test_unique_archive_id_transfers(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        self.assertEqual(self.target("archive_id"), 555)

    def test_uuid_transfers(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        self.assertEqual(self.target("fc2ppvdb_uuid"), "uuid-src")

    def test_existing_values_are_not_overwritten(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        self.assertEqual(self.target("name_en"), "Target")

    def test_works_are_repointed_and_source_removed(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM works WHERE actress_id=1").fetchone()[0], 2)
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM actresses WHERE id=2").fetchone()[0], 0)

    def test_names_are_kept_as_aliases(self):
        dedupe.merge_group(self.con, [1, 2], target=1)
        aliases = {r[0] for r in self.con.execute(
            "SELECT alias FROM aliases WHERE actress_id=1")}
        self.assertIn("なまえ", aliases)

    def test_single_id_is_a_no_op(self):
        self.assertEqual(dedupe.merge_group(self.con, [1]), {"skipped": True})


if __name__ == "__main__":
    unittest.main()
