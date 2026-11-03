#!/usr/bin/env python3
"""
scan.py — universal per-code metadata scanner for the FC2 library.

Fills the DB from the shell-reachable sources, in priority order, WITHOUT
overwriting better/existing data (fill-if-empty unless force=True):

  title_en   <- 123av.com          (clean English title)
  title (JP) <- ffjav.com          (original Japanese title; also feeds tag derivation)
  cover_url  <- ffjav.com          (cover image, for wishlist/missing items)
  release_date <- ffjav / 123av
  tags       <- derived from the JP or EN title (genre keywords)

Never touched here (owned by richer sources / the user):
  actress, producer/studio, fav_count  -> bigboobs only
  duration, resolution                 -> local ffprobe (authoritative)
  anything the user edited by hand

Importable (serve.py uses scan_code / scan_library) and runnable standalone.
"""
import html as htmlmod
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_db():
    for p in (os.path.join(HERE, "data", "catalog.db"),
              os.path.join(HERE, "db", "catalog.db"),
              os.path.join(HERE, "webui", "data", "catalog.db")):
        if os.path.exists(p):
            return p
    return os.path.join(HERE, "db", "catalog.db")


DB = _find_db()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---- genre tag extraction (self-contained: Japanese + English keyword maps) ----
_JP = [
    (r"中出|中だ|膣内射精|種付|孕ま|妊娠", "Creampie"), (r"潮吹|潮まみ|イキ潮|大量潮", "Squirting"),
    (r"ハメ撮", "POV"), (r"素人", "Amateur"), (r"顔出", "Face Shown"),
    (r"フェラ|咥え|しゃぶ|イラマ|口内", "Blowjob"), (r"パイズリ", "Titfuck"), (r"騎乗位", "Cowgirl"),
    (r"爆乳|美巨乳|巨乳|神乳|[HGIJＨＧＩＪ]カップ", "Big Tits"),
    (r"貧乳|ちっぱい|微乳", "Small Tits"), (r"スレンダー|華奢|細身", "Slender"),
    (r"人妻|若妻|嫁|美人妻", "Married Woman"), (r"女子大生|女子大|大学生|ＪＤ|音大", "College Girl"),
    (r"メンエス|メンズエステ|エステ", "Massage"), (r"3P|３P|4P|４P|5P|５P|乱交|ハーレム|複数プレイ", "Group Sex"),
    (r"顔射|ぶっかけ|顔面", "Facial"), (r"ごっくん", "Swallow"), (r"アナル", "Anal"),
    (r"コスプレ|制服|メイド|ナース|バニー|セーラー", "Cosplay"), (r"妊婦", "Pregnant"),
    (r"黒人|異人種|白人", "Interracial"), (r"寝取|ネトリ|不倫|セフレ", "NTR/Cheating"),
    (r"手コキ|モモコキ", "Handjob"), (r"オナニー|自慰", "Masturbation"), (r"初撮|デビュー", "Debut"),
    (r"パイパン", "Shaved"), (r"電マ|バイブ|ディルド|ローター", "Toys"),
    (r"お嬢様|箱入り", "Ojousama"), (r"OL|事務員|受付|店員|看護|歯科|保育士", "Working Girl"),
    (r"ギャル", "Gal"), (r"露天|温泉|お風呂", "Bath"), (r"野外|露出", "Outdoor"), (r"無修正", "Uncensored"),
]
_EN = [
    (r"cream ?pie|cum (inside|in her|deep)|semen inside|nakadashi|creampie", "Creampie"),
    (r"squirt", "Squirting"), (r"\bpov\b|gonzo", "POV"), (r"amateur", "Amateur"),
    (r"face reveal|face shown|face out", "Face Shown"),
    (r"blow ?job|fellatio|deep ?throat|\boral\b", "Blowjob"),
    (r"tit ?job|paizuri|titty ?fuck", "Titfuck"), (r"cowgirl|cowgirl position|riding", "Cowgirl"),
    (r"big (tits|breasts|boobs)|busty|huge (tits|breasts|boobs)|[G-J][- ]?cup", "Big Tits"),
    (r"small (tits|breasts|boobs)|flat.?chest", "Small Tits"), (r"slender|slim|skinny", "Slender"),
    (r"\bwife\b|married woman|housewife", "Married Woman"),
    (r"college student|university student|schoolgirl|\bcoed\b|\bjd\b", "College Girl"),
    (r"\bmassage\b|esthetician|men'?s esthe", "Massage"),
    (r"threesome|foursome|\borgy\b|gang ?bang|group sex|\b[3-5]p\b", "Group Sex"),
    (r"facial|cum on (her )?face|bukkake", "Facial"), (r"swallow|gokkun", "Swallow"),
    (r"\banal\b", "Anal"), (r"cosplay|uniform|\bmaid\b|\bnurse\b|bunny|sailor", "Cosplay"),
    (r"pregnan", "Pregnant"), (r"black (man|guy|cock)|interracial|\bbbc\b", "Interracial"),
    (r"cheating|netorare|\bntr\b|\baffair\b|mistress|cuckold", "NTR/Cheating"),
    (r"hand ?job", "Handjob"), (r"masturbat", "Masturbation"),
    (r"first shoot|first time|\bdebut\b", "Debut"), (r"shaved|hairless|paipan", "Shaved"),
    (r"vibrator|dildo|sex toy", "Toys"), (r"\bgal\b|gyaru", "Gal"),
    (r"outdoor|\bpublic\b|outside", "Outdoor"), (r"hot spring|onsen|\bbath\b", "Bath"),
    (r"uncensored", "Uncensored"),
    (r"\btall\b", "Tall"), (r"\bpetite\b|\bmini(mum)?\b", "Petite"),
    (r"\bmature\b|\bmilf\b|cougar", "Mature"),
    (r"innocent|\bnaive\b|\bpure\b|wholesome|shy", "Innocent"),
    (r"\bvirgin\b|first (sexual )?experience", "Virgin"),
    (r"doggy|from behind|doggystyle", "Doggy Style"),
    (r"\b69\b|sixty[- ]?nine", "Sixty-Nine"),
    (r"foot ?job", "Footjob"), (r"cunnilingus|pussy[- ]licking", "Cunnilingus"),
    (r"\bbdsm\b|bondage|restrain|tied[- ]?up", "BDSM"),
    (r"\bidol\b|underground idol", "Idol"), (r"gravure", "Gravure"),
    (r"\bteacher\b", "Teacher"),
    (r"influencer|streamer|\bvtuber\b|tiktoker", "Influencer"),
    (r"\bleaked\b", "Leaked"), (r"hairy|unshaved|\bbush\b", "Hairy"),
    (r"fair[- ]?skinned|pale skin|white skin", "Fair Skin"),
    (r"\bchubby\b|\bbbw\b|\bplump\b", "Chubby"),
    (r"beautiful legs|long legs|nice legs", "Legs"),
    (r"pissing|urination|golden shower|omorashi", "Pissing"),
    (r"impregnat|breeding|seeding", "Creampie"),
    (r"office lady|\bol\b|receptionist|secretary", "Working Girl"),
    (r"pantyhose|stockings|fishnet|tights", "Pantyhose"),
]
_JP += [
    (r"長身|高身長", "Tall"), (r"小柄|ミニマム", "Petite"), (r"熟女", "Mature"),
    (r"純粋|無垢|うぶ|初々|清楚", "Innocent"), (r"処女|バージン", "Virgin"),
    (r"バック|後背位", "Doggy Style"), (r"シックスナイン", "Sixty-Nine"),
    (r"足コキ", "Footjob"), (r"クンニ", "Cunnilingus"), (r"緊縛|調教|拘束", "BDSM"),
    (r"アイドル", "Idol"), (r"グラビア|グラドル", "Gravure"), (r"女教師", "Teacher"),
    (r"配信者|インフルエンサー|ストリーマー", "Influencer"), (r"流出", "Leaked"),
    (r"剛毛|陰毛", "Hairy"), (r"色白|美白", "Fair Skin"),
    (r"ぽっちゃり|むっちり|マシュマロ", "Chubby"), (r"美脚", "Legs"),
    (r"放尿|お漏らし|おしっこ", "Pissing"), (r"孕ませ|種付", "Creampie"),
    (r"事務員|受付|秘書", "Working Girl"), (r"パンスト|ストッキング|網タイツ", "Pantyhose"),
]
_JP = [(re.compile(p), t) for p, t in _JP]
_EN = [(re.compile(p, re.I), t) for p, t in _EN]


def derive_tags(jp_title, en_title):
    """Genre tags from a Japanese title AND/OR an English title/description."""
    out, seen = [], set()
    for rx, t in _JP:
        if t not in seen and rx.search(jp_title or ""):
            seen.add(t); out.append(t)
    for rx, t in _EN:
        if t not in seen and rx.search(en_title or ""):
            seen.add(t); out.append(t)
    return out


