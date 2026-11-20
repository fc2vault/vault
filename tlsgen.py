"""Make Vault's HTTPS certificates with nothing but the Python standard library.

Settings → HTTPS → Turn on HTTPS calls make_all(): a small private certificate authority (CA,
10 years) and a server certificate signed by it (825 days, the longest Apple devices accept),
naming this computer: localhost, its host names and its network addresses. Devices trust the
CA once (it is offered at /vault-ca.crt); renewing the server certificate later, after the
computer got a new address, keeps that trust because the CA stays the same.

There is no openssl on Windows and no `cryptography` package in the installers, so RSA keys,
the X.509 encoding (DER) and the signatures (PKCS #1 v1.5, SHA-256) are done here by hand.
The ssl module loads and checks the result; tests/test_tlsgen.py does a real handshake with it.

Files in the tls folder: vault-ca.crt, vault-ca.key, vault.crt, vault.key (PEM)."""
import base64
import datetime
import hashlib
import ipaddress
import os
import secrets
import socket
import subprocess
import sys

CA_NAME = "Vault local CA"
CA_DAYS = 3650
CERT_DAYS = 825

# ---------------------------------------------------------------- DER


def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _seq(*items):
    return _tlv(0x30, b"".join(items))


def _set(*items):
    return _tlv(0x31, b"".join(items))


def _int(n):
    b = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big", signed=False)
    if b[0] & 0x80:
        b = b"\x00" + b
    while len(b) > 1 and b[0] == 0 and not b[1] & 0x80:
        b = b[1:]
    return _tlv(0x02, b)


def _oid(dotted):
    parts = [int(x) for x in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


def _bits(data, unused=0):
    return _tlv(0x03, bytes([unused]) + data)


def _octets(data):
    return _tlv(0x04, data)


def _bool(v):
    return _tlv(0x01, b"\xff" if v else b"\x00")


def _null():
    return b"\x05\x00"


def _utf8(s):
    return _tlv(0x0C, s.encode("utf-8"))


def _time(dt):
    if dt.year < 2050:
        return _tlv(0x17, dt.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, dt.strftime("%Y%m%d%H%M%SZ").encode())


def _explicit(n, body):
    return _tlv(0xA0 | n, body)


def _read(data, i=0):
    """-> (tag, body, next index) of the TLV at data[i]."""
    tag, ln = data[i], data[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(data[i:i + k], "big")
        i += k
    return tag, data[i:i + ln], i + ln


# ---------------------------------------------------------------- RSA

_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]


def _probable_prime(n, rounds=40):
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits, e):
    while True:
        p = secrets.randbits(bits) | (3 << (bits - 2)) | 1      # top two bits set: n has full length
        if p % e != 1 and _probable_prime(p):
            return p


