"""vault_license.py: Ed25519 against RFC 8032, key parsing, and the free cap in serve.py."""
import datetime
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

vl = helpers.load("webui/vault_license.py", "vault_license_under_test")

# RFC 8032 section 7.1, tests 1 and 2
RFC = [("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
         "1cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613"
         "d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")]
TEST_PUB = bytes.fromhex(RFC[0][0])
# signed with the RFC test-1 secret key, never with the real one
PRO_KEY = ("VAULT1-cHJvfDIwMjctMDEtMzF8dDNzdDAwMDHHICzj1blMu4aYAK8G8sxphwn_kj7Vr57kRG8omsBO2cZUDg"
           "kbo2oYIKE3koD6AqbL3K0VnxYadgGcfi0kO2QD")
TEAM_KEY = ("VAULT1-dGVhbXwyMDI3LTAxLTMxfHQzc3QwMDAyefdT8Yn1W8ULaaB8onoDPmtgoLd9ezxoGuqCDmsSbcuZOWH"
            "uh88rVEniN1GELyxLqVxVLkvgo89Ra4uP0RwwDA")
D = datetime.date


class Ed25519Test(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for pub, msg, sig in RFC:
            pub, msg, sig = map(bytes.fromhex, (pub, msg, sig))
            self.assertTrue(vl.verify(pub, msg, sig))
            self.assertFalse(vl.verify(pub, msg + b"x", sig))
            self.assertFalse(vl.verify(pub, msg, sig[:-1] + bytes([sig[-1] ^ 1])))

    def test_bad_lengths(self):
        self.assertFalse(vl.verify(b"\0" * 31, b"", b"\0" * 64))
        self.assertFalse(vl.verify(TEST_PUB, b"", b"\0" * 63))


class KeyTest(unittest.TestCase):
    def test_valid_key(self):
        st = vl.status(PRO_KEY, today=D(2027, 1, 31), public=TEST_PUB)
        self.assertEqual((st["pro"], st["state"], st["expires"], st["id"]),
                         (True, "pro", "2027-01-31", "t3st0001"))

    def test_whitespace_from_copy_paste_is_ignored(self):
        key = PRO_KEY[:40] + "\n  " + PRO_KEY[40:]
        self.assertTrue(vl.status(key, today=D(2026, 12, 1), public=TEST_PUB)["pro"])

    def test_expired(self):
        st = vl.status(PRO_KEY, today=D(2027, 2, 1), public=TEST_PUB)
        self.assertEqual((st["pro"], st["state"]), (False, "expired"))

    def test_foreign_signer_is_refused(self):
        # the shipped public key did not sign the test key
        st = vl.status(PRO_KEY, today=D(2027, 1, 1))
        self.assertEqual(st["state"], "invalid")

    def test_tampered_payload_is_refused(self):
        import base64
        body = PRO_KEY[len(vl.PREFIX):]
        raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        raw[7] ^= 1                                   # 2027 -> 2026 / 2037 …
        forged = vl.PREFIX + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
        self.assertEqual(vl.status(forged, public=TEST_PUB)["state"], "invalid")

    def test_unknown_tier(self):
        st = vl.status(TEAM_KEY, today=D(2027, 1, 1), public=TEST_PUB)
        self.assertEqual((st["pro"], st["state"]), (False, "invalid"))

    def test_no_key_and_garbage(self):
        self.assertEqual(vl.status("")["state"], "free")
        self.assertEqual(vl.status(None)["state"], "free")
        for junk in ("hello", "VAULT1-", "VAULT1-!!!", "VAULT1-AAAA"):
            self.assertEqual(vl.status(junk)["state"], "invalid", junk)


_DATA = tempfile.mkdtemp(prefix="vaulttest-lic-")
os.environ["VAULT_DATA"] = _DATA
serve = helpers.load("webui/serve.py", "serve_license_under_test")


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


class FreeCapTest(unittest.TestCase):
    def setUp(self):
        self.limit = serve.vault_license.FREE_LIMIT
        serve.vault_license.FREE_LIMIT = 5
        self.status = serve.license_status
        self.items = [{"code": "FC2-PPV-%07d" % i, "added": 100 - i} for i in range(8)]

    def tearDown(self):
        serve.vault_license.FREE_LIMIT = self.limit
        serve.license_status = self.status

    def test_free_keeps_the_oldest_in_their_order(self):
        serve.license_status = lambda: {"pro": False}
        kept = serve._free_cap(list(self.items))
        self.assertEqual([it["code"] for it in kept], [it["code"] for it in self.items[3:]])
        self.assertEqual(serve.STATE["on_disk"], 8)

    def test_pro_keeps_everything(self):
        serve.license_status = lambda: {"pro": True}
        self.assertEqual(len(serve._free_cap(list(self.items))), 8)

    def test_small_library_is_not_capped(self):
        serve.license_status = lambda: {"pro": False}
        self.assertEqual(len(serve._free_cap(self.items[:5])), 5)


if __name__ == "__main__":
    unittest.main()


class MultipleSignersTest(unittest.TestCase):
    """Keys from any listed signer are accepted; dropping a signer revokes its keys."""

    def test_second_signer_accepted(self):
        other = bytes(32)
        self.assertTrue(vl.status(PRO_KEY, today=D(2026, 12, 1), public=[other, TEST_PUB])["pro"])
        self.assertEqual(vl.status(PRO_KEY, today=D(2026, 12, 1), public=[other])["state"], "invalid")

    def test_shipped_list_has_two_distinct_keys(self):
        self.assertEqual(len(vl.PUBLIC_KEYS), 2)
        self.assertEqual(len(set(vl.PUBLIC_KEYS)), 2)
        self.assertTrue(all(len(k) == 32 for k in vl.PUBLIC_KEYS))