def tidy_title(t):
    """Strip promo/pricing/bracket noise from a scraped title, leaving clean prose.
    (Tags are harvested from the RAW title BEFORE this runs — see scan_code.)"""
    if not t:
        return t
    # leftover code prefix (any casing/spacing), belt-and-suspenders
    t = re.sub(r"^\s*FC2[\s\-]?PPV[\s\-]?\d+\s*[—–\-:]*\s*", "", t, flags=re.I)
    # promotional / sale / pricing noise
    t = re.sub(r"\d[\d,]*\s*(pt|points?)\s*[→\-—–]+\s*\d[\d,]*\s*(pt|points?)", " ", t, flags=re.I)
    t = re.sub(r"\b\d[\d,]*\s*(pt|points?)\b", " ", t, flags=re.I)                # 980pt / 1980 points
    t = re.sub(r"\buntil\s*\d{1,2}\s*[/月]\s*\d{1,2}\b", " ", t, flags=re.I)      # until 8/17
    t = re.sub(r"\bfor\s*\d+\s*days?\b", " ", t, flags=re.I)                      # for 3 days
    t = re.sub(r"(limited sale|limited time|limited price|discount|discontinued|for a limited|\bsale\b)",
               " ", t, flags=re.I)
    # bracketed segments (their useful words were already turned into tags)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"【[^】]*】", " ", t)
    t = re.sub(r"《[^》]*》", " ", t)
    t = re.sub(r"（[^）]*）", " ", t)
    # trailing "*Uncensored/facial" / "※..." promo notes (cut to end of line)
    t = re.sub(r"[\*※].*$", "", t)
    # remove any remaining stray brackets, then collapse runs of dashes/marks/punctuation
    t = re.sub(r"[\[\]【】《》（）]", " ", t)
    t = re.sub(r"(?:\s*[-–—~!！?？/／・]\s*){2,}", " ", t)
    # tidy whitespace + dangling punctuation/dashes at the edges
    t = re.sub(r"\s+", " ", t).strip()
    t = t.strip(" -–—~・,.。/、！!♡♥").strip()
    return t


_CUP_EN = re.compile(r"\b([A-K])[\s\-]?cup\b", re.I)
_CUP_JP = re.compile(r"([A-KＡ-Ｋ])\s*カップ")
_AGE_EN = re.compile(r"\b(1[89]|[2-4]\d)\s*[-\s]?(?:year[-\s]?old|years?[-\s]?old|\byo)\b", re.I)
_AGE_PAREN = re.compile(r"[（(]\s*(1[89]|[2-4]\d)\s*[)）]")
_AGE_JP = re.compile(r"(1[89]|[2-4]\d)\s*歳")


def extract_cup(jp, en):
    for txt, rx in ((en, _CUP_EN), (jp, _CUP_JP)):
        m = rx.search(txt or "")
        if m:
            c = m.group(1)
            return chr(ord("A") + (ord(c) - ord("Ａ"))) if "Ａ" <= c <= "Ｋ" else c.upper()
    return None


def extract_age(jp, en):
    m = _AGE_EN.search(en or "")
    if m:
        return int(m.group(1))
    both = (jp or "") + " " + (en or "")
    for rx in (_AGE_JP, _AGE_PAREN):
        m = rx.search(both)
        if m:
            a = int(m.group(1))
            if 18 <= a <= 45:
                return a
    return None


def _fill_actress_from_title(con, code, jp, en):
    """If the movie has an actress with no cup/age, fill it from the title."""
    row = con.execute("SELECT actress_id FROM works WHERE code=?", (code,)).fetchone()
    aid = row[0] if row else None
    if not aid:
        return {}
    cur = con.execute("SELECT cup, age FROM actresses WHERE id=?", (aid,)).fetchone()
    if not cur:
        return {}
    cup, age = cur
    out = {}
    if not cup:
        c = extract_cup(jp, en)
        if c:
            con.execute("UPDATE actresses SET cup=? WHERE id=? AND (cup IS NULL OR cup='')", (c, aid))
            out["cup"] = c
    if age is None:
        a = extract_age(jp, en)
        if a:
            con.execute("UPDATE actresses SET age=? WHERE id=? AND age IS NULL", (a, aid))
            out["age"] = a
    return out


# ---- address-family-pinned openers ----------------------------------------
# Cloudflare binds a cf_clearance cookie to the exact IP that solved its check.
# On a dual-stack connection the browser and Python may exit over different
# address families (and IPv6 privacy addresses rotate), so the cookie is bound
# to one family but the request goes out the other -> CF re-challenges
# (403 cf-mitigated=challenge / a "Just a moment" page) even with a fresh,
# correct cookie + user-agent. We can't know which family the browser used and
# it isn't stable, so for Cloudflare-gated fetches we try both and keep the one
# the cookie validates against. See _get_cf.
class _FamilyHTTPSConnection(http.client.HTTPSConnection):
    _family = 0                       # AF_UNSPEC (default resolver)
    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port or 443,
                                   self._family, socket.SOCK_STREAM)
        af, socktype, proto, _cn, sa = infos[0]
        sock = socket.socket(af, socktype, proto)
        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(self.timeout)
        if self.source_address:
            sock.bind(self.source_address)
        sock.connect(sa)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _V4HTTPSConnection(_FamilyHTTPSConnection): _family = socket.AF_INET
class _V6HTTPSConnection(_FamilyHTTPSConnection): _family = socket.AF_INET6


