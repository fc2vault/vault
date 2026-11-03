"""fc2db.net: the gap-filling source added after review 3. Fixtures only — no network."""
import os
import shutil
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

scan = helpers.load("scan.py", "scan_fc2dbnet_t")
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def db(tmp):
    con = sqlite3.connect(helpers.temp_db(tmp))
    for col in ("fc2db_at", "actress_src", "fc2dbnet_at"):
        con.execute("ALTER TABLE works ADD COLUMN %s TEXT" % col)
    con.commit()
    return con


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.d = scan.parse_fc2dbnet_work(fixture("fc2dbnet_work.html"))

    def test_a_real_page_yields_every_field(self):
        d = self.d
        self.assertEqual(d["title_jp"], "テスト作品のタイトル")      # the heading, not og:title
        self.assertEqual(d["release_date"], "2026-10-06")
        self.assertEqual(d["duration"], 1 * 3600 + 45 * 60 + 4)
        self.assertEqual(d["producer"], "テスト販売者")
        self.assertEqual([a["name"] for a in d["actresses"]], ["テスト女優"])
        self.assertEqual(d["actresses"][0]["url"], "https://fc2db.net/actress/42/")
        self.assertEqual(d["tags"], ["NTR", "スレンダー", "無修正"])

    def test_the_cover_is_thumbnailUrl_never_a_recommendation(self):
        """The carousel of OTHER movies reuses the wp-post-image class; "any image on the page"
        attaches the wrong thumbnail."""
        self.assertIn("cover-1234567", self.d["cover_url"])
        self.assertNotIn("other-", self.d["cover_url"])

    def test_og_title_is_not_used_as_a_title(self):
        self.assertNotEqual(self.d["title_jp"], "FC2PPV-1234567")

    def test_the_comment_turnstile_is_not_a_challenge(self):
        """Every real page has a Turnstile on its comment form and a challenge-platform
        script. The fixture puts both near the top on purpose: the guard must clear the page
        because its VideoObject is there, not because the markers sit past byte 4000."""
        html = fixture("fc2dbnet_work.html")
        self.assertLess(html.find("challenge-platform"), 4000)
        self.assertFalse(scan._is_fc2dbnet_challenge(html))

    def test_challenge_and_age_wall_parse_to_nothing(self):
        self.assertEqual(scan.parse_fc2dbnet_work(fixture("fc2dbnet_challenge.html")), {})
        self.assertEqual(scan.parse_fc2dbnet_work(fixture("fc2dbnet_agewall.html")), {})
        self.assertTrue(scan._is_fc2dbnet_challenge(fixture("fc2dbnet_challenge.html")))
        self.assertTrue(scan._is_fc2dbnet_age_wall(fixture("fc2dbnet_agewall.html")))


class FetchTest(unittest.TestCase):
    """fetch_fc2dbnet decides absent / error / data from the HTTP status and the body."""

    def setUp(self):
        self.real = scan._get_status

    def tearDown(self):
        scan._get_status = self.real

    def fetch(self, body, status):
        scan._get_status = lambda *a, **k: (body, status)
        return scan.fetch_fc2dbnet("FC2-PPV-1234567")

    def test_a_404_is_absent(self):
        self.assertEqual(self.fetch(None, 404), {"_missing": True})

    def test_a_challenge_is_an_error(self):
        self.assertIn("challenge", self.fetch(fixture("fc2dbnet_challenge.html"), 200)["_error"])

    def test_an_age_wall_is_an_error_naming_18(self):
        self.assertIn("18+", self.fetch(fixture("fc2dbnet_agewall.html"), 200)["_error"])

    def test_the_age_flag_is_sent_and_only_this_sites(self):
        seen = {}
        def grab(url, headers, timeout=25):
            seen.update(url=url, cookie=headers.get("Cookie"))
            return fixture("fc2dbnet_work.html"), 200
        scan._get_status = grab
        scan.fetch_fc2dbnet("FC2-PPV-1234567")
        self.assertEqual(seen["url"], "https://fc2db.net/work/1234567/")
        self.assertEqual(seen["cookie"], "mdb_age_gate_confirmed=1")


