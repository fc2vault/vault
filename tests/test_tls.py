"""serve.py HTTPS: one port for TLS, loopback plain HTTP, and a redirect for plain HTTP from the LAN."""
import http.client
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers

_DATA = tempfile.mkdtemp(prefix="vaulttest-data-")
os.environ["VAULT_DATA"] = _DATA
serve = helpers.load("webui/serve.py", "serve_tls_under_test")
OPENSSL = shutil.which("openssl")


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


class Hello(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"hello " + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(OPENSSL, "needs openssl to make a test certificate")
class TlsServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tls_dir = tempfile.mkdtemp(prefix="vaulttest-tls-")
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-days", "2", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", os.path.join(cls.tls_dir, "vault.key"),
                        "-out", os.path.join(cls.tls_dir, "vault.crt")],
                       check=True, capture_output=True)
        shutil.copy(os.path.join(cls.tls_dir, "vault.crt"), os.path.join(cls.tls_dir, "vault-ca.crt"))
        cls._old_dir, serve.TLS_DIR = serve.TLS_DIR, cls.tls_dir
        cls.srv = serve.VaultServer(("127.0.0.1", 0), Hello)
        cls.srv.tls = serve._tls_context()
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        serve.TLS_DIR = cls._old_dir
        shutil.rmtree(cls.tls_dir, ignore_errors=True)

    def test_no_certificate_means_plain_http(self):
        old, serve.TLS_DIR = serve.TLS_DIR, tempfile.mkdtemp()
        try:
            self.assertIsNone(serve._tls_context())
        finally:
            shutil.rmtree(serve.TLS_DIR, ignore_errors=True)
            serve.TLS_DIR = old

    def test_https_is_served(self):
        ctx = ssl.create_default_context(cafile=os.path.join(self.tls_dir, "vault.crt"))
        c = http.client.HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=10)
        c.request("GET", "/a")
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (200, b"hello /a"))
        c.request("GET", "/b")                     # keep-alive over TLS
        self.assertEqual(c.getresponse().read(), b"hello /b")
        c.close()

    def test_loopback_plain_http_still_works(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/x")
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (200, b"hello /x"))
        c.close()

    def _plain_from_lan(self, raw):
        a, b = socket.socketpair()
        t = threading.Thread(target=self.srv.finish_request, args=(a, ("10.1.2.3", 50000)))
        t.start()
        b.sendall(raw)
        t.join(10)
        self.srv.shutdown_request(a)               # what the real server does after the handler
        data = b""
        b.settimeout(10)
        while True:
            chunk = b.recv(65536)
            if not chunk:
                break
            data += chunk
        b.close()
        return data

    def test_plain_http_from_the_lan_is_redirected(self):
        data = self._plain_from_lan(b"GET /movie?x=1 HTTP/1.1\r\nHost: vault.lan:8730\r\n\r\n")
        head = data.split(b"\r\n\r\n")[0].decode()
        self.assertIn(" 307 ", head.splitlines()[0])
        self.assertIn("Location: https://vault.lan:8730/movie?x=1", head)

    def test_ca_certificate_is_downloadable_over_plain_http(self):
        data = self._plain_from_lan(b"GET /vault-ca.crt HTTP/1.1\r\nHost: vault.lan:8730\r\n\r\n")
        head, body = data.split(b"\r\n\r\n", 1)
        self.assertIn(b" 200 ", head.splitlines()[0])
        self.assertIn(b"application/x-x509-ca-cert", head)
        self.assertTrue(body.startswith(b"-----BEGIN CERTIFICATE-----"))


class StreamGuardTest(unittest.TestCase):
    def test_streaming_never_sendfiles_into_tls(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "webui", "serve.py")   # dev tree; a release is flat
        if not os.path.exists(path):
            path = os.path.join(root, "serve.py")
        src = open(path, encoding="utf-8").read()
        self.assertIn("while remaining > 0 and not isinstance(sock, ssl.SSLSocket):", src)


if __name__ == "__main__":
    unittest.main()