def _family_opener(conn_cls):
    class _H(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(conn_cls, req)
    return urllib.request.build_opener(_H)


_V4_OPENER = _family_opener(_V4HTTPSConnection)
_V6_OPENER = _family_opener(_V6HTTPSConnection)


def _get(url, timeout=20, headers=None):
    try:
        h = {"User-Agent": UA}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return None


def _is_cf_challenge(html):
    """True if the response is a Cloudflare interstitial rather than real content."""
    if not html:
        return True
    low = html[:4000].lower()
    return ("just a moment" in low or "challenge-platform" in low
            or "cf-mitigated" in low or "attention required" in low
            or "cf_chl_" in low)


def _is_age_gate(html):
    """True if fc2ppv-db rendered its 18+ confirmation instead of a video page."""
    return 'property="og:title" content="Age Verification' in (html or "")[:8000]


def _get_cf(url, timeout=25, headers=None):
    """GET a Cloudflare-gated URL. Returns (html, status).

    cf_clearance is bound to the IP family the browser solved the check on, which varies
    on dual-stack, so try IPv4 then IPv6 and return whichever the cookie validates against
    (None if both fail). `status` is the last HTTP code seen, so the caller can tell a
    plain 404 -- a code fc2ppv-db simply does not have -- from a refusal."""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    status = None
    for opener in (_V4_OPENER, _V6_OPENER):
        try:
            req = urllib.request.Request(url, headers=h)
            with opener.open(req, timeout=timeout) as r:
                status = r.status
                if r.status == 200:
                    html = r.read().decode("utf-8", "ignore")
                    if not _is_cf_challenge(html):
                        return html, status
        except urllib.error.HTTPError as e:
            status = e.code
            continue
        except Exception:
            continue
    return None, status


def _digits(code):
    m = re.search(r"(\d{4,})", code or "")
    return m.group(1) if m else ""


# ---- 123av: English title -------------------------------------------------
def fetch_123av_title(num):
    html = _get(f"https://123av.com/en/v/fc2-ppv-{num}")
    if not html:
        return None
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if not m:
        return None
    t = htmlmod.unescape(htmlmod.unescape(m.group(1)))          # double-encoded
    t = re.sub(r"^\s*FC2[\s\-]?PPV[\s\-]?\d+\s*[—–\-:]*\s*", "", t, flags=re.I)
    t = re.sub(r"\s*[—–\-]\s*123AV\s*$", "", t, flags=re.I).strip()
    if not t or "not found" in t.lower():
        return None
    return t[:400]


# ---- ffjav: JP title + cover + date ---------------------------------------
def fetch_ffjav(code):
    html = _get("https://ffjav.com/?s=" + code)
    if not html:
        return {}
    num = _digits(code)
    # A search page lists every hit in its own card. Reading each field with a page-wide
    # regex mixed results together: the title was code-checked but the DATE was simply the
    # first YYYY-MM-DD on the page, and it was returned even when the title had been
    # rejected as unrelated. Find the one card that is this code and read only that.
    blocks = re.split(r'<div class="card mb-3"', html)[1:]
    if not blocks:                      # markup changed: fall back to the whole page,
        blocks = [html] if num and num in html else []   # but still require the code
    block = next((b for b in blocks if num and num in b), None)
    if block is None:
        return {}
    cov = re.search(r'<img class="image" src="([^"]+)"', block)
    cov = cov.group(1) if cov else None
    # only trust a result whose cover filename actually matches this code
    if cov and num not in cov:
        cov = None
    tt = re.search(r'<h5 class="title[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>', block, re.S)
    jp = None
    if tt:
        jp = htmlmod.unescape(re.sub(r"<[^>]+>", "", tt.group(1)).strip())
        jp = re.sub(r"^\s*FC2[\s\-]?PPV[\s\-]?\d+\s*", "", jp, flags=re.I).strip()
        if num not in (tt.group(1) + (cov or "")) and not cov:
            jp = None                                          # unrelated result
    d = re.search(r"(\d{4}-\d{2}-\d{2})", block)
    return {"title_jp": jp or None, "cover_url": cov,
            "release_date": d.group(1) if (d and jp) else None}


# ---- actress profiles: javdatabase (body info) + jav.guru (FC2 code list) --
# These enrich an actress's physical profile AND, crucially, list her works — so we can
# VERIFY the identification by checking whether any listed FC2 code is one we own. Two
# people share a name often enough that body data must never be trusted without that
# overlap check. See enrich_actress().

def _slug_variants(name):
    """Hyphen-slug candidates for a romaji name, in BOTH word orders (sites disagree:
    javdatabase uses given-family 'sora-mikumo', jav.guru family-given 'mikumo-sora')."""
    if not name:
        return []
    base = re.sub(r"[^a-z0-9\s]", "", name.lower()).strip()
    toks = base.split()
    if not toks:
        return []
    cands = ["-".join(toks)]
    if len(toks) > 1:
        cands.append("-".join(reversed(toks)))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _age_from_dob(dob):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", dob or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    n = time.localtime()
    age = n.tm_year - y - ((n.tm_mon, n.tm_mday) < (mo, d))
    return age if 15 < age < 80 else None


# label and value sit in separate tags ("Age:</span> <span>26"), so the gap between
# them may contain any run of tags / whitespace / entities.
_GAP = r"(?:\s|&nbsp;|<[^>]+>)*"


def _jd(h, label, valpat):
    m = re.search(re.escape(label) + r"\s*:?" + _GAP + "(" + valpat + ")", h, re.I)
    return m.group(1).strip() if m else None


def fetch_javdatabase(name):
    """Return {url, name_en, name_jp, dob, age, height, cup, bust, waist, hip} or {}."""
    for slug in _slug_variants(name):
        url = f"https://www.javdatabase.com/idols/{slug}/"
        h = _get(url)
        if not h or "idols" not in h.lower():
            continue
        prof = {"url": url}
        title = re.search(r"<title>\s*([^<|]+?)\s*-\s*JAV\b", h, re.I) \
            or re.search(r">\s*([A-Z][A-Za-z]+ [A-Z][A-Za-z]+)\s*-\s*JAV Profile", h)
        if title:
            prof["name_en"] = htmlmod.unescape(title.group(1)).strip()
        jp = _jd(h, "JP", r"[぀-ヿ一-鿿ａ-ﾟ]{2,20}")
        if jp:
            prof["name_jp"] = jp
        dob = _jd(h, "DOB", r"\d{4}-\d{2}-\d{2}")
        if dob:
            prof["dob"] = dob
            prof["age"] = _age_from_dob(dob)
        cup = _jd(h, "Cup", r"[A-K]")
        if cup:
            prof["cup"] = cup
        hgt = _jd(h, "Height", r"\d{3}")
        if hgt:
            prof["height"] = int(hgt)
        meas = re.search(r"Measurements\s*:?" + _GAP + r"(\d{2,3})-(\d{2,3})-(\d{2,3})", h, re.I)
        if meas:
            prof["bust"], prof["waist"], prof["hip"] = map(int, meas.groups())
        # only worth returning if we actually recognized the profile
        if len(prof) > 1:
            return prof
    return {}


def fetch_javguru_codes(name):
    """Return (url, set_of_FC2_codes) for an actress on jav.guru, UNIONing every slug
    variant (the site indexes the same person under both word orders, each with a partial
    filmography) so verification sees the fullest possible code list."""
    codes, urls = set(), []
    for slug in _slug_variants(name):
        url = f"https://jav.guru/actress/{slug}/"
        h = _get(url)
        if not h:
            continue
        found = set("FC2-PPV-" + n for n in re.findall(r"FC2-?PPV-?(\d{6,})", h, re.I))
        if found:
            codes |= found
            urls.append(url)
    return (urls[0] if urls else None), codes


# ---- fc2ppv-db.com: the richest source (JP title, actress+face+aliases, tags, date) ----
# Cloudflare-gated. Two transports, chosen in Settings:
#   * a session cookie + user-agent the user copied from a browser on this machine
#   * FlareSolverr: Vault sends each page URL to that browser and parses the HTML it returns.
#     The clearance never comes back to urllib, so it stays bound to the resolver's browser.
# The parser itself is pure — feed it the page HTML from either transport.
FC2DB = "https://fc2ppv-db.com"
_CDN = "https://d39jz7pbpqkw9s.cloudfront.net"

# --- kana -> romaji (Hepburn). Deterministic, offline. Only used to give an actress an
#     English display name when she has none and her name/alias is pure kana. ---
_YOON = {"きゃ": "kya", "きゅ": "kyu", "きょ": "kyo", "しゃ": "sha", "しゅ": "shu",
         "しょ": "sho", "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho", "にゃ": "nya",
         "にゅ": "nyu", "にょ": "nyo", "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo",
         "みゃ": "mya", "みゅ": "myu", "みょ": "myo", "りゃ": "rya", "りゅ": "ryu",
         "りょ": "ryo", "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo", "じゃ": "ja",
         "じゅ": "ju", "じょ": "jo", "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
         "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo"}
_KANA = {"あ": "a", "い": "i", "う": "u", "え": "e", "お": "o", "か": "ka", "き": "ki",
         "く": "ku", "け": "ke", "こ": "ko", "が": "ga", "ぎ": "gi", "ぐ": "gu",
         "げ": "ge", "ご": "go", "さ": "sa", "し": "shi", "す": "su", "せ": "se",
         "そ": "so", "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
         "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to", "だ": "da",
         "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do", "な": "na", "に": "ni",
         "ぬ": "nu", "ね": "ne", "の": "no", "は": "ha", "ひ": "hi", "ふ": "fu",
         "へ": "he", "ほ": "ho", "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be",
         "ぼ": "bo", "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
         "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo", "や": "ya",
         "ゆ": "yu", "よ": "yo", "ら": "ra", "り": "ri", "る": "ru", "れ": "re",
         "ろ": "ro", "わ": "wa", "を": "o", "ん": "n", "ぁ": "a", "ぃ": "i",
         "ぅ": "u", "ぇ": "e", "ぉ": "o", "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ー": ""}


def romaji(s):
    """Romanize a PURE-kana string (hiragana/katakana). Returns Title-cased romaji, or None
    if the string has any non-kana char (kanji readings are ambiguous — we don't guess)."""
    if not s:
        return None
    kana = "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s.strip())
    out, i, sok = [], 0, False
    while i < len(kana):
        ch = kana[i]
        if kana[i:i + 2] in _YOON:
            r = _YOON[kana[i:i + 2]]
            i += 2
        elif ch == "っ":
            sok = True
            i += 1
            continue
        elif ch in _KANA:
            r = _KANA[ch]
            i += 1
        elif ch in "　 ":
            out.append(" ")
            i += 1
            continue
        else:
            return None                     # kanji / latin / symbol -> can't romanize a name
        if sok and r:
            r = r[0] + r
            sok = False
        out.append(r)
    res = " ".join(w for w in "".join(out).split() if w).strip()
    return res.title() or None


def set_english_from_kana(con, aid):
    """If actress `aid` has no English name, derive one by romanizing a pure-kana name/alias."""
    row = con.execute("SELECT name_en,name_jp FROM actresses WHERE id=?", (aid,)).fetchone()
    if not row or (row[0] or "").strip():
        return None
    cands = [row[1]] + [r[0] for r in con.execute(
        "SELECT alias FROM aliases WHERE actress_id=?", (aid,))]
    for c in cands:
        en = romaji(c)
        if en:
            try:
                con.execute("UPDATE actresses SET name_en=? WHERE id=?", (en, aid))
                return en
            except sqlite3.IntegrityError:  # that romaji already belongs to someone
                return None
    return None


def _jp_date(html):
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", html)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r'"releaseDate":"\$D(\d{4}-\d{2}-\d{2})', html)
    return m.group(1) if m else None


# fc2ppv-db answers an unknown id with HTTP **200** and its bare site shell, not a 404: the
# og:title is the site name and the og:image is one shared placeholder. The parser used to read
# that as a movie called "FC2PPV Database" with a generic cover, scan_fc2db counted it as a hit,
# and 52 catalog rows ended up holding the site's own chrome as their Japanese title.
_SITE_TITLES = {"fc2ppv database", "fc2ppv-db", "fc2ppv db"}
_GENERIC_COVER = "opengraph-image"


def parse_fc2ppvdb_video(html):
    """Parse an fc2ppv-db.com /videos/<id> page into a structured dict. Pure — no network.

    Returns {} for the site's not-found shell, so the caller records "absent" rather than
    storing the placeholder as metadata."""
    if not html or "fc2ppv" not in html.lower() or _is_age_gate(html):
        return {}
    d = {}
    m = re.search(r'/videos/(\d{5,})"', html)
    if m:
        d["code"] = "FC2-PPV-" + m.group(1)
    m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    if m:
        t = htmlmod.unescape(m.group(1))
        t = re.sub(r"^\s*FC2-?PPV-?\d+\s*", "", t).strip()
        if t.lower() in _SITE_TITLES:       # the shell, not a movie
            t = ""
        d["title_jp"] = t or None
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m and _GENERIC_COVER not in m.group(1):
        d["cover_url"] = m.group(1)
    d["release_date"] = _jp_date(html)
    # Mosaic comes from the page data only. Testing for the WORD "Uncensored" (or the token
    # "NONE") anywhere in the HTML matched chrome, tag lists and i18n strings, so an ordinary
    # censored page could be stored as uncensored. No mosaicStatus -> say nothing.
    m = re.search(r'"mosaicStatus":"(FULL|THIN|NONE)"', html)
    if m:
        d["censorship"] = "uncensored" if m.group(1) == "NONE" else "censored"
    # actresses (from the DOM chips: uuid in href, face img, name, alias in parens)
    actresses = []
    for a in re.finditer(
            r'/(?:en|ja)/actresses/([0-9a-f-]{36})"(.*?)</a>', html, re.S):
        uuid, inner = a.group(1), a.group(2)
        face = re.search(r'src="([^"]*faces/[^"]+)"', inner)
        name = re.search(r'<img alt="([^"]*)"', inner) \
            or re.search(r'font-medium">([^<]+)<', inner)
        alias = re.search(r'text-muted-foreground">\((.*?)\)</span>', inner)
        aliases = []
        if alias:
            for al in re.split(r"[,、]", re.sub(r"<!--.*?-->", "", alias.group(1))):
                al = htmlmod.unescape(al).strip()
                if al:
                    aliases.append(al)
        actresses.append({
            "uuid": uuid,
            "name": htmlmod.unescape(name.group(1)).strip() if name else None,
            "face": (face.group(1) if face else _CDN + "/faces/actress_%s.jpg" % uuid),
            "aliases": aliases,
        })
    # de-dup by uuid (the same actress can appear in a blurred + main img block)
    seen, uniq = set(), []
    for a in actresses:
        if a["uuid"] not in seen and a.get("name"):
            seen.add(a["uuid"])
            uniq.append(a)
    d["actresses"] = uniq
    # tags (Japanese) from the tag links
    tags = []
    for tm in re.finditer(r'href="/(?:en|ja)/videos\?tags=[^"]+"[^>]*>([^<]{1,30})</a>', html):
        tg = htmlmod.unescape(tm.group(1)).strip()
        if tg and tg not in tags:
            tags.append(tg)
    d["tags"] = tags
    # mosaicStatus lives in a real video page's embedded JSON; the not-found shell has none,
    # so it counts as evidence the page was real even when everything else is empty.
    if not (d.get("title_jp") or uniq or d.get("release_date")
            or d.get("cover_url") or d.get("censorship")):
        return {}                     # the not-found shell: nothing worth writing
    # seller / studio
    sm = re.search(r'/(?:en|ja)/sellers/[^"]+"(.*?)</a>', html, re.S)
    if sm:
        nm = re.search(r'font-medium[^>]*>([^<]+)<', sm.group(1))
        if nm:
            d["producer"] = htmlmod.unescape(nm.group(1)).strip()
    return d


# One FlareSolverr session is one Chromium. Keep Vault's requests serialized and
# on a session name nothing else uses, so a second scan can't share the browser.
_SOLVER_LOCK = threading.Lock()
_SOLVER_SESSION = "vault-fc2db"
# fc2db.net gets its OWN browser. One session name is one Chromium, and every fc2ppv-db scan
# ends with solver_release, which destroys that session — a second site sharing the name
# would be kicked off its browser mid-scan.
_FC2DBNET_SESSION = "vault-fc2dbnet"
# Endpoint whose session we already created. sessions.create on every page is a wasted
# round trip to the resolver; we only (re)create after a failure cleared the session.
# "endpoint" is the default session's (kept for the tests that set it); others live in
# "sessions", keyed by name.
_SOLVER_LIVE = {"endpoint": None, "sessions": {}}


def _live(session):
    if session == _SOLVER_SESSION:
        return _SOLVER_LIVE["endpoint"]
    return _SOLVER_LIVE.setdefault("sessions", {}).get(session)


def _set_live(session, endpoint):
    if session == _SOLVER_SESSION:
        _SOLVER_LIVE["endpoint"] = endpoint
    else:
        _SOLVER_LIVE.setdefault("sessions", {})[session] = endpoint


def _solver_endpoint(base):
    """Accept host:port, http://host:8191, or http://host:8191/v1."""
    b = (base or "").strip().rstrip("/")
    if b and "://" not in b:
        b = "http://" + b
    if not b.startswith(("http://", "https://")):
        return ""
    if not b.endswith("/v1"):
        b += "/v1"
    return b


def _solver_post(endpoint, payload, timeout):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    return json.loads(raw.decode("utf-8", "ignore"))


def solver_ready(base, timeout=4):
    """True when the resolver answers. Does not solve a challenge."""
    endpoint = _solver_endpoint(base)
    if not endpoint:
        return False
    try:
        d = _solver_post(endpoint, {"cmd": "sessions.list"}, timeout=timeout)
        return d.get("status") == "ok"
    except Exception:
        return False


def _solver_ensure(endpoint, session=_SOLVER_SESSION):
    if _live(session) == endpoint:
        return True, ""
    # a freshly started resolver has to launch Chromium first; 30 s was not always enough
    d = _solver_post(endpoint, {"cmd": "sessions.create", "session": session}, timeout=90)
    if d.get("status") == "ok":
        _set_live(session, endpoint)
        return True, ""
    msg = d.get("message") or "could not open a FlareSolverr session"
    if "exist" in msg.lower():
        _set_live(session, endpoint)
        return True, ""
    return False, msg


def _solver_destroy(endpoint, session=_SOLVER_SESSION):
    _set_live(session, None)
    try:
        _solver_post(endpoint, {"cmd": "sessions.destroy", "session": session}, timeout=15)
    except Exception:
        pass


def solver_release(base, session=_SOLVER_SESSION):
    """Hand THIS site's Chromium back after a scan; the next run opens a fresh one."""
    endpoint = _solver_endpoint(base)
    if endpoint:
        with _SOLVER_LOCK:
            _solver_destroy(endpoint, session)


def solver_get(base, url, session=_SOLVER_SESSION):
    """Load `url` inside the resolver's browser. Returns (html, error, status).

    `status` is the HTTP code the resolver's browser got. A 404 means the site does not
    have that page; it is NOT a failed Cloudflare check, so the caller must not treat it
    as one -- doing that threw away a working session and aborted the scan after three
    codes fc2ppv-db simply did not carry."""
    endpoint = _solver_endpoint(base)
    if not endpoint:
        return None, "no FlareSolverr URL", None
    last = "FlareSolverr request failed"
    with _SOLVER_LOCK:
        for _attempt in (1, 2):
            try:
                ok, msg = _solver_ensure(endpoint, session)
            except Exception:
                # A cold FlareSolverr can take longer than the create timeout to spawn its
                # first Chromium. The resolver finishes anyway, so the SECOND attempt finds
                # the session already there -- which is why pressing "Test connection" first
                # appeared to "make the scan work". Retry here instead of making the user
                # do it by hand.
                last = "FlareSolverr did not answer in time (still starting?)"
                _SOLVER_LIVE["endpoint"] = None
                continue
            if not ok:
                return None, msg, None
            try:
                payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": 60000,
                    "session": session,
                }
                # The site's 18+ button only sets this cookie. Sending it with the
                # request keeps the confirmation inside the same browser that
                # cleared Cloudflare. path=/ so it covers every video, not one URL.
                if "fc2ppv-db.com" in url:
                    payload["cookies"] = [{
                        "name": "age-verified",
                        "value": "true",
                        "domain": "fc2ppv-db.com",
                        "path": "/",
                    }]
                elif "fc2db.net" in url:
                    # a fixed flag the 18+ button sets — not a personal session. Leading dot
                    # as the browser stores it, so img.fc2db.net is covered too.
                    payload["cookies"] = [{
                        "name": "mdb_age_gate_confirmed",
                        "value": "1",
                        "domain": ".fc2db.net",
                        "path": "/",
                    }]
                d = _solver_post(endpoint, payload, timeout=90)
            except Exception:
                last = "FlareSolverr unreachable"
                _solver_destroy(endpoint, session)
                continue
            sol = d.get("solution") or {}
            html = sol.get("response") or ""
            code = sol.get("status")
            if (d.get("status") == "ok" and html
                    and code in (200, None)
                    and not _is_cf_challenge(html)):
                if _is_age_gate(html):
                    # THREE values. Returning two raised ValueError in fetch_fc2ppvdb before
                    # _error could be set, so the 18+ wall never tripped the abort and a scan
                    # walked the whole library making futile round trips, reporting nothing.
                    return (None,
                            "FlareSolverr cleared Cloudflare, but fc2ppv-db is still "
                            "showing its 18+ confirmation — the age-verified cookie was "
                            "not accepted (the site may have renamed it).",
                            code)
                return html, "", code
            # The browser got through and the site answered "no such page". Keep the
            # session -- re-solving the check for every unknown code is what made a
            # perfectly healthy resolver look broken.
            if d.get("status") == "ok" and code in (404, 410) and not _is_cf_challenge(html):
                return None, "", code
            last = d.get("message") or "Cloudflare check was not solved"
            _solver_destroy(endpoint, session)
        return None, last, None


