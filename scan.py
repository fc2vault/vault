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
import os
import re
import socket
import sqlite3
import sys
import time
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


# ---- IPv4-only opener -----------------------------------------------------
# Cloudflare binds a cf_clearance cookie to the exact IP that solved its check.
# On a dual-stack connection the browser typically solves it over IPv4, but
# Python's urllib prefers IPv6 (often a rotating privacy address) — a different
# source IP, so CF re-challenges (403 cf-mitigated=challenge) even with a fresh,
# correct cookie + user-agent. Forcing IPv4 makes the server exit from the same
# address family the browser used, so fc2ppv-db sessions validate.
class _V4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port or 443,
                                   socket.AF_INET, socket.SOCK_STREAM)
        af, socktype, proto, _cn, sa = infos[0]
        sock = socket.socket(af, socktype, proto)
        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            sock.settimeout(self.timeout)
        if self.source_address:
            sock.bind(self.source_address)
        sock.connect(sa)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _V4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_V4HTTPSConnection, req)


_V4_OPENER = urllib.request.build_opener(_V4HTTPSHandler)


def _get(url, timeout=20, headers=None, ipv4=False):
    try:
        h = {"User-Agent": UA}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h)
        opener = _V4_OPENER.open if ipv4 else urllib.request.urlopen
        with opener(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8", "ignore")
    except Exception:
        return None


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
    cov = re.search(r'<img class="image" src="([^"]+)"', html)
    cov = cov.group(1) if cov else None
    # only trust a result whose cover filename actually matches this code
    if cov and _digits(code) not in cov:
        cov = None
    tt = re.search(r'<h5 class="title[^"]*"[^>]*>\s*<a[^>]*>(.*?)</a>', html, re.S)
    jp = None
    if tt:
        jp = htmlmod.unescape(re.sub(r"<[^>]+>", "", tt.group(1)).strip())
        jp = re.sub(r"^\s*FC2[\s\-]?PPV[\s\-]?\d+\s*", "", jp, flags=re.I).strip()
        if _digits(code) not in (tt.group(1) + (cov or "")) and not cov:
            jp = None                                          # unrelated result
    d = re.search(r"(\d{4}-\d{2}-\d{2})", html)
    d = d.group(1) if d else None
    return {"title_jp": jp or None, "cover_url": cov, "release_date": d}


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
# Cloudflare-gated: only reachable with a session cookie the USER established in a real
# browser (the human passes Turnstile; we reuse the session). The parser itself is pure and
# transport-independent — feed it the page HTML from any source (cookie fetch or bookmarklet).
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


def parse_fc2ppvdb_video(html):
    """Parse an fc2ppv-db.com /videos/<id> page into a structured dict. Pure — no network."""
    if not html or "fc2ppv" not in html.lower():
        return {}
    d = {}
    m = re.search(r'/videos/(\d{5,})"', html)
    if m:
        d["code"] = "FC2-PPV-" + m.group(1)
    m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    if m:
        t = htmlmod.unescape(m.group(1))
        t = re.sub(r"^\s*FC2-?PPV-?\d+\s*", "", t).strip()
        d["title_jp"] = t or None
    m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
    if m:
        d["cover_url"] = m.group(1)
    d["release_date"] = _jp_date(html)
    # mosaic: the page shows an "Uncensored" badge, or the data carries mosaicStatus
    if re.search(r'"mosaicStatus":"(FULL|THIN)"', html) or ">Censored<" in html:
        d["censorship"] = "censored"
    elif "Uncensored" in html or '"NONE"' in html:
        d["censorship"] = "uncensored"
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
    # seller / studio
    sm = re.search(r'/(?:en|ja)/sellers/[^"]+"(.*?)</a>', html, re.S)
    if sm:
        nm = re.search(r'font-medium[^>]*>([^<]+)<', sm.group(1))
        if nm:
            d["producer"] = htmlmod.unescape(nm.group(1)).strip()
    return d


def fetch_fc2ppvdb(code, cookie=None, ua=None):
    """Fetch + parse one video page. `cookie` is the browser session string (cf_clearance +
    auth) the user provides; without it Cloudflare returns a challenge (empty parse)."""
    num = _digits(code)
    if not num:
        return {}
    headers = {}
    if ua:
        headers["User-Agent"] = ua
    if cookie:
        headers["Cookie"] = cookie
    # fc2ppv-db is Cloudflare-gated; force IPv4 so the cf_clearance cookie (bound
    # to the IPv4 the browser solved the check on) validates. See _V4HTTPSConnection.
    html = _get(f"{FC2DB}/en/videos/{num}", headers=headers, timeout=25, ipv4=True)
    return parse_fc2ppvdb_video(html or "")


def _download(url, cookie=None, ua=None, timeout=20, ipv4=False):
    try:
        h = {"User-Agent": ua or UA}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(url, headers=h)
        opener = _V4_OPENER.open if ipv4 else urllib.request.urlopen
        with opener(req, timeout=timeout) as r:
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


def _fc2_actress(con, a):
    """Match a in {uuid,name,face,aliases} to an existing actress (by fc2 uuid link, name,
    or alias) or create one. Returns the actress id."""
    uuid, name = a.get("uuid"), (a.get("name") or "").strip()
    if uuid:
        r = con.execute("SELECT actress_id FROM links WHERE kind='fc2ppvdb' AND url=?",
                        (uuid,)).fetchone()
        if r:
            return r[0]
    if name:
        r = con.execute("SELECT id FROM actresses WHERE name_jp=? OR "
                        "name_en=? COLLATE NOCASE OR name_kana=?",
                        (name, name, name)).fetchone()
        if r:
            return r[0]
        r = con.execute("SELECT actress_id FROM aliases WHERE alias=? COLLATE NOCASE",
                        (name,)).fetchone()
        if r:
            return r[0]
    for al in a.get("aliases", []):
        r = con.execute("SELECT actress_id FROM aliases WHERE alias=? COLLATE NOCASE",
                        (al,)).fetchone()
        if r:
            return r[0]
    aid = con.execute("INSERT INTO actresses(name_jp,source) VALUES(?, 'fc2ppv-db')",
                      (name or None,)).lastrowid
    if uuid:
        con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) VALUES(?, 'fc2ppvdb', ?)",
                    (aid, uuid))
    return aid


