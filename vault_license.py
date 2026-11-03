"""
vault_license.py — offline check of a Vault Pro key.

A key is "VAULT1-" + base64url(payload + signature). The payload is ASCII
"<tier>|<last valid day YYYY-MM-DD>|<random id>" and the signature is Ed25519 over it.
Checking needs only the public key below: Vault never phones home, and a key carries no
name or email address.

Ed25519 is the reference implementation from RFC 8032, section 6 (Simplified BSD
License), in plain Python because Vault uses the standard library only. It is slow
(tens of milliseconds) and not constant-time, which is fine for checking one key.
"""
import base64
import datetime
import hashlib

# Keys signed by any of these are accepted. First: issued by hand (seed kept offline).
# Second: the key server that hands keys to supporters after a Patreon / SubscribeStar login.
# Drop one here to revoke every key it ever signed.
PUBLIC_KEYS = (
    bytes.fromhex("399a7749f38bdad8efbef3216c867573c258de44a5bf67cd9532c15933753f7c"),
    bytes.fromhex("f4197a6ad5cd21957cdc2374c905b99668ce8d74c372a7685238818ece850959"),
)
PUBLIC_KEY = PUBLIC_KEYS[0]
FREE_LIMIT = 250          # movies the free version shows
PREFIX = "VAULT1-"

# ---------------------------------------------------------------- Ed25519 (RFC 8032)
_p = 2 ** 255 - 19
_d = -121665 * pow(121666, _p - 2, _p) % _p
_q = 2 ** 252 + 27742317777372353535851937790883648493
_SQRT_M1 = pow(2, (_p - 1) // 4, _p)


def _hq(s):
    return int.from_bytes(hashlib.sha512(s).digest(), "little") % _q


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return ((P[0] * Q[2] - Q[0] * P[2]) % _p == 0
            and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0)


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _SQRT_M1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * pow(5, _p - 2, _p) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zinv = pow(P[2], _p - 2, _p)
    x, y = P[0] * zinv % _p, P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def verify(public, msg, signature):
    """True if signature is a valid Ed25519 signature of msg under public (32 bytes)."""
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    R = _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = _hq(signature[:32] + public + msg)
    return _equal(_mul(s, _G), _add(R, _mul(h, A)))


# ---------------------------------------------------------------- keys
def decode(key, public=None):
    """Parse and verify a key. Returns {tier, expires, id} or raises ValueError."""
    key = "".join((key or "").split())
    if not key.startswith(PREFIX):
        raise ValueError("not a Vault key")
    body = key[len(PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception:
        raise ValueError("not a Vault key")
    if len(raw) <= 64:
        raise ValueError("not a Vault key")
    payload, sig = raw[:-64], raw[-64:]
    keys = PUBLIC_KEYS if public is None else ([public] if isinstance(public, bytes) else public)
    if not any(verify(k, payload, sig) for k in keys):
        raise ValueError("this key was not issued for Vault")
    try:
        tier, expires, kid = payload.decode("ascii").split("|")
        datetime.date.fromisoformat(expires)
    except Exception:
        raise ValueError("unreadable key")
    return {"tier": tier, "expires": expires, "id": kid}


_CACHE = {}


def status(key, today=None, public=None):
    """What a stored key unlocks today: {pro, state, expires, id, error}.
    state is one of free (no key), pro, expired, invalid."""
    key = "".join((key or "").split())
    if not key:
        return {"pro": False, "state": "free", "expires": "", "id": "", "error": ""}
    ck = (key, tuple(public) if isinstance(public, list) else public)
    if ck not in _CACHE:
        try:
            _CACHE[ck] = decode(key, public)
        except ValueError as e:
            _CACHE[ck] = str(e)
    info = _CACHE[ck]
    if isinstance(info, str):
        return {"pro": False, "state": "invalid", "expires": "", "id": "", "error": info}
    today = today or datetime.date.today()
    if info["tier"] != "pro":
        return {"pro": False, "state": "invalid", "expires": info["expires"], "id": info["id"],
                "error": "unknown key type"}
    if today > datetime.date.fromisoformat(info["expires"]):
        return {"pro": False, "state": "expired", "expires": info["expires"], "id": info["id"],
                "error": "expired on " + info["expires"]}
    return {"pro": True, "state": "pro", "expires": info["expires"], "id": info["id"],
            "error": ""}