def fetch_fc2ppvdb(code, cookie=None, ua=None, solver_url=None):
    """Fetch + parse one video page.

    With `solver_url`, the resolver's browser loads the page and Vault parses the HTML.
    Otherwise `cookie` is the browser session string (cf_clearance + auth) the user
    pasted; without it Cloudflare returns a challenge (empty parse)."""
    num = _digits(code)
    if not num:
        return {}
    if solver_url:
        html, err, status = solver_get(solver_url, f"{FC2DB}/en/videos/{num}")
        if not html:
            if status in (404, 410):
                return {"_missing": True}
            return {"_error": err or "FlareSolverr could not fetch the page"}
        return parse_fc2ppvdb_video(html)
    headers = {}
    if ua:
        headers["User-Agent"] = ua
    if cookie:
        # The 18+ button only sets this cookie. The solver path already sends it; without it
        # here, every page came back as the age gate, parsed to {}, and counted as a normal
        # miss -- so a cookie-mode scan walked the whole library recording nothing.
        c = cookie.strip()
        if "age-verified" not in c:
            c = (c.rstrip("; ") + "; age-verified=true") if c else "age-verified=true"
        headers["Cookie"] = c
    # fc2ppv-db is Cloudflare-gated; _get_cf tries both IP families so the
    # cf_clearance cookie validates regardless of which one the browser used.
    html, status = _get_cf(f"{FC2DB}/en/videos/{num}", headers=headers, timeout=25)
    if not html:
        if status in (404, 410):
            return {"_missing": True}
        return {"_error": "no response from fc2ppv-db (network, or Cloudflare refused)"}
    if _is_cf_challenge(html):
        return {"_error": "Cloudflare challenge - the saved session was rejected"}
    if _is_age_gate(html):
        return {"_error": "fc2ppv-db returned its 18+ page - the age-verified cookie was "
                          "not accepted (the site may have renamed it)"}
    return parse_fc2ppvdb_video(html)


