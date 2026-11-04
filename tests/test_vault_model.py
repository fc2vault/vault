"""vault_model: the per-user model folder, the zip installer and the download with a fake server."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

vm = helpers.load("webui/vault_model.py", "vault_model")


def make_zip(path, folder="vault-r2", files=None):
    files = files if files is not None else {"config.json": "{}", "model.safetensors": "x" * 100,
                                             "labels.json": "[]"}
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(folder + "/" + name if folder else name, data)
    return path


class UserModelsDirTest(unittest.TestCase):
    def test_per_platform(self):
        with mock.patch.object(vm.sys, "platform", "darwin"):
            self.assertTrue(vm.user_models_dir().endswith("Library/Application Support/Vault/models"))
        with mock.patch.object(vm.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            self.assertEqual(vm.user_models_dir(), os.path.join(r"C:\Users\x\AppData\Local", "Vault", "models"))
        with mock.patch.object(vm.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/xdg"}):
            self.assertEqual(vm.user_models_dir(), "/tmp/xdg/vault/models")


class InstalledTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_newest_complete_round(self):
        self.assertIsNone(vm.installed(self.dir))
        for name, config in (("vault-r1", True), ("vault-r10", True), ("vault-r11", False), ("other", True)):
            os.makedirs(os.path.join(self.dir, name))
            if config:
                open(os.path.join(self.dir, name, "config.json"), "w").close()
        self.assertEqual(vm.installed(self.dir), "vault-r10")

    def test_missing_folder(self):
        self.assertIsNone(vm.installed(os.path.join(self.dir, "nope")))


class InstallZipTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.zip = os.path.join(self.dir, "m.zip")

    def test_installs_and_replaces(self):
        make_zip(self.zip)
        self.assertEqual(vm.install_zip(self.zip, self.dir), "vault-r2")
        dest = os.path.join(self.dir, "vault-r2")
        self.assertEqual(sorted(os.listdir(dest)), ["config.json", "labels.json", "model.safetensors"])
        self.assertEqual(os.path.getsize(os.path.join(dest, "model.safetensors")), 100)
        # a second install of the same round replaces the folder wholesale
        open(os.path.join(dest, "stale.bin"), "w").close()
        make_zip(self.zip, files={"config.json": "{}", "model.safetensors": "y" * 7})
        vm.install_zip(self.zip, self.dir)
        self.assertEqual(sorted(os.listdir(dest)), ["config.json", "model.safetensors"])
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_refuses_bad_zips(self):
        make_zip(self.zip, files={"config.json": "{}", "../../evil": "x"})
        with self.assertRaises(ValueError):
            vm.install_zip(self.zip, self.dir)
        make_zip(self.zip, folder="weights")
        with self.assertRaises(ValueError):
            vm.install_zip(self.zip, self.dir)
        make_zip(self.zip, files={"model.safetensors": "x"})
        with self.assertRaises(ValueError):
            vm.install_zip(self.zip, self.dir)
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("vault-r1/config.json", "{}")
            z.writestr("vault-r2/config.json", "{}")
        with self.assertRaises(ValueError):
            vm.install_zip(self.zip, self.dir)
        self.assertEqual(os.listdir(self.dir), ["m.zip"])


class FakeResponse(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        vm._LATEST.update(at=0.0, info=None)
        vm.PROGRESS.update(running=False, state="", done=0, total=0, error="", name="")
        zpath = make_zip(os.path.join(self.dir, "src.zip"), folder="vault-r3")
        self.blob = open(zpath, "rb").read()
        os.remove(zpath)
        self.calls = []
        self.refuse = False

    def opener(self, req, timeout=0):
        url = req.full_url
        self.calls.append(url)
        if url.endswith("/releases/latest"):
            rel = {"tag_name": "vault-r3", "published_at": "2026-11-04T06:40:58Z", "body": "notes",
                   "assets": [{"name": "README.txt", "size": 1, "browser_download_url": "https://x/README.txt"},
                              {"name": "vault-r3.zip", "size": len(self.blob),
                               "browser_download_url": "https://example.test/dl/vault-r3.zip"}]}
            return FakeResponse(json.dumps(rel).encode())
        if url == "https://example.test/dl/vault-r3.zip" and not self.refuse:
            return FakeResponse(self.blob, {"Content-Length": str(len(self.blob))})
        from urllib.error import HTTPError
        raise HTTPError(url, 404, "not found", {}, io.BytesIO(b""))

    def test_latest_is_cached_and_reports_offline(self):
        info = vm.latest(opener=self.opener)
        self.assertEqual((info["name"], info["file"], info["size"], info["notes"]),
                         ("vault-r3", "vault-r3.zip", len(self.blob), "notes"))
        self.assertEqual(info["url"], "https://example.test/dl/vault-r3.zip")
        vm.latest(opener=self.opener)
        self.assertEqual(len(self.calls), 1, "second call served from cache")
        vm._LATEST.update(at=0.0, info=None)

        def down(req, timeout=0):
            raise OSError("no route to host")
        self.assertIn("could not reach", vm.latest(opener=down)["error"])

    def test_download_installs_and_cleans_up(self):
        name = vm.download(models_dir=self.dir, opener=self.opener)
        self.assertEqual(name, "vault-r3")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "vault-r3", "config.json")))
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".part")], [])
        p = vm.PROGRESS
        self.assertEqual((p["state"], p["running"], p["done"], p["total"], p["name"]),
                         ("done", False, len(self.blob), len(self.blob), "vault-r3"))
        self.assertEqual(vm.installed(self.dir), "vault-r3")

    def test_refused_download_is_reported(self):
        self.refuse = True
        with self.assertRaises(RuntimeError) as cm:
            vm.download(models_dir=self.dir, opener=self.opener)
        self.assertIn("download refused", str(cm.exception))
        self.assertEqual(vm.PROGRESS["state"], "error")
        self.assertFalse(vm.PROGRESS["running"])
        self.assertIsNone(vm.installed(self.dir))

    def test_only_one_download_at_a_time(self):
        vm.PROGRESS["running"] = True
        with self.assertRaises(RuntimeError):
            vm.download(models_dir=self.dir, opener=self.opener)
        vm.PROGRESS["running"] = False


if __name__ == "__main__":
    unittest.main()