class SolverSessionTest(unittest.TestCase):
    """fc2db.net must use its OWN resolver session: one name is one Chromium, and every
    fc2ppv-db scan ends by destroying the vault-fc2db session."""

    def test_the_fallback_uses_a_separate_session_and_cookie(self):
        sent = []
        real = scan._solver_post
        scan._solver_post = lambda ep, p, timeout: (sent.append(p) or
            ({"status": "ok"} if p["cmd"] == "sessions.create" else
             {"status": "ok", "solution": {"status": 200,
                                           "response": fixture("fc2dbnet_work.html")}}))
        try:
            scan.solver_get("http://solver.test", "https://fc2db.net/work/1234567/",
                            session=scan._FC2DBNET_SESSION)
        finally:
            scan._solver_post = real
            scan._SOLVER_LIVE["sessions"] = {}
        sessions = {p.get("session") for p in sent}
        self.assertEqual(sessions, {"vault-fc2dbnet"})
        get = [p for p in sent if p["cmd"] == "request.get"][0]
        self.assertEqual(get["cookies"][0]["name"], "mdb_age_gate_confirmed")
        self.assertEqual(get["cookies"][0]["domain"], ".fc2db.net")


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = db(self.tmp)
        self.page = scan.parse_fc2dbnet_work(fixture("fc2dbnet_work.html"))

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def row(self, code):
        self.con.row_factory = sqlite3.Row
        r = self.con.execute("SELECT * FROM works WHERE code=?", (code,)).fetchone()
        self.con.row_factory = None
        return r

    def test_an_empty_lead_with_fc2db_at_set_still_gets_this_sites_actress(self):
        """fc2ppv-db's 'nobody listed' stamps fc2db_at and leaves the lead empty. That is
        precisely the case this site exists for — the stamp must not block it."""
        self.con.execute("INSERT INTO works(code,in_library,availability,source,fc2db_at) "
                         "VALUES('FC2-PPV-1234567',1,'available','fs','2026-10-09')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        r = self.row("FC2-PPV-1234567")
        self.assertIsNotNone(r["actress_id"])
        self.assertEqual(r["actress_src"], "fc2db.net")
        name = self.con.execute("SELECT name_jp FROM actresses WHERE id=?",
                                (r["actress_id"],)).fetchone()[0]
        self.assertEqual(name, "テスト女優")
        link = self.con.execute("SELECT url FROM links WHERE kind='fc2db.net'").fetchone()[0]
        self.assertEqual(link, "https://fc2db.net/actress/42/")
        self.assertTrue(r["fc2dbnet_at"])

    def test_an_fc2ppv_db_title_and_a_manual_actress_are_left_alone(self):
        self.con.execute("INSERT INTO actresses(id,name_jp,source) VALUES(9,'手で設定','manual')")
        self.con.execute("INSERT INTO works(code,title,actress_id,actress_src,in_library,"
                         "availability,source) VALUES('FC2-PPV-1234567','fc2ppv-db の題名',9,"
                         "'manual',1,'available','fc2ppv-db')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        r = self.row("FC2-PPV-1234567")
        self.assertEqual(r["title"], "fc2ppv-db の題名")
        self.assertEqual((r["actress_id"], r["actress_src"]), (9, "manual"))

    def test_a_manual_src_on_an_empty_lead_is_still_respected(self):
        self.con.execute("INSERT INTO works(code,actress_src,in_library,availability,source) "
                         "VALUES('FC2-PPV-1234567','manual',1,'available','fs')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        self.assertIsNone(self.row("FC2-PPV-1234567")["actress_id"])

    def test_an_exact_name_reuses_the_existing_actress(self):
        self.con.execute("INSERT INTO actresses(id,name_jp,source) VALUES(5,'テスト女優','catalog')")
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-1234567',1,'available','fs')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        self.assertEqual(self.row("FC2-PPV-1234567")["actress_id"], 5)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM actresses").fetchone()[0], 1)

    def test_co_stars_from_elsewhere_are_never_removed(self):
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-1234567',1,'available','fs')")
        self.con.execute("INSERT INTO work_actresses(code,actress_id,role) "
                         "VALUES('FC2-PPV-1234567',77,'costar')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        ids = {r[0] for r in self.con.execute(
            "SELECT actress_id FROM work_actresses WHERE code='FC2-PPV-1234567'")}
        self.assertIn(77, ids)

    def test_gaps_are_filled_and_tags_canonicalised(self):
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-1234567',1,'available','fs')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        r = self.row("FC2-PPV-1234567")
        self.assertEqual(r["release_date"], "2026-10-06")
        self.assertEqual(r["producer"], "テスト販売者")
        self.assertIn("cover-1234567", r["cover_url"])
        tags = {t[0] for t in self.con.execute("SELECT tag FROM tags")}
        self.assertIn("Uncensored", tags)                      # 無修正 via _canon_tag
        self.assertEqual(r["censorship"], "uncensored")

    def test_an_existing_cover_and_date_are_not_overwritten(self):
        self.con.execute("INSERT INTO works(code,cover_url,release_date,in_library,availability,"
                         "source) VALUES('FC2-PPV-1234567','https://x/keep.jpg','2020-01-01',1,"
                         "'available','fc2ppv-db')")
        scan._apply_fc2dbnet(self.con, "FC2-PPV-1234567", self.page)
        r = self.row("FC2-PPV-1234567")
        self.assertEqual((r["cover_url"], r["release_date"]), ("https://x/keep.jpg", "2020-01-01"))


class PassTest(unittest.TestCase):
    """The scan pass: a 404 is stamped, a block is not."""

    def setUp(self):
        self.tmp = helpers.mkdtemp()
        self.con = db(self.tmp)
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-1234567',1,'available','fs')")
        self.con.commit()
        self.real = scan.fetch_fc2dbnet
        self.real_sleep = scan.time.sleep
        scan.time.sleep = lambda s: None

    def tearDown(self):
        scan.fetch_fc2dbnet = self.real
        scan.time.sleep = self.real_sleep
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stamp(self):
        return self.con.execute("SELECT fc2dbnet_at FROM works "
                                "WHERE code='FC2-PPV-1234567'").fetchone()[0]

    def run_with(self, result):
        scan.fetch_fc2dbnet = lambda *a, **k: result
        p = {}
        scan.scan_fc2dbnet(self.con, ["FC2-PPV-1234567"], progress=p)
        return p

    def test_a_404_is_absent_and_stamped(self):
        p = self.run_with({"_missing": True})
        self.assertEqual((p["absent"], p["failed"]), (1, 0))
        self.assertTrue(self.stamp())

    def test_a_challenge_is_failed_and_not_stamped(self):
        p = self.run_with({"_error": "fc2db.net returned a Cloudflare challenge"})
        self.assertEqual(p["failed"], 1)
        self.assertIsNone(self.stamp())

    def test_an_age_wall_stops_the_run(self):
        scan.fetch_fc2dbnet = lambda *a, **k: {"_error": "fc2db.net returned its 18+ page"}
        p = {}
        self.con.execute("INSERT INTO works(code,in_library,availability,source) "
                         "VALUES('FC2-PPV-1234568',1,'available','fs')")
        scan.scan_fc2dbnet(self.con, ["FC2-PPV-1234567", "FC2-PPV-1234568"], progress=p)
        self.assertEqual(p["failed"], 1)
        self.assertIsNone(self.stamp())


if __name__ == "__main__":
    unittest.main()