# ---- fc2db.net: cover, JP title, date, seller and — often — the cast -------
# A different site from fc2ppv-db.com, and the one that fills its holes: FC2-PPV-4988506,
# which fc2ppv-db lists with "no information" for the cast, has a named performer here.
# Plain urllib gets the article (Cloudflare fronts it but does not challenge it); FlareSolverr
# is only the fallback when a challenge or the 18+ wall comes back instead.
FC2DBNET = "https://fc2db.net"
_FC2DBNET_COOKIE = "mdb_age_gate_confirmed=1"   # the 18+ button's fixed flag, not a session


def _fc2dbnet_videoobject(html):
    """The page's JSON-LD VideoObject, or {}. This block is the stable record."""
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "",
                          re.S):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        items = d.get("@graph", [d]) if isinstance(d, dict) else d
        for it in items if isinstance(items, list) else [items]:
            if isinstance(it, dict) and it.get("@type") == "VideoObject":
                return it
    return {}


def _is_fc2dbnet_age_wall(html):
    """The 18+ confirmation a BROWSER can get instead of the article."""
    h = html or ""
    return not _fc2dbnet_videoobject(h) and ("18歳以上です" in h or "mdb_age_gate" in h)


def _is_fc2dbnet_challenge(html):
    """A Cloudflare interstitial instead of the article.

    NOT "the page mentions Turnstile": every real work page carries a Turnstile widget on its
    COMMENT form (cf-turnstile-comments, ~50 KB in) plus a challenge-platform script. Treating
    those as a challenge would reject every page the site has. A real page always has its
    VideoObject; an interstitial never does."""
    if _fc2dbnet_videoobject(html):
        return False
    return _is_cf_challenge(html)


def _hms(v):
    m = re.match(r"^\s*(\d+):(\d{2}):(\d{2})\s*$", v or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def parse_fc2dbnet_work(html):
    """Parse a fc2db.net /work/<digits>/ page. Pure — no network.

    JSON-LD first; the visible HTML only for what JSON-LD does not carry (the title, which is
    the page heading — og:title is just "FC2PPV-<digits>" — and the /work-tags/ links).
    The cover is thumbnailUrl and nothing else: the recommendation carousel below reuses the
    same wp-post-image class for OTHER movies, so "any image on the page" attaches the wrong
    thumbnail. Returns {} when nothing identifying is present (the template is fc2ppv-db's
    not-found shell, which became 52 titles reading "FC2PPV Database")."""
    if not html or _is_fc2dbnet_age_wall(html) or _is_fc2dbnet_challenge(html):
        return {}
    vo = _fc2dbnet_videoobject(html)
    d = {}
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if h1:
        t = htmlmod.unescape(re.sub(r"<[^>]+>", "", h1.group(1))).strip()
        t = re.sub(r"^\[?\s*FC2[-\s]?PPV[-\s]?\d+\s*\]?\s*", "", t, flags=re.I).strip()
        d["title_jp"] = t or None
    if vo:
        thumb = vo.get("thumbnailUrl")
        if isinstance(thumb, list):
            thumb = thumb[0] if thumb else None
        d["cover_url"] = thumb or None
        up = (vo.get("uploadDate") or "")[:10]
        d["release_date"] = up if re.match(r"^\d{4}-\d{2}-\d{2}$", up) else None
        d["duration"] = _hms(vo.get("duration"))
        actors = vo.get("actor") or []
        if isinstance(actors, dict):
            actors = [actors]
        d["actresses"] = [{"name": (a.get("name") or "").strip(), "url": a.get("url") or ""}
                          for a in actors if isinstance(a, dict) and (a.get("name") or "").strip()]
        pub = vo.get("publisher") or {}
        if isinstance(pub, dict) and "/seller/" in (pub.get("url") or ""):
            d["producer"] = (pub.get("name") or "").strip() or None
    else:
        d["actresses"] = []
    tags = []
    for tm in re.finditer(r'href="(?:https?://fc2db\.net)?/work-tags/[^"]*"[^>]*>([^<]{1,30})</a>',
                          html):
        tg = htmlmod.unescape(tm.group(1)).strip()
        if tg and tg not in tags:
            tags.append(tg)
    d["tags"] = tags
    if not (d.get("title_jp") or d.get("actresses") or d.get("release_date")
            or d.get("cover_url")):
        return {}
    return d


def _get_status(url, headers, timeout=25):
    """(html, status) for a plain GET; status is None on a network failure."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "ignore"), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, None


def fetch_fc2dbnet(code, solver_url=None):
    """One work page -> parsed dict, {"_missing": True} on a real 404/410, or {"_error": ...}.

    urllib first, like 123av/ffjav. Only a challenge or an age wall goes to the resolver,
    and then on its own browser session so it cannot evict an fc2ppv-db scan's."""
    num = _digits(code)
    if not num:
        return {}
    url = "%s/work/%s/" % (FC2DBNET, num)
    html, status = _get_status(url, {"User-Agent": UA, "Cookie": _FC2DBNET_COOKIE})
    if status in (404, 410):
        return {"_missing": True}
    blocked = (html is None or _is_fc2dbnet_challenge(html) or _is_fc2dbnet_age_wall(html))
    if blocked and solver_url:
        html, err, status = solver_get(solver_url, url, session=_FC2DBNET_SESSION)
        if status in (404, 410):
            return {"_missing": True}
        if not html:
            return {"_error": err or "FlareSolverr could not fetch the fc2db.net page"}
        blocked = _is_fc2dbnet_challenge(html) or _is_fc2dbnet_age_wall(html)
    if blocked:
        if html is None:
            return {"_error": "no response from fc2db.net (network, status %s)" % status}
        if _is_fc2dbnet_age_wall(html):
            return {"_error": "fc2db.net returned its 18+ page — the age flag was not accepted"}
        return {"_error": "fc2db.net returned a Cloudflare challenge"}
    return parse_fc2dbnet_work(html)


def _stamp_fc2dbnet(con, code):
    """Separate from fc2db_at: fc2ppv-db's "nobody listed" stamp must not stop THIS site."""
    try:
        con.execute("UPDATE works SET fc2dbnet_at=? WHERE code=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), code))
    except sqlite3.OperationalError:
        pass


def _apply_fc2dbnet(con, code, data):
    """Fill gaps only. fc2ppv-db stays the source of truth where it has a value, and
    nothing a person set is ever touched."""
    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                "VALUES(?,1,'available','fc2db.net')", (code,))
    row = con.execute("SELECT title, cover_url, release_date, producer, duration, actress_id "
                      "FROM works WHERE code=?", (code,)).fetchone()
    title, cover, rel, prod, dur, aid0 = row
    sets, vals = [], []
    for col, cur, key in (("title", title, "title_jp"), ("cover_url", cover, "cover_url"),
                          ("release_date", rel, "release_date"), ("producer", prod, "producer")):
        if not (cur or "").strip() and data.get(key):
            sets.append(col + "=?"); vals.append(data[key])
    if not dur and data.get("duration"):          # ffprobe's own value, when present, wins
        sets.append("duration=?"); vals.append(data["duration"])
    if sets:
        con.execute("UPDATE works SET %s WHERE code=?" % ",".join(sets), vals + [code])
    canon = []
    for tg in data.get("tags", []):
        c = _canon_tag(con, tg)
        if c:
            con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, c))
            canon.append(c)
    # censorship: only when nothing has set it yet, and never over a hand-set value
    try:
        cur = con.execute("SELECT censorship, censorship_src FROM works WHERE code=?",
                          (code,)).fetchone()
    except sqlite3.OperationalError:
        cur = None
    cens = "censored" if "Light Mosaic" in canon else ("uncensored" if "Uncensored" in canon else None)
    if cens and cur and not cur[0] and (cur[1] or "") != "manual":
        con.execute("UPDATE works SET censorship=? WHERE code=?", (cens, code))
    # cast: only into an EMPTY lead that a person did not set. fc2db_at being stamped
    # ("fc2ppv-db lists nobody") is exactly the case this site is for, so it is not a reason
    # to skip. One named performer is ADDED; nothing that came from elsewhere is removed.
    try:
        src = con.execute("SELECT actress_src FROM works WHERE code=?", (code,)).fetchone()
        manual = bool(src and (src[0] or "") == "manual")
    except sqlite3.OperationalError:
        manual = False
    cast = data.get("actresses") or []
    if cast and aid0 is None and not manual:
        for idx, a in enumerate(cast):
            name = a["name"]
            # exact name only — a loose match here would make a wrong link permanent, and
            # there is no uuid to confirm it against. No aliases are grafted either.
            hit = con.execute(
                "SELECT id FROM actresses WHERE name_jp=? OR name_en=? COLLATE NOCASE "
                "ORDER BY id LIMIT 1", (name, name)).fetchone()
            if hit:
                aid = hit[0]
            else:
                jp = bool(re.search(r"[぀-ヿ一-鿿]", name))
                aid = con.execute(
                    "INSERT INTO actresses(name_jp,name_en,source) VALUES(?,?,'fc2db.net')",
                    (name if jp else None, None if jp else name)).lastrowid
                set_english_from_kana(con, aid)
            if a.get("url"):
                con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) "
                            "VALUES(?, 'fc2db.net', ?)", (aid, a["url"]))
            if idx == 0:
                try:
                    con.execute("UPDATE works SET actress_id=?, actress_src='fc2db.net' "
                                "WHERE code=? AND actress_id IS NULL", (aid, code))
                except sqlite3.OperationalError:
                    con.execute("UPDATE works SET actress_id=? WHERE code=? "
                                "AND actress_id IS NULL", (aid, code))
            con.execute("INSERT OR IGNORE INTO work_actresses(code,actress_id,role) "
                        "VALUES(?,?,?)", (code, aid, "main" if idx == 0 else "costar"))
    _stamp_fc2dbnet(con, code)


