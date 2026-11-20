"""updater.py: versions, how Vault was installed, the release check, verified downloads, and the
Mac swap helper run for real on a fake Vault.app (temp folders; no real app is quit or opened)."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

updater = helpers.load("webui/updater.py", "updater_under_test")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


BLOB = b"installer bytes " * 1000
REL = {"tag_name": "v9.1.0", "body": "What's new", "published_at": "2030-01-01T00:00:00Z",
       "html_url": "https://github.com/fc2vault/vault/releases/tag/v9.1.0",
       "assets": [{"name": n, "size": len(BLOB), "digest": "sha256:" + hashlib.sha256(BLOB).hexdigest(),
                   "browser_download_url": "https://github.com/fc2vault/vault/releases/download/v9.1.0/" + n}
                  for n in ("Vault-mac-apple-silicon.dmg", "Vault-mac-intel.dmg", "Vault-windows-setup.exe")]}


def opener(req, timeout=0):
    if req.full_url.endswith("/releases/latest"):
        return FakeResponse(json.dumps(REL).encode())
    return FakeResponse(BLOB)


class VersionTest(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v1.10.0", "1.9.1"))
        self.assertTrue(updater.is_newer("1.11", "1.10.0"))
        self.assertFalse(updater.is_newer("v1.10.0", "1.10.0"))
        self.assertFalse(updater.is_newer("vault-r3", "1.10.0"))

    def test_install_kind(self):
        with mock.patch.object(updater.sys, "platform", "darwin"):
            self.assertEqual(updater.install_kind("/Applications/Vault.app/Contents/Resources/app"),
                             ("mac", "/Applications/Vault.app"))
        self.assertEqual(updater.install_kind(os.path.dirname(os.path.abspath(__file__)))[0], "source")
        self.assertEqual(updater.asset_name("mac", "arm64"), "Vault-mac-apple-silicon.dmg")
        self.assertEqual(updater.asset_name("mac", "x86_64"), "Vault-mac-intel.dmg")
        self.assertEqual(updater.asset_name("windows"), "Vault-windows-setup.exe")
        self.assertIsNone(updater.asset_name("source"))

    def test_can_install(self):
        self.assertIn("git pull", updater.can_install("source", None))
        self.assertIn("Applications", updater.can_install(
            "mac", "/private/var/folders/x/AppTranslocation/ABC/d/Vault.app"))


class CheckDownloadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vaulttest-upd-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_check_picks_this_computers_installer(self):
        info = updater.check("1.10.0", "windows", opener=opener)
        self.assertEqual((info["latest"], info["newer"], info["notes"]), ("9.1.0", True, "What's new"))
        self.assertEqual(info["asset"]["name"], "Vault-windows-setup.exe")
        self.assertEqual(updater.check("1.10.0", "source", opener=opener)["asset"], None)

    def test_offline(self):
        def down(req, timeout=0):
            raise OSError("no route to host")
        self.assertIn("could not reach GitHub", updater.check("1.10.0", "mac", opener=down)["error"])

    def test_download_is_verified(self):
        asset = updater.check("1.10.0", "windows", opener=opener)["asset"]
        path = updater.download(asset, self.dir, opener=opener)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), BLOB)
        bad = dict(asset, sha256="0" * 64)
        with self.assertRaises(RuntimeError):
            updater.download(bad, self.dir, opener=opener)
        with self.assertRaises(RuntimeError):
            updater.download(dict(asset, sha256=""), self.dir, opener=opener)
        with self.assertRaises(RuntimeError):
            updater.download(dict(asset, url="https://evil.example/x.exe"), self.dir, opener=opener)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".part")], [])


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("hdiutil"), "macOS only")
class MacSwapTest(unittest.TestCase):
    """The real helper script: mount a disk image, copy its Vault.app, swap, clean up."""
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vaulttest-swap-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_app(self, where, version):
        app = os.path.join(where, "Vault.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        exe = os.path.join(app, "Contents", "MacOS", "Vault")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        with open(os.path.join(app, "Contents", "VERSION"), "w") as f:
            f.write(version)
        return app

    def run_helper(self, dmg, app):
        helper = os.path.join(self.dir, "helper.sh")
        with open(helper, "w") as f:
            f.write(updater.MAC_HELPER)
        log = os.path.join(self.dir, "update.log")
        r = subprocess.run(["/bin/bash", helper, dmg, app, log, "io.github.fc2vault.vault-test-none", "0"],
                           capture_output=True, timeout=120)
        with open(log) as f:
            return r.returncode, f.read()

    def test_swap(self):
        apps = os.path.join(self.dir, "Applications")
        os.makedirs(apps)
        app = self.fake_app(apps, "old")
        src = os.path.join(self.dir, "src")
        os.makedirs(src)
        self.fake_app(src, "new")
        dmg = os.path.join(self.dir, "Vault-mac-apple-silicon.dmg")
        subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", src, "-format", "UDZO", dmg], check=True)
        code, log = self.run_helper(dmg, app)
        self.assertEqual(code, 0, log)
        with open(os.path.join(app, "Contents", "VERSION")) as f:
            self.assertEqual(f.read(), "new")
        self.assertEqual(sorted(os.listdir(apps)), ["Vault.app"])        # no stage / old copies left
        self.assertFalse(os.path.exists(dmg))

    def test_bad_image_keeps_the_old_app(self):
        apps = os.path.join(self.dir, "Applications")
        os.makedirs(apps)
        app = self.fake_app(apps, "old")
        empty = os.path.join(self.dir, "empty")
        os.makedirs(empty)
        dmg = os.path.join(self.dir, "broken.dmg")
        subprocess.run(["hdiutil", "create", "-quiet", "-srcfolder", empty, "-format", "UDZO", dmg], check=True)
        code, log = self.run_helper(dmg, app)
        self.assertNotEqual(code, 0)
        self.assertIn("no Vault.app", log)
        with open(os.path.join(app, "Contents", "VERSION")) as f:
            self.assertEqual(f.read(), "old")


if __name__ == "__main__":
    unittest.main()