def _apply_fc2db(con, code, data):
    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                "VALUES(?,1,'available','fc2ppv-db')", (code,))
    sets, vals = [], []
    # fc2ppv-db is the SOURCE OF TRUTH — it OVERWRITES these fields (unlike the shell scan,
    # which is fill-if-empty). So running fc2ppv-db always wins over 123av/ffjav data, and a
    # later shell scan can only fill gaps it leaves.
    for col, key in (("title", "title_jp"), ("release_date", "release_date"),
                     ("producer", "producer"), ("cover_url", "cover_url"),
                     ("censorship", "censorship")):
        if data.get(key):
            sets.append(f"{col}=?")
            vals.append(data[key])
    if sets:
        con.execute(f"UPDATE works SET {','.join(sets)} WHERE code=?", vals + [code])
    for tg in data.get("tags", []):
        # store the CANONICAL form: English label if we have a translation, else the JP tag
        row = con.execute("SELECT display FROM tag_translations WHERE raw=? COLLATE NOCASE",
                          (tg,)).fetchone()
        con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)",
                    (code, row[0] if row and row[0] else tg))
    for idx, a in enumerate(data.get("actresses", [])):
        aid = _fc2_actress(con, a)
        if idx == 0:
            con.execute("UPDATE works SET actress_id=COALESCE(actress_id,?) WHERE code=?",
                        (aid, code))
        for al in a.get("aliases", []):
            con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) VALUES(?,?)",
                        (aid, al))
        set_english_from_kana(con, aid)       # give her a romaji English name if she lacks one
        if a.get("face"):
            row = con.execute("SELECT image_blob IS NOT NULL FROM actresses WHERE id=?",
                              (aid,)).fetchone()
            if row and not row[0]:
                img = _download(a["face"])                # CDN — open, no cookie needed
                if img:
                    con.execute("UPDATE actresses SET image_blob=?, "
                                "image_url=COALESCE(image_url,?) WHERE id=?",
                                (sqlite3.Binary(img), a["face"], aid))


def scan_fc2db(con, codes, cookie, ua, thumb_dir=None, progress=None):
    """Mass-scrape fc2ppv-db for a list of owned codes using the user's browser session."""
    p = progress if progress is not None else PROGRESS
    p.update(running=True, done=0, total=len(codes), hits=0, code="")
    try:
        for i, code in enumerate(codes, 1):
            p["code"] = code
            try:
                data = fetch_fc2ppvdb(code, cookie=cookie, ua=ua)
                if data.get("title_jp") or data.get("actresses"):
                    _apply_fc2db(con, code, data)
                    p["hits"] += 1
                    con.commit()
            except Exception:
                pass
            p["done"] = i
            time.sleep(0.4)          # gentle, but fast enough to finish inside a session's life
    finally:
        p["running"] = False
    return p


# ---- orchestrator ----------------------------------------------------------
def scan_code(con, code, force=False):
    """Enrich one code. Returns dict of fields actually changed."""
    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                "VALUES(?,1,'available','fs')", (code,))
    row = con.execute("SELECT title,title_en,release_date,cover_url FROM works "
                      "WHERE code=?", (code,)).fetchone()
    title_jp, title_en, rel, cover = (row or ("", "", "", ""))
    num = _digits(code)
    changed = {}
    raw_en, raw_jp = None, None            # untidied text, for tag harvesting

    if num and (force or not title_en):
        t = fetch_123av_title(num)
        if t:
            raw_en = t
            clean = tidy_title(t)
            if clean and clean != title_en:
                con.execute("UPDATE works SET title_en=? WHERE code=?", (clean, code))
                title_en = clean
                changed["title_en"] = clean

    if force or not title_jp or not cover or not rel:
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
        if con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, t)).rowcount:
            added.append(t)
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
            tagrows += con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)",
                                    (code, t)).rowcount
        _fill_actress_from_title(con, code, jp, en)
        cj, ce = tidy_title(jp), tidy_title(en)
        if cj != (jp or "") or ce != (en or ""):
            con.execute("UPDATE works SET title=?, title_en=? WHERE code=?",
                        (cj, ce, code))
            retitled += 1
    con.commit()
    return tagrows, retitled


# ---- standalone / library sweep -------------------------------------------
PROGRESS = {"running": False, "done": 0, "total": 0, "hits": 0, "code": ""}


def enrich_actress(con, actress_id, force=False, dry=False, library_codes=None):
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

    prof, guru_url, guru_codes, tried = {}, None, set(), []
    for nm in names:
        tried.append(nm)
        if not prof:
            prof = fetch_javdatabase(nm)
        if not guru_codes:
            guru_url, guru_codes = fetch_javguru_codes(nm)
        if prof and guru_codes:
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


def scan_library(codes, force=False, progress=None):
    p = progress if progress is not None else PROGRESS
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    p.update(running=True, done=0, total=len(codes), hits=0, code="")
    try:
        for i, code in enumerate(codes, 1):
            try:
                ch = scan_code(con, code, force=force)
                if ch:
                    p["hits"] += 1
            except Exception:
                pass
            p["done"] = i
            p["code"] = code
            time.sleep(0.4)
    finally:
        con.close()
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