def scan_fc2dbnet(con, codes, progress=None, solver_url=None):
    """The fc2db.net pass. Same contract as scan_fc2db: hits/absent/failed kept apart, a 404
    stamped as asked-and-answered, a challenge/age wall/network failure NOT stamped, three
    failures in a row (or one age wall) stop the run, and the run releases only its own
    resolver session."""
    p = progress if progress is not None else PROGRESS
    if p.get("hold"):              # one step of a pipeline: keep the job's totals
        for k in ("hits", "absent", "failed"):    # ...but never trust a caller to have
            p.setdefault(k, 0)                     # created them: a missing one was a
        p.setdefault("error", "")                  # KeyError mid-scan
        p.update(running=True, done=0, total=len(codes), code="")
    else:
        p.update(running=True, done=0, total=len(codes), hits=0, absent=0, failed=0,
                 error="", code="")
    streak = 0
    try:
        for i, code in enumerate(codes, 1):
            p["code"] = code
            try:
                data = fetch_fc2dbnet(code, solver_url=solver_url)
                if data.get("_error"):
                    streak += 1
                    p["failed"] += 1
                    p["error"] = "%s: %s" % (code, data["_error"])
                    if streak >= 3 or "18+" in data["_error"]:
                        break
                elif data.get("_missing") or not data:
                    streak = 0
                    p["absent"] += 1
                    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,"
                                "source) VALUES(?,1,'available','fs')", (code,))
                    _stamp_fc2dbnet(con, code)
                    con.commit()
                else:
                    streak = 0
                    _apply_fc2dbnet(con, code, data)
                    p["hits"] += 1
                    con.commit()
            except Exception as exc:
                try:
                    con.rollback()
                except Exception:
                    pass
                p["failed"] += 1
                p["error"] = "%s: %r" % (code, exc)
            p["done"] = i
            time.sleep(0.4)                    # a plain-urllib site: be gentle
    finally:
        if not p.get("hold"):
            p["running"] = False
        if solver_url:
            solver_release(solver_url, _FC2DBNET_SESSION)
    return p


def _download(url, cookie=None, ua=None, timeout=20):
    try:
        h = {"User-Agent": ua or UA}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if r.status == 200 else None
    except Exception:
        return None


# ---- javfc2.xyz: title + cover for any FC2 code (no login needed) ----------
JAVFC2 = "https://javfc2.xyz"


def fetch_javfc2(code):
    """Fetch {title, cover_url} for an FC2 code from javfc2.xyz's /watch page.
    Public, no auth. Returns None if the page is absent or has no usable data."""
    num = _digits(code)
    if not num or not re.search(r"fc2", code or "", re.I):
        return None                       # javfc2 /watch/fc2ppv-<n> is FC2-only
    html = _get(f"{JAVFC2}/watch/fc2ppv-{num}.html", timeout=20)
    if not html:
        return None
    title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        title = htmlmod.unescape(m.group(1)).strip()
        title = re.sub(r"^\s*FC2PPV\s*\d+\s*", "", title, flags=re.I).strip()
        title = re.sub(r"\s*[|\-–]\s*javfc2.*$", "", title, flags=re.I).strip()
    cover = ""
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                  html, re.I)
    if m:
        cover = m.group(1).strip()
    if "blank_thumbnail" in cover or "default_image" in cover:
        cover = ""                        # site's placeholder = no real cover
    if not title and not cover:
        return None
    return {"title": title, "cover_url": cover}


def backfill_wishlist(con, codes=None, sleep=0.5, progress=None):
    """Fill title_en / cover_url on wishlist (availability='missing') works from javfc2.xyz.
    `codes` limits to a set; otherwise every missing work still lacking a title or cover.
    Returns the number of works updated. Idempotent (only fills empty fields)."""
    if codes:
        rows = [(c,) for c in codes]
    else:
        rows = con.execute(
            "SELECT code FROM works WHERE availability='missing' "
            "AND (title_en IS NULL OR title_en='' OR cover_url IS NULL OR cover_url='')"
        ).fetchall()
    total, hits = len(rows), 0
    for i, (code,) in enumerate(rows):
        if progress:
            progress(i, total, code)
        d = fetch_javfc2(code)
        if d:
            sets, vals = [], []
            if d.get("title"):
                sets += ["title_en=COALESCE(NULLIF(title_en,''),?)"]
                vals += [d["title"]]
            if d.get("cover_url"):
                sets += ["cover_url=COALESCE(NULLIF(cover_url,''),?)"]
                vals += [d["cover_url"]]
            if sets:
                vals.append(code)
                con.execute(f"UPDATE works SET {','.join(sets)} WHERE code=?", vals)
                hits += 1
        if sleep:
            time.sleep(sleep)
    con.commit()
    if progress:
        progress(total, total, "")
    return hits


def _alias_owner(con, alias):
    """The actress an alias belongs to, but ONLY when it belongs to exactly one.

    Stage names like りん or ほのか are attached to several performers. Matching on an
    ambiguous alias silently folded a new person into whoever happened to hold it, and the
    page's aliases were then copied onto that row, making the collision permanent."""
    rows = con.execute("SELECT DISTINCT actress_id FROM aliases WHERE alias=? COLLATE NOCASE",
                       (alias,)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _fc2_actress(con, a):
    """Match a in {uuid,name,face,aliases} to an existing actress, or create one.

    Returns (actress_id, how) where how is "uuid" | "name" | "alias" | "new". The caller
    needs `how`: identity is only certain on a uuid match or a fresh row, and the page's
    aliases must not be grafted onto a row we merely name-matched."""
    uuid, name = a.get("uuid"), (a.get("name") or "").strip()
    if uuid:
        r = con.execute("SELECT l.actress_id FROM links l JOIN actresses a ON a.id=l.actress_id "
                        "WHERE l.kind='fc2ppvdb' AND l.url=?", (uuid,)).fetchone()
        if r:
            # the sidebar disambiguates same-name performers by this column, so keep it filled
            con.execute("UPDATE actresses SET fc2ppvdb_uuid=COALESCE(fc2ppvdb_uuid,?) "
                        "WHERE id=?", (uuid, r[0]))
            return r[0], "uuid"
        r = con.execute("SELECT id FROM actresses WHERE fc2ppvdb_uuid=?", (uuid,)).fetchone()
        if r:
            con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) "
                        "VALUES(?, 'fc2ppvdb', ?)", (r[0], uuid))
            return r[0], "uuid"
    if name:
        r = con.execute("SELECT id FROM actresses WHERE name_jp=? OR "
                        "name_en=? COLLATE NOCASE OR name_kana=?",
                        (name, name, name)).fetchone()
        if r:
            return r[0], "name"
        owner = _alias_owner(con, name)
        if owner:
            return owner, "alias"
    for al in a.get("aliases", []):
        owner = _alias_owner(con, al)
        if owner:
            return owner, "alias"
    aid = con.execute("INSERT INTO actresses(name_jp,source,fc2ppvdb_uuid) "
                      "VALUES(?, 'fc2ppv-db', ?)", (name or None, uuid)).lastrowid
    if uuid:
        con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) VALUES(?, 'fc2ppvdb', ?)",
                    (aid, uuid))
    return aid, "new"


_FC2DB_FIELDS = ("title_jp", "actresses", "release_date", "cover_url",
                 "censorship", "tags", "producer")


def _has_fields(data):
    """True when the parsed page carried anything worth storing."""
    return bool(data) and not data.get("_missing") and any(data.get(k) for k in _FC2DB_FIELDS)


