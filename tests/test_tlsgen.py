"""tlsgen.py certificates and Settings → HTTPS switching on a live server (temp folders only)."""
import http.client
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

_DATA = tempfile.mkdtemp(prefix="vaulttest-data-")
os.environ["VAULT_DATA"] = _DATA
tlsgen = helpers.load("webui/tlsgen.py", "tlsgen_under_test")
serve = helpers.load("webui/serve.py", "serve_tlsgen_under_test")


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


def read(path):
    with open(path) as f:
        return f.read()


def handshake(port, cafile, host="127.0.0.1"):
    ctx = ssl.create_default_context(cafile=cafile)
    c = http.client.HTTPSConnection(host, port, context=ctx, timeout=10)
    c.request("GET", "/x")
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


class Hello(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"hello"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CertTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vaulttest-tls-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_certificate_verifies_against_its_ca(self):
        r = tlsgen.make_all(self.dir, names=["localhost", "vaultbox.local"], ips=["127.0.0.1", "10.1.2.3"])
        self.assertTrue(r["new_ca"])
        d = serve._cert_details(os.path.join(self.dir, "vault.crt"))
        self.assertEqual(d["names"], ["localhost", "vaultbox.local"])
        self.assertEqual(d["ips"], ["127.0.0.1", "10.1.2.3"])
        self.assertTrue(d["issuer"].startswith(tlsgen.CA_NAME))
        srv = serve.VaultServer(("127.0.0.1", 0), Hello)
        srv.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv.tls.load_cert_chain(os.path.join(self.dir, "vault.crt"), os.path.join(self.dir, "vault.key"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            self.assertEqual(handshake(port, os.path.join(self.dir, "vault-ca.crt")), (200, b"hello"))
            self.assertEqual(handshake(port, os.path.join(self.dir, "vault-ca.crt"), "localhost"), (200, b"hello"))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_renewing_keeps_the_ca(self):
        tlsgen.make_all(self.dir, names=["localhost"], ips=["127.0.0.1"])
        ca = read(os.path.join(self.dir, "vault-ca.crt"))
        r = tlsgen.make_all(self.dir, names=["localhost"], ips=["127.0.0.1", "10.9.9.9"])
        self.assertFalse(r["new_ca"])
        self.assertEqual(read(os.path.join(self.dir, "vault-ca.crt")), ca)
        self.assertIn("10.9.9.9", serve._cert_details(os.path.join(self.dir, "vault.crt"))["ips"])

    def test_someone_elses_ca_is_not_reused(self):
        with open(os.path.join(self.dir, "vault-ca.key"), "w") as f:
            f.write("-----BEGIN EC PRIVATE KEY-----\nAAAA\n-----END EC PRIVATE KEY-----\n")
        open(os.path.join(self.dir, "vault-ca.crt"), "w").close()
        self.assertIsNone(tlsgen.vault_ca(self.dir))
        self.assertTrue(tlsgen.make_all(self.dir, names=["localhost"], ips=["127.0.0.1"])["new_ca"])

    def test_this_computer(self):
        self.assertEqual(tlsgen.local_names()[0], "localhost")
        self.assertEqual(tlsgen.local_ips()[0], "127.0.0.1")


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="vaulttest-tls-")
        self._old = (serve.TLS_DIR, serve.SERVER, serve.STATE.get("no_tls"))
        serve.TLS_DIR = self.dir
        serve.STATE["no_tls"] = False
        serve.SERVER = self.srv = serve.VaultServer(("127.0.0.1", 0), Hello)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        serve.TLS_DIR, serve.SERVER, serve.STATE["no_tls"] = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def plain(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/x")
        st = c.getresponse().status
        c.close()
        return st

    def test_on_renew_off_on(self):
        ca = os.path.join(self.dir, "vault-ca.crt")
        self.assertFalse(serve._tls_info()["on"])
        self.assertEqual(serve._tls_switch("on"), "new_ca")
        info = serve._tls_info()
        self.assertTrue(info["on"] and info["have"] and info["ours"] and info["has_ca"])
        self.assertEqual(handshake(self.port, ca), (200, b"hello"))      # live, no restart
        self.assertEqual(self.plain(), 200)                              # this machine: plain HTTP still fine
        before = read(ca)
        self.assertEqual(serve._tls_switch("renew"), "renewed")
        self.assertEqual(read(ca), before)
        self.assertEqual(handshake(self.port, ca), (200, b"hello"))
        serve._tls_switch("off")
        info = serve._tls_info()
        self.assertFalse(info["on"] or info["have"])
        self.assertTrue(info["parked"])
        with self.assertRaises((ssl.SSLError, OSError)):
            handshake(self.port, ca)
        self.assertEqual(serve._tls_switch("on"), "")                    # the parked certificate again
        self.assertEqual(read(ca), before)
        self.assertEqual(handshake(self.port, ca), (200, b"hello"))

    def test_no_tls_flag_blocks_switching(self):
        serve.STATE["no_tls"] = True
        with self.assertRaises(RuntimeError):
            serve._tls_switch("on")
        self.assertFalse(serve._tls_info()["available"])


if __name__ == "__main__":
    unittest.main()