class RSAKey:
    def __init__(self, n, e, d, p, q):
        self.n, self.e, self.d, self.p, self.q = n, e, d, p, q
        self.dp, self.dq, self.qi = d % (p - 1), d % (q - 1), pow(q, -1, p)

    @classmethod
    def generate(cls, bits=2048, e=65537):
        while True:
            p, q = _prime(bits // 2, e), _prime(bits // 2, e)
            n = p * q
            if p != q and n.bit_length() == bits:
                break
        d = pow(e, -1, (p - 1) * (q - 1) // _gcd(p - 1, q - 1))
        return cls(n, e, d, max(p, q), min(p, q))

    def public_der(self):
        """SubjectPublicKeyInfo."""
        return _seq(_seq(_oid("1.2.840.113549.1.1.1"), _null()),
                    _bits(_seq(_int(self.n), _int(self.e))))

    def key_id(self):
        return hashlib.sha1(_seq(_int(self.n), _int(self.e))).digest()

    def private_pem(self):
        der = _seq(_int(0), _int(self.n), _int(self.e), _int(self.d), _int(self.p), _int(self.q),
                   _int(self.dp), _int(self.dq), _int(self.qi))
        return _pem("RSA PRIVATE KEY", der)

    @classmethod
    def from_pem(cls, text):
        der = _unpem(text, "RSA PRIVATE KEY")
        _, body, _ = _read(der)
        vals, i = [], 0
        while i < len(body):
            _, v, i = _read(body, i)
            vals.append(int.from_bytes(v, "big"))
        _, n, e, d, p, q = vals[:6]
        return cls(n, e, d, p, q)

    def sign(self, data):
        """PKCS #1 v1.5 signature over SHA-256(data)."""
        k = (self.n.bit_length() + 7) // 8
        info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(data).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(info) - 3) + b"\x00" + info
        m = int.from_bytes(em, "big")
        m1, m2 = pow(m, self.dp, self.p), pow(m, self.dq, self.q)      # CRT: ~4x faster
        s = m2 + self.q * ((self.qi * (m1 - m2)) % self.p)
        assert pow(s, self.e, self.n) == m
        return s.to_bytes(k, "big")


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


# ---------------------------------------------------------------- X.509

_SHA256_RSA = _seq(_oid("1.2.840.113549.1.1.11"), _null())


def _name(cn):
    return _seq(_set(_seq(_oid("2.5.4.3"), _utf8(cn))))


def _ext(oid, value, critical=False):
    return _seq(_oid(oid), *([_bool(True)] if critical else []), _octets(value))


def _certificate(subject_cn, subject_key, issuer_cn, issuer_key, days, extensions):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    tbs = _seq(
        _explicit(0, _int(2)),                                   # v3
        _int(secrets.randbits(63) | 1),
        _SHA256_RSA,
        _name(issuer_cn),
        _seq(_time(now - datetime.timedelta(days=1)), _time(now + datetime.timedelta(days=days))),
        _name(subject_cn),
        subject_key.public_der(),
        _explicit(3, _seq(*extensions)),
    )
    return _seq(tbs, _SHA256_RSA, _bits(issuer_key.sign(tbs)))


def make_ca(name=CA_NAME):
    key = RSAKey.generate()
    der = _certificate(name, key, name, key, CA_DAYS, [
        _ext("2.5.29.19", _seq(_bool(True), _int(0)), critical=True),   # CA, no sub-CAs
        _ext("2.5.29.15", _bits(b"\x06", 1), critical=True),            # keyCertSign, cRLSign
        _ext("2.5.29.14", _octets(key.key_id())),
    ])
    return key, der


def make_server_cert(ca_key, ca_name, names, ips):
    key = RSAKey.generate()
    alt = b"".join([_tlv(0x82, n.encode("ascii")) for n in names] +
                   [_tlv(0x87, ipaddress.ip_address(ip).packed) for ip in ips])
    cn = names[0] if names else (ips[0] if ips else "localhost")
    der = _certificate(cn, key, ca_name, ca_key, CERT_DAYS, [
        _ext("2.5.29.19", _seq(), critical=True),                         # not a CA
        _ext("2.5.29.15", _bits(b"\xa0", 5), critical=True),             # digitalSignature, keyEncipherment
        _ext("2.5.29.37", _seq(_oid("1.3.6.1.5.5.7.3.1"))),              # serverAuth
        _ext("2.5.29.17", _seq(alt)),                                     # subjectAltName
        _ext("2.5.29.14", _octets(key.key_id())),
        _ext("2.5.29.35", _seq(_tlv(0x80, ca_key.key_id()))),             # authorityKeyIdentifier
    ])
    return key, der


# ---------------------------------------------------------------- PEM


def _pem(label, der):
    b64 = base64.b64encode(der).decode()
    return ("-----BEGIN %s-----\n" % label + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
            + "\n-----END %s-----\n" % label)


def _unpem(text, label):
    a = text.index("-----BEGIN %s-----" % label) + len("-----BEGIN %s-----" % label)
    b = text.index("-----END %s-----" % label)
    return base64.b64decode("".join(text[a:b].split()))


# ---------------------------------------------------------------- this computer


def local_names():
    """Names this computer answers to: localhost, its host name, its Bonjour .local name."""
    names = ["localhost"]
    host = socket.gethostname() or ""
    short = host.split(".")[0]
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                short = r.stdout.strip()
        except Exception:
            pass
    for n in (short, short + ".local" if short else "", host):
        n = n.strip().lower()
        if n and n not in names and all(c.isalnum() or c in "-." for c in n):
            names.append(n)
    return names


def local_ips():
    """127.0.0.1 and the addresses other devices reach this computer at."""
    ips = ["127.0.0.1"]
    cands = []
    try:                                     # the address of the route out (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            cands.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        cands += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    for ip in cands:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not (a.is_loopback or a.is_link_local or a.is_unspecified) and ip not in ips:
            ips.append(ip)
    return ips


# ---------------------------------------------------------------- the tls folder


def _write(path, text, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(text)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def vault_ca(tls_dir):
    """(key, name) of a CA this module made earlier, or None (none there, or someone else's)."""
    kp, cp = os.path.join(tls_dir, "vault-ca.key"), os.path.join(tls_dir, "vault-ca.crt")
    try:
        with open(kp, encoding="ascii") as f:
            key = RSAKey.from_pem(f.read())
        with open(cp, encoding="ascii") as f:
            der = _unpem(f.read(), "CERTIFICATE")
    except (OSError, ValueError, IndexError, UnicodeDecodeError):
        return None
    if key.public_der() not in der:          # the key must belong to that certificate
        return None
    return key, _ca_name(der)


def _ca_name(der):
    """The CA certificate's common name (the issuer name the server certificate must carry)."""
    _, cert, _ = _read(der)
    _, tbs, _ = _read(cert)
    i, fields = 0, []
    while i < len(tbs):
        t, v, i = _read(tbs, i)
        fields.append((t, v))
    subject = fields[5][1] if fields[0][0] == 0xA0 else fields[4][1]
    j = 0
    while j < len(subject):                  # SET { SEQ { OID, value } } ...
        _, rdn, j = _read(subject, j)
        _, atv, _ = _read(rdn)
        _, oid, k = _read(atv)
        _, val, _ = _read(atv, k)
        if oid == _oid("2.5.4.3")[2:]:
            return val.decode("utf-8", "replace")
    return CA_NAME


def make_all(tls_dir, names=None, ips=None, new_ca=False):
    """Write a server certificate (and a CA, when there is none of ours or new_ca) into tls_dir.
    Returns {"new_ca": bool, "names": [...], "ips": [...]}."""
    os.makedirs(tls_dir, exist_ok=True)
    try:
        os.chmod(tls_dir, 0o700)
    except OSError:
        pass
    names = names or local_names()
    ips = ips or local_ips()
    ca = None if new_ca else vault_ca(tls_dir)
    made_ca = ca is None
    if made_ca:
        host = next((n for n in names if n != "localhost"), "")
        ca_name = "%s (%s)" % (CA_NAME, host) if host else CA_NAME
        ca_key, ca_der = make_ca(ca_name)
        _write(os.path.join(tls_dir, "vault-ca.key"), ca_key.private_pem(), private=True)
        _write(os.path.join(tls_dir, "vault-ca.crt"), _pem("CERTIFICATE", ca_der))
        ca = (ca_key, ca_name)
    key, der = make_server_cert(ca[0], ca[1], names, ips)
    _write(os.path.join(tls_dir, "vault.key"), key.private_pem(), private=True)
    _write(os.path.join(tls_dir, "vault.crt"), _pem("CERTIFICATE", der))
    return {"new_ca": made_ca, "names": names, "ips": ips}