def _stamp_fc2db(con, code):
    """Record that this code's fc2ppv-db page has been read.

    Stamped for an EMPTY page too. Only hits were stamped at first, so the 109 codes the site
    has no page for stayed "never checked" and every future scan re-read all of them — a cost
    that only grows. The "All movies" scope still re-reads everything when you want a refresh.
    """
    try:
        con.execute("UPDATE works SET fc2db_at=? WHERE code=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), code))
    except sqlite3.OperationalError:
        pass                                  # older catalog without the column


def _apply_fc2db(con, code, data):
    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                "VALUES(?,1,'available','fc2ppv-db')", (code,))
    canon = []
    for tg in data.get("tags", []):
        c = _canon_tag(con, tg)         # JP->EN / merge-alias, drops untranslated JP junk
        if c:
            con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, c))
            canon.append(c)
    sets, vals = [], []
    # fc2ppv-db is the SOURCE OF TRUTH — it OVERWRITES these fields (unlike the shell scan,
    # which is fill-if-empty). So running fc2ppv-db always wins over 123av/ffjav data, and a
    # later shell scan can only fill gaps it leaves.
    # censorship is handled separately: it is the one field the user can set in the editor,
    # and a scrape used to overwrite that silently on the next run.
    for col, key in (("title", "title_jp"), ("release_date", "release_date"),
                     ("producer", "producer"), ("cover_url", "cover_url")):
        if data.get(key):
            sets.append(f"{col}=?")
            vals.append(data[key])
    # The site's own tag is the dependable signal: of 518 works whose title says 無修正,
    # every one carries the Uncensored tag (and none lacks it). mosaicStatus wins when the
    # page has it; otherwise the tags decide; otherwise the column is left as it is.
    cens = data.get("censorship")
    if not cens:
        if "Light Mosaic" in canon:
            cens = "censored"
        elif "Uncensored" in canon:
            cens = "uncensored"
    if cens:
        try:
            src = con.execute("SELECT censorship_src FROM works WHERE code=?",
                              (code,)).fetchone()
        except sqlite3.OperationalError:
            src = None                      # column not added yet (older catalog)
        if not (src and src[0] == "manual"):
            sets.append("censorship=?")
            vals.append(cens)
    if sets:
        con.execute(f"UPDATE works SET {','.join(sets)} WHERE code=?", vals + [code])
    _stamp_fc2db(con, code)
    cast = data.get("actresses", [])
    faces = []                      # (actress_id, url) — downloaded after the commit below
    if len(cast) > 1:
        # The cast table was never written, so a scan could not record a co-star: only the
        # lead landed (on works.actress_id) and everyone else on the page was dropped.
        # A page naming SEVERAL performers is authoritative for that code, so replace.
        # A page naming one is not: it cannot tell "she is the only one" from "only the lead
        # is listed", and ~4,100 cast rows here came from elsewhere. Those we only add to.
        con.execute("DELETE FROM work_actresses WHERE code=?", (code,))
    for idx, a in enumerate(cast):
        aid, how = _fc2_actress(con, a)
        if idx == 0:
            # Primary source: replace a lead that came from the folder pass or from an
            # earlier fc2ppv-db read. COALESCE alone meant whoever wrote first won, so a
            # folder-derived guess could never be corrected. 'manual' is never touched.
            try:
                con.execute("UPDATE works SET actress_id=?, actress_src='fc2ppv-db' "
                            "WHERE code=? AND COALESCE(actress_src,'') <> 'manual'",
                            (aid, code))
            except sqlite3.OperationalError:
                con.execute("UPDATE works SET actress_id=COALESCE(actress_id,?) WHERE code=?",
                            (aid, code))      # older catalog without actress_src
        con.execute("INSERT OR IGNORE INTO work_actresses(code,actress_id,role) VALUES(?,?,?)",
                    (code, aid, "main" if idx == 0 else "costar"))
        if how in ("uuid", "new"):
            # Only graft the page's aliases when we are sure who this is. On a loose name or
            # alias match they would make a wrong link permanent.
            for al in a.get("aliases", []):
                con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) VALUES(?,?)",
                            (aid, al))
        set_english_from_kana(con, aid)       # give her a romaji English name if she lacks one
        if a.get("face"):
            row = con.execute("SELECT image_blob IS NOT NULL FROM actresses WHERE id=?",
                              (aid,)).fetchone()
            if row and not row[0]:
                faces.append((aid, a["face"]))
    # Portraits are fetched AFTER the row work is committed. Downloading inside the
    # transaction held SQLite's write lock for a 20 s HTTP call, once per new face, and in
    # solver mode nothing sleeps between codes — a library of new faces meant a long write
    # transaction per movie.
    con.commit()
    for aid, url in faces:
        img = _download(url)                              # CDN — open, no cookie needed
        if img:
            con.execute("UPDATE actresses SET image_blob=?, "
                        "image_url=COALESCE(image_url,?) WHERE id=?",
                        (sqlite3.Binary(img), url, aid))
            con.commit()


def scan_fc2db(con, codes, cookie, ua, thumb_dir=None, progress=None, solver_url=None):
    """Mass-scrape fc2ppv-db for a list of owned codes.

    `solver_url` loads every page through FlareSolverr. Otherwise `cookie` + `ua` are
    the pasted browser session, replayed with urllib."""
    p = progress if progress is not None else PROGRESS
    # hits / absent / failed are kept apart so the result answers the only question worth
    # asking afterwards: did the scan fail, or does fc2ppv-db just not have these movies?
    # `hits` stays additive when this runs as the second pass of a combined scan —
    # resetting it meant the final "Done — N updated" silently dropped the titles pass.
    if p.get("hold"):              # one step of a pipeline: keep the job's totals
        for k in ("hits", "absent", "failed"):    # ...but never trust a caller to have
            p.setdefault(k, 0)                     # created them: a missing one was a
        p.setdefault("error", "")                  # KeyError mid-scan
        p.update(running=True, done=0, total=len(codes),
                 code="solving check…" if solver_url else "")
    else:
        p.update(running=True, done=0, total=len(codes), hits=0, absent=0, failed=0, error="",
                 code="solving check…" if solver_url else "")
    streak = 0
    try:
        for i, code in enumerate(codes, 1):
            if not (solver_url and i == 1):
                p["code"] = code
            try:
                data = fetch_fc2ppvdb(code, cookie=cookie, ua=ua, solver_url=solver_url)
                if data.get("_error"):
                    streak += 1
                    p["failed"] += 1
                    p["error"] = data["_error"]
                    # The 18+ wall is the same for every code; don't walk the library.
                    if streak >= 3 or "18+" in data["_error"]:
                        break
                else:
                    streak = 0
                    p["error"] = ""
                    # "the parser found fields" is the hit test. Keying it on title/cast
                    # alone threw away the date, cover, tags, censorship and seller of a
                    # page that merely has no performer listed -- and then stamped it, so
                    # "Scan missing" never came back for it.
                    if _has_fields(data):
                        _apply_fc2db(con, code, data)
                        p["hits"] += 1
                        con.commit()
                    else:
                        p["absent"] += 1   # page read fine, fc2ppv-db has nothing for it
                        con.execute("INSERT OR IGNORE INTO works"
                                    "(code,in_library,availability,source) "
                                    "VALUES(?,1,'available','fs')", (code,))
                        _stamp_fc2db(con, code)   # asked and answered: do not ask again
                        con.commit()
            except Exception as exc:
                # a half-written row must not ride along on the NEXT code's commit
                try:
                    con.rollback()
                except Exception:
                    pass
                p["failed"] += 1
                p["error"] = "%s: %r" % (code, exc)
            p["code"] = code
            p["done"] = i
            if not solver_url:
                time.sleep(0.4)      # gentle; cookie mode must finish inside a session's life
    finally:
        # The CALLER owns `running` when it holds the job: it still has to rebuild the
        # index, and a poll that saw `running: false` here reloaded the grid from the
        # pre-rebuild index — the toast said "N updated" and nothing on screen changed.
        if not p.get("hold"):
            p["running"] = False
        if solver_url:
            solver_release(solver_url)   # don't leave a Chromium parked on the resolver
    return p


# ---- orchestrator ----------------------------------------------------------
def _canon_tag(con, tag, drop_jp=True):
    """Canonical stored form of a scraped tag, honored on every run and for every user:
      * map via tag_translations (JP->EN and English merge-aliases we recorded), so merged
        tags stay merged;
      * drop untranslated Japanese tags (the junk one-offs) so they never pollute the set.
    Returns the canonical string, or None to skip. Manual entry uses drop_jp=False."""
    t = (tag or "").strip()
    if not t:
        return None
    row = con.execute("SELECT display FROM tag_translations WHERE raw=? COLLATE NOCASE",
                      (t,)).fetchone()
    if row and row[0]:
        return row[0].strip()
    if drop_jp and re.search(r'[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]', t):
        return None                     # untranslated Japanese one-off -> don't store
    return t


def scan_code(con, code, force=False, sources=None):
    """Enrich one code. Returns dict of fields actually changed.

    Refuses anything that cannot be a release id. A caller once handed scan_library() a
    STRING instead of a list of codes; list() exploded it into characters and this function
    dutifully created a catalog row for each one ("n", "o", "t", "-", ...), then filled them
    from whatever a one-letter search happened to return. The row is the damage, so the guard
    belongs here rather than only at the callers."""
    code = (code or "").strip()
    if len(code) < 3 or not re.search(r"[A-Za-z0-9]{2,}", code):
        return {}
    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                "VALUES(?,1,'available','fs')", (code,))
    row = con.execute("SELECT title,title_en,release_date,cover_url FROM works "
                      "WHERE code=?", (code,)).fetchone()
    title_jp, title_en, rel, cover = (row or ("", "", "", ""))
    num = _digits(code)
    changed = {}
    raw_en, raw_jp = None, None            # untidied text, for tag harvesting

    # `sources` = the enabled set from Settings. None means "no opinion, use everything",
    # which is what every caller did before: the checkboxes were saved and never read, so
    # unticking 123av or ffjav changed nothing at all.
    def on(name):
        return sources is None or name in sources

    if num and on("123av") and (force or not title_en):
        t = fetch_123av_title(num)
        if t:
            raw_en = t
            clean = tidy_title(t)
            if clean and clean != title_en:
                con.execute("UPDATE works SET title_en=? WHERE code=?", (clean, code))
                title_en = clean
                changed["title_en"] = clean

    if on("ffjav") and (force or not title_jp or not cover or not rel):
        ff = fetch_ffjav(code)
        if ff.get("title_jp") and (force or not title_jp):
            raw_jp = ff["title_jp"]
            clean = tidy_title(ff["title_jp"])
            con.execute("UPDATE works SET title=? WHERE code=?", (clean, code))
            title_jp = clean
            changed["title"] = clean
        if ff.get("cover_url") and (force or not cover):
            con.execute("UPDATE works SET cover_url=? WHERE code=?", (ff["cover_url"], code))
            cover = ff["cover_url"]
            changed["cover_url"] = ff["cover_url"]
        if ff.get("release_date") and (force or not rel):
            con.execute("UPDATE works SET release_date=? WHERE code=?", (ff["release_date"], code))
            rel = ff["release_date"]
            changed["release_date"] = ff["release_date"]

    # harvest tags from the RAW titles (so bracketed/promo keywords are kept), then merge
    added = []
    for t in derive_tags(raw_jp or title_jp, raw_en or title_en):
        c = _canon_tag(con, t)
        if c and con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, c)).rowcount:
            added.append(c)
    if added:
        changed["tags"] = added
    # fill actress cup/age from the title when missing
    filled = _fill_actress_from_title(con, code, raw_jp or title_jp, raw_en or title_en)
    if filled:
        changed.update(filled)
    con.commit()
    return changed


def retag_all(con):
    """Network-free pass over every work: harvest tags from the (raw) stored titles,
    then tidy the titles in place. Idempotent."""
    con.execute("PRAGMA busy_timeout=60000")
    tagrows = 0
    retitled = 0
    for code, jp, en in con.execute("SELECT code,title,title_en FROM works").fetchall():
        for t in derive_tags(jp, en):                 # harvest BEFORE tidying
            c = _canon_tag(con, t)
            if c:
                tagrows += con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)",
                                        (code, c)).rowcount
        _fill_actress_from_title(con, code, jp, en)
        cj, ce = tidy_title(jp), tidy_title(en)
        if cj != (jp or "") or ce != (en or ""):
            con.execute("UPDATE works SET title=?, title_en=? WHERE code=?",
                        (cj, ce, code))
            retitled += 1
    con.commit()
    return tagrows, retitled


# ---- standalone / library sweep -------------------------------------------
PROGRESS = {"running": False, "done": 0, "total": 0, "hits": 0,
            "absent": 0, "failed": 0, "phase": "", "note": "", "code": ""}


def enrich_actress(con, actress_id, force=False, dry=False, library_codes=None,
                   sources=None):
    """Scrape javdatabase + jav.guru for one actress, VERIFY by FC2-code overlap with the
    library, and (only when verified, or force=True) write the physical profile + aliases.

    `library_codes` is the set of codes you actually own (files on disk). Verification is
    "does your library contain any title from her scraped filmography" — which needs no
    prior linkage, so it recognises her even before her movies are attributed. dry=True
    fetches + verifies but writes NOTHING. Returns a report dict; writes only if applied."""
    row = con.execute("SELECT id,name_en,name_jp FROM actresses WHERE id=?",
                      (actress_id,)).fetchone()
    if not row:
        return {"error": "no such actress"}
    aid, name_en, name_jp = row
    # names to try (english first — the scrapers are romaji-slug based)
    names = [n for n in [name_en] if n]
    names += [r[0] for r in con.execute("SELECT alias FROM aliases WHERE actress_id=?", (aid,))]
    # what you own: the whole library if provided, else fall back to already-linked works
    owned = set(library_codes) if library_codes is not None else set(
        r[0] for r in con.execute(
            "SELECT code FROM works WHERE actress_id=? AND in_library=1", (aid,)))

    # `sources` = the ticked set from Settings (None = no opinion, use both). The boxes were
    # saved and ignored: unticking javdatabase or jav.guru still fetched them.
    use_jdb = sources is None or "javdatabase" in sources
    use_guru = sources is None or "jav.guru" in sources
    prof, guru_url, guru_codes, tried = {}, None, set(), []
    for nm in names:
        tried.append(nm)
        if use_jdb and not prof:
            prof = fetch_javdatabase(nm)
        if use_guru and not guru_codes:
            guru_url, guru_codes = fetch_javguru_codes(nm)
        if (prof or not use_jdb) and (guru_codes or not use_guru):
            break

    matched = sorted(owned & guru_codes)
    verified = bool(matched)
    report = {"actress_id": aid, "name": name_en, "tried": tried,
              "profile": prof or None, "profile_source": prof.get("url") if prof else None,
              "javguru_source": guru_url, "javguru_codes": len(guru_codes),
              "owned_codes": len(matched), "matched_codes": matched,
              "verified": verified, "applied": False}
    if not prof:
        report["note"] = "no javdatabase profile found for these name spellings"
        return report
    if dry:
        report["note"] = ("verified — you own %d of her %d listed titles (%s)"
                          % (len(matched), len(guru_codes), ", ".join(matched[:5]))) \
            if verified else \
            ("NOT verified — none of the %d works listed for her online are in your library, "
             "so this may be a different person with the same name." % len(guru_codes))
        return report                                   # preview only, no writes
    if not (verified or force):
        report["note"] = ("profile found but NOT verified — none of this actress's owned "
                           "FC2 codes appear in her scraped filmography, so it may be a "
                           "different person with the same name. Re-run with force to apply.")
        return report

    # ---- apply (verified or forced) ----
    # COALESCE(NULLIF(col,''), ?) so blank strings count as empty and never block a fill,
    # while genuine curated values are still preserved.
    sets, vals = [], []
    for col in ("age", "height", "cup", "bust", "waist", "hip"):
        if prof.get(col) is not None:
            sets.append(f"{col}=COALESCE(NULLIF({col},''), ?)")
            vals.append(prof[col])
    if prof.get("dob"):
        sets.append("birthdate=COALESCE(NULLIF(birthdate,''), ?)")
        vals.append(prof["dob"])
    if prof.get("name_jp"):
        sets.append("name_jp=COALESCE(NULLIF(name_jp,''), ?)")
        vals.append(prof["name_jp"])
    if sets:
        con.execute(f"UPDATE actresses SET {','.join(sets)} WHERE id=?", vals + [aid])
    for alias in (prof.get("name_en"), prof.get("name_jp")):
        if alias:
            con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) VALUES(?,?)",
                        (aid, alias))
    for kind, url in (("javdatabase", prof.get("url")), ("jav.guru", guru_url)):
        if url:
            con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) VALUES(?,?,?)",
                        (aid, kind, url))
    # fold her scraped filmography into the catalog, linked to her:
    #   - a code you OWN  -> attribute the file to her (available, in_library=1)
    #   - a code you DON'T -> a 'missing' wishlist entry
    # Either way COALESCE keeps an existing actress link; owned status is never downgraded.
    wl = 0
    for gc in sorted(guru_codes):
        if gc in owned:
            con.execute(
                "INSERT INTO works(code,actress_id,availability,in_library,source) "
                "VALUES(?,?, 'available', 1, 'jav.guru') "
                "ON CONFLICT(code) DO UPDATE SET "
                "actress_id=COALESCE(works.actress_id, excluded.actress_id), "
                "in_library=1, availability='available'", (gc, aid))
        else:
            cur = con.execute(
                "INSERT INTO works(code,actress_id,availability,in_library,source) "
                "VALUES(?,?, 'missing', 0, 'jav.guru') "
                "ON CONFLICT(code) DO UPDATE SET "
                "actress_id=COALESCE(works.actress_id, excluded.actress_id)", (gc, aid))
            if cur.rowcount:
                wl += 1
    con.commit()
    report["applied"] = True
    report["wishlist_added"] = wl
    report["attributed_owned"] = len(owned & guru_codes)
    report["forced_unverified"] = bool(force and not verified)
    return report


def scan_library(codes, force=False, progress=None, con=None, sources=None):
    """Scrape metadata for `codes`.

    `con` is the caller's connection, the way scan_code / scan_fc2db / backfill_wishlist
    already work. Opening DB here was wrong inside the packaged app: serve.py copies the
    catalog to a writable folder and serves THAT, while this module's DB still pointed at
    the read-only copy in the bundle, so a toolbar scan showed progress and changed nothing
    the UI would ever read."""
    if isinstance(codes, str):
        raise TypeError("scan_library() takes a list of codes, not the string %r "
                        "(list() would walk it character by character)" % codes[:40])
    codes = list(codes)
    p = progress if progress is not None else PROGRESS
    own = con is None
    if own:
        con = sqlite3.connect(DB, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
    # absent/failed belong to the fc2ppv-db pass, but a titles-only run that left them at
    # the PREVIOUS run's values reported those as its own. Reset everything a job owns.
    if p.get("hold"):              # one step of a pipeline: keep the job's totals
        for k in ("hits", "absent", "failed"):    # ...but never trust a caller to have
            p.setdefault(k, 0)                     # created them: a missing one was a
        p.setdefault("error", "")                  # KeyError mid-scan
        p.update(running=True, done=0, total=len(codes), code="")
    else:
        p.update(running=True, done=0, total=len(codes), hits=0, absent=0, failed=0,
                 code="", error="")
    try:
        for i, code in enumerate(codes, 1):
            try:
                ch = scan_code(con, code, force=force, sources=sources)
                if ch:
                    p["hits"] += 1
            except Exception as exc:
                # was `pass`: a 123av/ffjav failure or a constraint error counted nothing,
                # set no error, and the dialog reported a clean pass
                try:
                    con.rollback()
                except Exception:
                    pass
                p["failed"] = p.get("failed", 0) + 1
                p["error"] = "%s: %r" % (code, exc)
            p["done"] = i
            p["code"] = code
            time.sleep(0.4)
    finally:
        if own:                      # never close a connection we were handed
            con.close()
        # `hold` = another pass follows. Clearing `running` here let a 1.2 s poll land in
        # the gap and paint "Done" while the fc2ppv-db pass was about to start.
        if not p.get("hold"):
            p["running"] = False
    return p


if __name__ == "__main__":
    con = sqlite3.connect(DB, timeout=30)
    if len(sys.argv) > 1 and sys.argv[1] == "--retag":   # scan.py --retag (no network)
        tr, rt = retag_all(con)
        print(f"added {tr} tag rows, tidied {rt} titles")
    elif len(sys.argv) > 2 and sys.argv[1] == "--enrich":   # scan.py --enrich <actress_id> [force]
        import json as _json
        print(_json.dumps(enrich_actress(con, int(sys.argv[2]),
                                         force=(len(sys.argv) > 3)), indent=2, default=str))
    elif len(sys.argv) > 1:                     # scan.py <CODE> [force]
        print(scan_code(con, sys.argv[1], force=(len(sys.argv) > 2)))
    else:
        codes = [r[0] for r in con.execute("SELECT code FROM works WHERE in_library=1")]
        con.close()
        scan_library(codes)
