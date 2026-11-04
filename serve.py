#!/usr/bin/env python3
"""
serve.py — "Porn-Plex": a local, filterable, visual web UI over the FC2 library.

The FILESYSTEM is the spine (every video folder becomes a card); db/catalog.db is
an enrichment overlay (actress / age / measurements / portrait when known). Poster
frames are generated on demand from the videos themselves with ffmpeg and cached.

Endpoints
  GET /                     -> the single-page app
  GET /api/data             -> full collection index (items + actresses + facets)
  GET /api/meta?codes=a,b   -> batch ffprobe meta (duration/res/vcodec), lazy+cached
  GET /thumb/<code>.jpg     -> generated poster frame (lazy, cached to cache/thumbs)
  GET /portrait/<aid>.jpg   -> actress portrait from catalog image_blob
  GET /video/<code>         -> stream the primary video file (HTTP Range supported)
  POST /api/open            -> open a code's file in the Mac default player (localhost)
  POST /api/reveal          -> reveal a code's file in Finder (localhost)
  GET /api/rescan           -> rebuild the filesystem index

Bind: 0.0.0.0 — reachable from the LAN and over the WireGuard VPN. There is NO auth
and no TLS yet, and /api/open and /api/reveal act on the host Mac's GUI, so anyone who
can reach the port can drive them. HTTPS + logins are the next step.
No client-supplied paths ever touch disk; clients reference movies by CODE, resolved
against the server-built index.

Usage:  python3 serve.py [--port 8730] [--library "/path/to/library"]
"""
import argparse
import atexit
import errno
import gzip
import hashlib
import json
import socket
import ipaddress
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
RENAMERS = os.path.dirname(HERE)
sys.path.insert(0, RENAMERS)   # dev layout: scan.py is in the parent
sys.path.insert(0, HERE)       # release layout: scan.py sits next to serve.py
try:
    import scan as scanmod
except Exception:
    scanmod = None
try:
    import organize as orgmod
except Exception:
    orgmod = None
try:
    import dedupe as dedupemod
except Exception:
    dedupemod = None
import vault_model
import vault_service
# Writable data dir. Dev / portable-folder use keeps everything next to serve.py (HERE).
# A macOS .app (read-only in /Applications) sets VAULT_DATA to a user folder
# (~/Library/Application Support/Vault) so cache/config/DB land somewhere writable.
VAULT_VERSION = "1.7.0"
DATA_DIR = os.environ.get("VAULT_DATA") or HERE
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    pass


# ---- per-user state (favorites / watched / ratings / favorite actresses) ----
# Kept in a sidecar JSON next to the writable data dir, deliberately SEPARATE from
# catalog.db so it survives a catalog rebuild/reimport (played stats must be permanent).
USERDATA_FILE = os.path.join(DATA_DIR, "userdata.json")
_USERDATA = None


def _load_userdata():
    global _USERDATA
    if _USERDATA is None:
        try:
            with open(USERDATA_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        _USERDATA = {
            "fav": list(dict.fromkeys(str(x) for x in (d.get("fav") or []))),
            "seen": list(dict.fromkeys(str(x) for x in (d.get("seen") or []))),
            "rating": {str(k): int(v) for k, v in (d.get("rating") or {}).items() if v},
            "afav": list(dict.fromkeys(str(x) for x in (d.get("afav") or []))),
        }
    return _USERDATA


def _save_userdata():
    try:
        tmp = USERDATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_USERDATA, f, ensure_ascii=False)
        os.replace(tmp, USERDATA_FILE)
    except Exception:
        pass


def _find_db():
    # a DB already in the writable data dir wins
    wd = os.path.join(DATA_DIR, "catalog.db")
    if os.path.exists(wd):
        return wd
    # else locate a preloaded/bundled catalog (release ./data, dev ../db)
    for p in (os.path.join(HERE, "data", "catalog.db"),
              os.path.join(RENAMERS, "db", "catalog.db")):
        if os.path.exists(p):
            if DATA_DIR != HERE:            # app mode: seed the writable copy once
                try:
                    import shutil
                    shutil.copy2(p, wd)
                    return wd
                except Exception:
                    return p
            return p
    return wd


DB_PATH = _find_db()
CACHE = os.path.join(DATA_DIR, "cache")
THUMB_DIR = os.path.join(CACHE, "thumbs")
PROBE_CACHE = os.path.join(CACHE, "probe.json")
INDEX_CACHE = os.path.join(CACHE, "index.json")
SEEN_CACHE = os.path.join(CACHE, "seen.json")   # code -> epoch first seen by Vault ("date added")
SOURCES_CFG = os.path.join(DATA_DIR, "sources.json")

# The scraper registry shown in Settings. The 'builtin' ones are wired into scan.py and the
# enabled flag is honoured there (see enabled_sources). A custom entry is stored and shown
# only — there is no generic title fetch, despite what this comment used to claim.
DEFAULT_SOURCES = [
    {"name": "123av", "url": "https://123av.com/en/v/fc2-ppv-{num}", "provides": "English title",
     "builtin": True, "enabled": True},
    {"name": "ffjav", "url": "https://ffjav.com/?s={code}", "provides": "JP title, cover, date",
     "builtin": True, "enabled": True},
    {"name": "javdatabase", "url": "https://www.javdatabase.com/idols/{slug}/",
     "provides": "actress body info + JP name", "builtin": True, "enabled": True},
    {"name": "jav.guru", "url": "https://jav.guru/actress/{slug}/",
     "provides": "actress filmography (verification + wishlist)", "builtin": True, "enabled": True},
    {"name": "fc2db.net", "url": "https://fc2db.net/work/{num}/",
     "provides": "cover, JP title, date, seller, cast (fills gaps fc2ppv-db leaves)",
     "builtin": True, "enabled": True},
]


def enabled_sources():
    """Names of the scrapers Settings has ticked. scan.py takes this set and skips the rest
    — until now the boxes were saved to sources.json and never read by anything."""
    return {s.get("name") for s in _load_sources() if s.get("enabled")}


def _load_sources():
    try:
        with open(SOURCES_CFG) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            # A saved list predates any built-in added since; without this merge a new
            # source would be missing — and therefore OFF — for every existing install.
            have = {d.get("name") for d in data if isinstance(d, dict)}
            for d in DEFAULT_SOURCES:
                if d["name"] not in have:
                    data.append(dict(d))
            return data
    except Exception:
        pass
    return [dict(s) for s in DEFAULT_SOURCES]


def _save_sources(sources):
    try:
        with open(SOURCES_CFG, "w") as f:
            json.dump(sources, f, indent=2)
        return True
    except Exception:
        return False


# ---------------------------------------------- FlareSolverr helpers
_SOLVER_PROBE = {"url": "", "at": 0.0, "ok": False}   # cached reachability probe
# one thread builds /api/data; the others wait for it instead of walking the share too
_DATA_BUILD_LOCK = threading.Lock()
_INDEX_LOCK = threading.Lock()     # guards the one-step publish of STATE and the sequence
_INDEX_SEQ = 0                     # increases per rebuild; an older result never overwrites
_JOB_LOCK = threading.Lock()       # claims the single shared scan slot


def claim_scan(label):
    """Reserve the shared scan slot FROM THE REQUEST THREAD.

    The handlers used to test PROGRESS["running"] and only set it inside the worker they
    then started, so two quick clicks both passed the test and two scrapers wrote to SQLite
    through the same progress dict."""
    if not scanmod:
        return False
    with _JOB_LOCK:
        if scanmod.PROGRESS.get("running"):
            return False
        scanmod.PROGRESS.update(running=True, done=0, total=0, hits=0, error="", code=label)
        return True


def release_scan():
    if scanmod:
        scanmod.PROGRESS["running"] = False


def _is_private_url(url):
    """True when the host resolves to loopback/private space. The resolver is a LAN
    service; allowing a public URL would turn this no-auth server into a relay."""
    try:
        host = urlparse(url).hostname or ""
        if not host:
            return False
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if not (ip.is_private or ip.is_loopback or ip.is_link_local):
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------- secret store (session cookie etc.)
# Prefer the macOS Keychain (real encryption at rest). Fall back to a 0600 file only if
# `security` is unavailable, and flag it as unencrypted so the UI can warn.
_KC_SERVICE = "vault-fc2ppv-db"
_SECRETS_FILE = os.path.join(CACHE, "secrets.json")


def _kc_available():
    return sys.platform == "darwin" and bool(__import__("shutil").which("security"))


def secret_set(account, value):
    if _kc_available():
        subprocess.run(["security", "delete-generic-password", "-s", _KC_SERVICE,
                        "-a", account], capture_output=True)
        if not value:
            return True
        r = subprocess.run(["security", "add-generic-password", "-s", _KC_SERVICE,
                            "-a", account, "-w", value, "-U"], capture_output=True)
        return r.returncode == 0
    # fallback: 0600 file
    try:
        d = {}
        if os.path.exists(_SECRETS_FILE):
            d = json.load(open(_SECRETS_FILE))
        if value:
            d[account] = value
        else:
            d.pop(account, None)
        os.makedirs(CACHE, exist_ok=True)
        with open(_SECRETS_FILE, "w") as f:
            json.dump(d, f)
        os.chmod(_SECRETS_FILE, 0o600)
        return True
    except Exception:
        return False


def secret_get(account):
    if _kc_available():
        r = subprocess.run(["security", "find-generic-password", "-s", _KC_SERVICE,
                            "-a", account, "-w"], capture_output=True, text=True)
        return r.stdout.rstrip("\n") if r.returncode == 0 else None
    try:
        return json.load(open(_SECRETS_FILE)).get(account)
    except Exception:
        return None


# ---------------------------------------------------- tag translation (JP -> EN)
# A raw tag (often Japanese, e.g. from fc2ppv-db) maps to an English display label.
# Stored in catalog.db (tag_translations) and editable in Settings.
_TAG_SEED = {
    "20歳": "20 Years Old", "23歳": "23 Years Old", "2発射": "2 Ejaculations",
    "2連続射精": "2 Ejaculations", "2連続発射": "2 Ejaculations", "3P": "Threesome",
    "3射精": "3 Ejaculations", "7射精": "7 Ejaculations", "Dカップ": "D Cup", "Eカップ": "E Cup",
    "Fカップ": "F-cup", "Gカップ": "G Cup", "JD": "College Girl", "M女": "Submissive",
    "OL": "Office Lady", "P活": "Sugar Dating", "SSS級": "SSS-Class", "S級": "S-Class",
    "S級美女": "S-Class Beauty", "Y字バランス": "Y-Balance", "あどけない": "Innocent Looking", "うぶ": "Naive",
    "おしっこ": "Peeing", "おっぱい": "Boobs", "おもちゃ責め": "Toy Play", "お嬢様": "Princess Type", "お宝": "Gem",
    "お尻": "Butt", "お掃除フェラ": "Cleanup Blowjob", "お風呂": "Bath", "かわいい": "Cute", "がりがり": "Skinny",
    "くすぐり": "Tickling", "くぱぁ": "Spread", "くびれ": "Slim Waist", "ごっくん": "Cum Swallow",
    "ご奉仕": "Devoted Service", "すっぴん": "No Makeup", "たれ目": "Droopy Eyes", "ちっぱい": "Small boobs",
    "ちんこビンタ": "Cock Slapping", "ぬるぬる": "Slippery", "ぴえん": "Teary", "ぶっかけ": "Bukkake",
    "ぽっちゃり": "Chubby", "まんぐりがえし": "Mating Press", "むすめガチャ": "Musume Gacha", "むっちり": "Plump",
    "ろり": "Loli", "アイドル": "Idol", "アクメ": "Orgasm", "アスリート": "Athlete", "アナウンサー": "Announcer",
    "アナル": "Anal", "アナルATM": "Anal", "アナルパール": "Anal Beads", "アナル拡張": "Anal Stretching",
    "アナル舐め": "Anal Licking", "アナル貫通": "Anal Penetration", "アナル貫通ATM": "Anal Penetration",
    "アナル責め": "Anal Play", "アニコス": "Anime Cosplay", "アニメ": "Anime", "アニメ声": "Anime Voice",
    "アパレル": "Apparel Worker", "アヘ顔": "Ahegao", "イキ": "Orgasm", "イキまくり": "Multiple Orgasms",
    "イキまくる": "Multiple Orgasms", "イチャイチャ": "Lovey-Dovey", "イチャラブ": "Lovey-Dovey",
    "イマラチオ": "Deepthroat", "イラストレーター": "Illustrator", "イラマ": "Deepthroat", "イラマチオ": "Deep Throat",
    "インテリ": "Intellectual", "ウォシュレット型アナル舐め": "Anal Licking", "ウブ": "Naive", "エステ": "Spa",
    "エロ": "Erotic", "エロい": "Sexy", "エロマッサージ": "Erotic Massage", "エロ下着": "Sexy Lingerie",
    "オイル": "Oil", "オナニー": "Masturbation", "オナホ": "Onahole", "オモチャ": "Toys", "オリジナル": "Original",
    "カフェ店員": "Cafe Worker", "カメラマンあり": "Cameraman Present", "カーセックス": "Car Sex",
    "ガチイキ": "Real Orgasm", "キス": "Kiss", "ギャップ": "Gap Moe", "ギャル": "Gal", "クスコ": "Speculum",
    "クズの本懐": "Scums Wish", "クリスマス": "Christmas", "クリトリス": "Clitoris", "クリ責め": "Clit Play",
    "クンニ": "Cunnilingus", "クール": "Cool", "グラビア": "Gravure", "グラビアアイドル": "Gravure Idol",
    "コスプレ": "Cosplay", "ザーメン": "Semen", "シックスナイン": "69", "シャイ": "Shy", "シャワー": "Shower",
    "ショートカット": "Shortcuts", "ショートヘア": "Short Hair", "ショートボブ": "Short Bob", "ジム": "Gym",
    "スク水": "School Swimsuit", "スジマン": "Cameltoe", "スタイル": "Great Style", "スタイル抜群": "Great Style",
    "ストッキング": "Stockings", "スポブラ": "Sports Bra", "スポーツ": "Sports", "スポーツジム": "Gym", "スリム": "Slim",
    "スレンダー": "Slender", "スレンダーボディ": "Slender Body", "スレンダー美人": "Slender Beauty", "スーツ": "Suit",
    "スーツ女子": "Suit Girl", "セクシー": "Sexy", "セックスレス": "Sexless", "ソフトSM": "Soft SM",
    "ダブルフェラ": "Double Blowjob", "ダブル中出し": "Double Creampie", "ダンサー": "Dancer",
    "ツインテール": "Twintails", "ツンデレ": "Tsundere", "ディルド": "Dildo", "デカクリ": "Big Clit",
    "デカチン": "Big Cock", "デカ乳首": "Big Nipples", "デカ尻": "Big Ass", "デビュー作": "Debut Work",
    "デリヘル": "Delivery Health", "デンマ": "Magic Wand", "デート": "Date", "トー横": "Toyoko Kids",
    "ドM": "Masochist", "ドS": "Sadist", "ナマハメ": "Bareback", "ナンパ": "Pickup",
    "ナンパ師": "Pickup Artist", "ナース": "Nurse", "ニーソ": "Knee Socks", "ニーハイ": "Knee-High Socks",
    "ネトリ": "NTR", "ノーカット": "Uncut", "ノーハンドフェラ": "No-Hands Blowjob", "ノーパン": "No Panties",
    "ノーブラ": "No Bra", "ノーマル": "Normal", "ハメ撮り": "POV", "ハメ潮": "Squirting While Fucked",
    "ハーフ": "Half-Japanese", "ハーレム": "Harem", "バイト": "Part-Timer", "バイブ": "Vibrator",
    "バキュームフェラ": "Vacuum Blowjob", "バック": "Doggy Style", "パイズリ": "Titjob", "パイパン": "Shaved",
    "パジャマ": "Pajamas", "パンスト": "Pantyhose", "パンチラ": "Panties Peeking Out", "パンツ": "Panties",
    "パンツチェック": "Panty Check", "パンティ": "Panties", "ピュア": "Pure", "ピル無し": "No Pill",
    "ピンク乳首": "Pink Nipples", "ピンク色": "Pink", "ピンク色マンコ": "Pink Pussy", "フェチ": "Fetish",
    "フェラ": "Blowjob", "フェラチオ": "Blowjob", "フェラ抜き": "Blowjob to Completion", "ブルマ": "Bloomers",
    "プライベート": "Private", "ホテル": "Hotel", "ボブ": "Bob Cut", "ボーイッシュ": "Boyish", "ポニーテール": "Ponytail",
    "マイクロビキニ": "Micro Bikini", "マッサージ": "Massage", "マッチングアプリ": "Dating App", "マン毛": "Pubic Hair",
    "ミニマム": "Petite", "ムチムチ": "Voluptuous", "メイド": "Maid", "メイドコス": "Maid Cosplay",
    "メガネ": "Glasses", "メンエス": "Men's Massage", "メンエス嬢": "Massage Girl", "メンズエステ": "Men's Massage",
    "メンヘラ": "Unstable", "モザイク": "Mosaic", "モザイクなし": "Uncensored", "モデル": "Model",
    "モデル体型": "Model Figure", "モ無": "Uncensored", "モ無し": "Uncensored", "ヤラセなし": "No Fakery",
    "ヤリマン": "Slut", "ランジェリー": "Lingerie", "リアル": "Real", "ルーズソックス": "Loose Socks", "レズ": "Lesbian",
    "レンタル彼女": "Rental Girlfriend", "レースクイーン": "Race Queen", "ロケットオッパイ": "Rocket Tits",
    "ロシア": "Russian", "ロリ": "Loli", "ロング": "Long Hair", "ロングヘア": "Long Hair",
    "ロングヘアー": "Long Hair", "ローション": "Lotion", "ローター": "Egg Vibrator", "一本撮り": "One-Take",
    "一本筋": "Slit", "上品": "Elegant", "上玉": "Top Class", "下着": "Lingerie", "不倫": "Affair",
    "不思議ちゃん": "Quirky", "中 出し": "Creampie", "中イキ": "Vaginal Orgasm", "中出": "Creampie",
    "中出し": "Creampie", "中出し2発": "Double Creampie", "中逝き": "Internal Orgasm", "串刺し": "Skewered",
    "主婦": "Housewife", "主観": "POV", "乱交": "Orgy", "乳揉み": "Breast Fondling", "乳首": "Nipples",
    "乳首舐め": "Nipple Licking", "乳首責め": "Nipple Play", "人妻": "Married Woman", "低身長": "Petite",
    "体操着": "Gym Clothes", "保育士": "Nursery Teacher", "個人摄影": "Personal Shoot",
    "個人撮影": "Personal Shoot", "個撮": "Personal Shoot", "借金": "Debt", "元アイドル": "Former Idol",
    "元裏垢男子": "Ex Secret Account Guy", "先生": "Teacher", "処女": "Virgin", "出会い系": "Dating Site",
    "初": "First Time", "初アナル": "First Anal", "初中出し": "First Creampie", "初体験": "First Experience",
    "初撮": "Debut", "初撮り": "Debut", "初撮影": "First Shoot", "初物": "First Time",
    "初裏": "First Uncensored", "制服": "Uniform", "剃り残し": "Stubble", "剃毛": "Shaving",
    "剛毛": "Coarse hair", "半外半中": "Pull Out Then In", "危険日": "Fertile Day", "即ハメ": "Instant Sex",
    "即尺": "Instant Blowjob", "受付嬢": "Receptionist", "口内射精": "Mouth Ejaculation",
    "口内発射": "Cum in Mouth", "可愛い": "Cute", "台湾": "Taiwanese", "号泣": "Sobbing", "同人AV": "Doujin AV",
    "名器": "Excellent Pussy", "吸うやつ責め": "Clit Sucker", "吸うヤツ": "Clit Sucker", "唾液": "Saliva",
    "喘ぎ": "Moaning", "喘ぎ声": "Moaning Voice", "喪失": "Deflowering", "固定カメラ": "Fixed Camera",
    "地下アイドル": "Underground Idol", "地元": "Local", "地味": "Plain", "地味系": "Plain", "地方": "Rural",
    "坂道系": "Idol Lookalike", "声優": "Voice Actress", "声我慢": "Silent Sex", "売り切れ終了": "Sold Out",
    "変態": "Pervert", "外出し": "Pull Out", "大量中出し": "Massive Creampie", "天使": "Angel",
    "天然": "Airhead", "天然Fカップ": "Natural F Cup", "天真爛漫": "Cheerful", "太もも": "Thighs", "奥さん": "Wife",
    "奥様": "Wife", "女の本性": "True Nature", "女子アナ": "Female Announcer", "女子大生": "College Girl",
    "女教師": "Female Teacher", "妊娠": "Pregnancy", "妊婦": "Pregnant", "妊活": "Trying to Conceive",
    "妻": "Wife", "子持ち": "Has Kids", "子種配り": "Impregnation", "孕ませ": "Impregnation", "学生": "Student",
    "完全オリジナル": "Fully Original", "家出": "Runaway", "家庭教師": "Private Tutor", "密着": "Close Contact",
    "寝取られ": "Cuckold", "寝取り": "NTR", "射精": "Ejaculation", "小柄": "Petite", "小顔": "Small Face",
    "就活生": "Job Hunter", "尻": "Ass", "尻フェチ": "Ass Fetish", "局部アップ": "Close-up",
    "山芋責め": "Yam Torture", "巨乳": "Big Tits", "巨乳・美乳": "Big Beautiful Tits", "巨乳人妻": "Busty Wife",
    "巨尻": "Big Ass", "巨根": "Big Cock", "座位": "Sitting Position", "彼女": "Girlfriend",
    "彼氏持ち": "Has Boyfriend", "後輩": "Junior", "従順": "Obedient", "微乳": "Tiny Tits",
    "恥じらい": "Bashful", "恥ずかしがり屋": "Shy", "愛嬌": "Charming", "愛嬌抜群": "Very Charming",
    "感度": "Sensitive", "感度抜群": "Very Sensitive", "手コキ": "Handjob", "手マン": "Fingering",
    "拘束": "Restraint", "挟射": "Titjob Cumshot", "接写": "Close-up", "推しの素人": "Favorite Amateur",
    "援交": "Compensated Dating", "撮りおろし": "Exclusive", "撮り下ろし": "Exclusive Photoshoot",
    "放尿": "Peeing", "敏感": "Sensitive", "教え子": "Student", "教師": "Teacher", "新人": "Newcomer",
    "新卒": "New Graduate", "方言": "Dialect", "日焼け": "Suntanned", "昇天": "Climax", "晒し": "Exposed",
    "暴発": "Accidental discharge", "有毛": "Hairy", "未処理": "Unshaved", "未経験": "Inexperienced",
    "本物": "Genuine", "本番": "Real Sex", "本編": "Main Feature", "本編顔出し": "Face Shown",
    "杭打ち騎乗位": "Piston Cowgirl", "欲求不満": "Frustrated", "正常位": "Missionary",
    "正統派美女": "Classic Beauty", "歯科助手": "Dental Assistant", "残りわずか": "Almost Sold Out",
    "殿堂入り": "Hall of Fame", "毛あり": "With hair", "毛有": "Hairy", "毛有り": "Hairy", "水泳": "Swimming",
    "水泳部": "Swim Club", "水着": "Swimsuit", "汗だく": "Sweaty", "洗体": "Body Wash", "浮気": "Cheating",
    "浴衣": "Yukata", "海外": "Foreign", "淫乱": "Nympho", "清楚": "Innocent", "清楚系": "Prim and Proper",
    "清純": "Innocent", "温泉": "Hot Spring", "潮": "Squirting", "潮吹き": "Squirting",
    "激カワ": "Super Cute", "濃厚フェラ": "Deep Blowjob", "無": "Uncensored", "無〇可": "Uncensored",
    "無修正": "Uncensored", "無垢": "Pure", "無毛": "Hairless", "焦らし": "Teasing", "熟女": "Mature",
    "爆乳": "Huge Tits", "爆乳人妻": "Huge-Breasted Wife", "特典": "Bonus", "特典あり": "With Bonus",
    "特典有り": "With Bonus", "特別版": "Special Edition", "狭膣": "Tight Pussy", "猫耳": "Cat Ears",
    "玉舐め": "Ball Licking", "玩具": "Toys", "生": "Bareback", "生えっち": "Bareback", "生ハメ": "Bareback",
    "生中": "Raw Creampie", "生中出し": "Raw Creampie", "生姜": "Ginger", "生姜責め": "Ginger Torture",
    "生挿入": "Raw Insertion", "田舎": "Rural", "痙攣": "Convulsion", "痴女": "Slut", "白人": "White",
    "盗撮": "Voyeur", "目隠し": "Blindfold", "看護師": "Nurse", "真面目": "Serious", "眼鏡": "Glasses",
    "着衣": "Clothed", "神スタイル": "Amazing Style", "神乳": "Amazing Tits", "神作": "Masterpiece",
    "禁断": "Forbidden", "秋のキノコ狩り": "Autumn Mushroom Hunting", "秘密": "Secret", "秘蔵": "Treasured",
    "種付け": "Breeding", "立ちバック": "Standing Doggy", "童顔": "Baby Face", "笑顔": "Smile",
    "筋肉": "Muscular", "純無垢": "Pure", "純粋": "Pure", "素人": "Amateur",
    "素人個撮": "Amateur Personal Shoot", "素朴": "Plain", "素股": "Intercrural", "細い": "Slim",
    "細身": "Slim", "絶叫": "Screaming", "絶品": "Superb", "絶頂": "Orgasm", "絶頂寸止め": "Edging",
    "網タイツ": "Fishnets", "綺麗": "Beautiful", "緊縛": "Bondage", "締め付け": "Tight",
    "美ボディ": "Beautiful Body", "美マン": "Beautiful Pussy", "美乳": "Beautiful Tits", "美人": "Beauty",
    "美人妻": "Beautiful Wife", "美人局": "Badger Game", "美女": "Beautiful Woman",
    "美少女": "Beautiful Girl", "美尻": "Nice Butt", "美巨乳": "Beautiful Big Tits",
    "美熟女": "Beautiful Mature", "美白": "Fair Skin", "美肌": "Beautiful Skin", "美脚": "Beautiful Legs",
    "美裸体": "Beautiful Body", "羞恥": "Humiliation", "肉便器": "Cum Dump", "背徳感": "Immoral",
    "背面騎乗位": "Reverse Cowgirl", "腋毛": "Armpit Hair", "腹筋": "Abs", "膣内カメラ": "Internal Camera",
    "膣内ファイバースコープ": "Internal Endoscope", "膣内射精": "Internal Ejaculation",
    "膣内撮影": "Internal Filming", "自宅": "At Home", "自撮り": "Selfie", "色白": "Fair Skin", "芋": "Plain",
    "芸能": "Showbiz", "芸能人": "Celebrity", "若妻": "Young Wife", "華奢": "Petite", "薄モ": "Light Mosaic",
    "薄毛": "Thin Hair", "藻無し": "Uncensored", "融資": "Loan", "裏垢男子": "Secret Account Guy",
    "複数": "Multiple", "複数プレイ": "Multiple Partners", "褐色": "Tanned", "調教": "Training",
    "講習": "Training Session", "貧乳": "Small Tits", "貧乳美少女": "Small-Breasted Beauty",
    "足コキ": "Footjob", "足舐め": "Foot Licking", "身バレ": "Face Exposed", "車内": "In Car",
    "軟体": "Flexible", "転載対策済み": "Anti-Piracy", "近所": "Neighbor", "返済": "Repayment",
    "追撃中出し": "Second Creampie", "逆3P": "Reverse Threesome", "逆さ": "Reverse",
    "逆ナン": "Reverse Pickup", "透明感": "Translucent Skin", "連続": "Continuous",
    "連続イキ": "Continuous Orgasm", "連続中出し": "Continuous Creampie", "連続絶頂": "Continuous Orgasm",
    "運動系": "Sporty", "過去１": "Best Ever", "配信者": "Streamer", "野外": "Outdoor",
    "野外フェラ": "Outdoor Blowjob", "野外露出": "Outdoor Nudity", "野菜責め": "Vegetable Play", "金髪": "Blonde",
    "長身": "Tall", "長髪": "Long Hair", "関西": "Kansai", "関西弁": "Kansai Dialect", "限定": "Limited",
    "陥没乳首": "Inverted Nipples", "陰毛": "Pubic Hair", "陰毛あり": "Has Pubic Hair", "電マ": "Magic Wand",
    "電話しながら": "While on Phone", "露出": "Exposure", "露天風呂": "Open-Air Bath", "青姦": "Outdoor Sex",
    "面接": "Interview", "顔なし": "No Face", "顔出し": "Face Shown", "顔射": "Facial",
    "顔面騎乗": "Face Sitting", "顔面騎乗位": "Face Sitting", "風俗": "Sex Work", "飲精": "Cum Swallow",
    "駅弁": "Standing Carry", "駅弁ファック": "Standing Carry", "騎乗位": "Cowgirl", "高品質": "High Quality",
    "高学歴": "Highly Educated", "高画質": "High Quality", "高身長": "Tall", "鬼イカセ": "Forced Orgasm",
    "鬼畜": "Brutal", "黃金比": "Golden Ratio", "黒ギャル": "Black Gal", "黒乳首": "Dark Nipples",
    "黒人": "Black", "黒髪": "Black Hair", "黒髪ロング": "Long Black Hair", "１日撮り": "One-Day Shoot",
    "１発撮り": "One-Take", "１８才": "18 Years Old", "１８歳": "18 Years Old", "１９歳": "19 Years Old",
    "２射精": "2 Ejaculations", "７射精": "7 Ejaculations", "Ｃカップ": "C Cup", "Ｈカップ": "H Cup",
    "Ｗフェラ": "Double Blowjob", "ﾚﾋﾞｭｰ特典": "Review Bonus"
}
_tagtx = None


def _ensure_tagtx(con):
    con.execute("CREATE TABLE IF NOT EXISTS tag_translations "
                "(raw TEXT PRIMARY KEY COLLATE NOCASE, display TEXT)")
    if not con.execute("SELECT 1 FROM tag_translations LIMIT 1").fetchone():
        con.executemany("INSERT OR IGNORE INTO tag_translations(raw,display) VALUES(?,?)",
                        list(_TAG_SEED.items()))
        con.commit()


def _load_tagtx():
    """raw(lower) -> display, cached. None until first load."""
    global _tagtx
    if _tagtx is None:
        _tagtx = {}
        if os.path.exists(DB_PATH):
            try:
                con = sqlite3.connect(DB_PATH)
                _ensure_tagtx(con)
                _tagtx = {r[0].lower(): r[1] for r in
                          con.execute("SELECT raw,display FROM tag_translations")
                          if r[1]}
                con.close()
            except Exception:
                _tagtx = {}
    return _tagtx


def _tag_display(tag):
    """Safety net: if a raw Japanese tag ever slips through un-canonicalized, still show its
    English. After canonicalization the stored tag is already the display value."""
    return _load_tagtx().get((tag or "").lower(), tag)


def _is_jp(s):
    return bool(re.search(r"[぀-ヿ一-鿿]", s or ""))


def canon_tag(tag, jp2en=None):
    """The canonical form a tag is STORED under: its English label if it has one, else the
    tag itself (a Japanese tag with no English mapping stays Japanese)."""
    jp2en = jp2en if jp2en is not None else _load_tagtx()
    return jp2en.get((tag or "").lower(), tag)


def _canonicalize_tags(con):
    """One-time (idempotent) migration: rewrite every stored tag to its canonical English
    where a mapping exists, merging duplicates per movie (中出し + Creampie -> one Creampie)."""
    tx = {r[0].lower(): r[1] for r in con.execute("SELECT raw,display FROM tag_translations")
          if r[1]}
    changed = 0
    for (tag,) in con.execute("SELECT DISTINCT tag FROM tags").fetchall():
        en = tx.get((tag or "").lower())
        if en and en != tag:
            con.execute("UPDATE OR IGNORE tags SET tag=? WHERE tag=?", (en, tag))
            con.execute("DELETE FROM tags WHERE tag=?", (tag,))
            changed += 1
    if changed:
        con.commit()
    return changed


_seen = None


def _load_seen():
    """code -> first-seen epoch. This is the truthful 'date added', unlike file mtime
    (which copies preserve from the source and does not reflect when we got the file)."""
    global _seen
    if _seen is None:
        try:
            with open(SEEN_CACHE) as f:
                _seen = json.load(f)
        except Exception:
            _seen = {}
    return _seen


def _save_seen():
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = SEEN_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_seen, f)
        os.replace(tmp, SEEN_CACHE)
    except Exception:
        pass


def _stamp_added(items):
    """Assign each item an 'added' timestamp = when Vault first saw its code."""
    seen = _load_seen()
    now = int(time.time())
    changed = False
    # first ever run: seed from mtime so pre-existing files keep a sensible relative order
    # instead of collapsing to one instant. EXCEPTION: a folder still carrying a "<CODE> -
    # Name" suffix hasn't been organized yet, i.e. it's a fresh drop — and its file mtime is
    # unreliable anyway (copies preserve the source's old timestamp). Seed those as 'now' so
    # newly-added movies sort to the top of "recently added" even before Organize runs.
    # After the first build, any newly-discovered code is genuinely new -> 'now'.
    seeding = not seen
    for it in items:
        c = it["code"]
        if c not in seen:
            if seeding:
                folder = os.path.basename((it.get("rel") or "").rstrip("/"))
                fresh = bool(_name_from_folder(folder))
                seen[c] = now if fresh else min(it.get("mtime") or now, now)
            else:
                seen[c] = now
            changed = True
        it["added"] = seen[c]
    if changed:
        _save_seen()
DEFAULT_LIBRARY = ""   # no personal path baked in; set via config.json / --library / VAULT_LIBRARY

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".webm", ".flv",
             ".m4v", ".rmvb", ".mpg", ".mpeg"}

os.makedirs(THUMB_DIR, exist_ok=True)

# ---------------------------------------------------------------- global state
STATE = {
    "library": DEFAULT_LIBRARY,
    "download": "",       # optional import/staging folder (downloads land here)
    "items": [],          # list of item dicts (the collection)
    "by_code": {},        # code -> item   (metadata: one catalog row per code)
    "by_uid": {},         # uid  -> item   (files: one folder per uid)
    "actresses": [],      # list of actress dicts (with portrait flag)
    "facets": {},
    "scanned_at": 0,
}


def _model_info(fetch_latest=False):
    """The scene model, for Settings → Scene model: what is installed, what the newest release
    is (only asked for when the tab opens: fetch_latest), and the running download."""
    d = vault_model.user_models_dir()
    home = os.path.expanduser("~")
    if home and d.startswith(home + os.sep):     # the tab shows "~/…", not the account name
        d = "~" + d[len(home):]
    out = {"installed": vault_model.installed(), "dir": d,
           "progress": dict(vault_model.PROGRESS)}
    if fetch_latest:
        out["latest"] = vault_model.latest()
    return out


# Outcome of the most recent filesystem walk. An unmounted share or an unreadable folder
# used to produce a short list that silently replaced a good index.
LAST_WALK = {"ran": False, "errors": 0, "first_error": "", "missing_root": False}

# POST paths that never change the /api/data projection, so they must not throw its cache away
_POST_KEEPS_DATA = frozenset({
    "/api/open", "/api/reveal", "/api/fc2db/test", "/api/scanprogress",
    "/api/solver/check", "/api/fc2dbnet/test",
    "/api/importprogress", "/api/dedupe", "/api/organize", "/api/foldactress",
    "/api/stash", "/api/sourcesave",
})

# progress for the Import pipeline (rename+clean -> migrate/dedupe into library)
IMPORT_PROGRESS = {"running": False, "phase": "", "line": "", "done": 0, "total": 0}
PROBE = {}               # code -> {duration,width,height,vcodec}  (persisted)
PROBE_LOCK = threading.Lock()
PROBE_POOL = ThreadPoolExecutor(max_workers=3)
THUMB_POOL = ThreadPoolExecutor(max_workers=3)
_thumb_inflight = {}
_thumb_lock = threading.Lock()


# ------------------------------------------------------------------- helpers
def load_probe():
    global PROBE
    try:
        with open(PROBE_CACHE, "r") as f:
            PROBE = json.load(f)
    except Exception:
        PROBE = {}
    # Entries with nothing in them are failed probes the old code stored permanently.
    dead = [c for c, m in PROBE.items()
            if not isinstance(m, dict) or (m.get("duration") is None and m.get("width") is None)]
    for c in dead:
        PROBE.pop(c, None)
    if dead:
        sys.stderr.write("[probe] dropped %d failed entries from the cache\n" % len(dead))


def save_probe():
    with PROBE_LOCK:
        tmp = PROBE_CACHE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(PROBE, f)
            os.replace(tmp, PROBE_CACHE)
        except Exception:
            pass


def norm_digits(code):
    m = re.search(r"(\d{5,})", code or "")
    return m.group(1) if m else ""


def norm_code(name):
    """Pull a canonical FC2 code out of a messy folder/file name, else None.
    Handles FC2-PPV-123 / FC2PPV123 / [FC2-PPV-123] title / fc2 ppv 123 / FC2-123 ..."""
    if not name:
        return None
    m = re.search(r"FC2[-_\s]*PPV[-_\s]*(\d{5,10})", name, re.I)
    if not m:
        m = re.search(r"\bFC2[-_\s]*(\d{6,10})\b", name, re.I)
    return "FC2-PPV-" + m.group(1) if m else None


def safe_key(code):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", code)


# --------------------------------------------------------------- library scan
def _nat_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _largest_video(files, dirpath):
    vids = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext in VIDEO_EXT and not f.startswith("._"):
            p = os.path.join(dirpath, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            vids.append((sz, p, ext))
    if not vids:
        return None
    total = sum(v[0] for v in vids)
    # play order = natural filename order (part1, part2, part10, ...); Part 1 = main
    ordered = sorted(vids, key=lambda v: _nat_key(os.path.basename(v[1])))
    primary = ordered[0]                          # main = first part (poster/probe/mtime)
    parts = [{"path": v[1], "name": os.path.basename(v[1]), "size": v[0]}
             for v in ordered]
    return {"primary": primary[1], "ext": primary[2], "size": total,
            "nparts": len(vids), "parts": parts}


# dirs the by-actress folder pass must never move into/out of — and that the library walk
# never descends into. "_needs_review" is a human holding pen for loose, un-coded files: the
# walk used to index it as a single movie whose code was literally "_needs_review", which then
# became a catalog row and a wasted fc2ppv-db lookup on every scan.
FOLD_RESERVED = {"_vault_trash", "_duplicates", "_unsorted", "_vault", "@eadir",
                 "_needs_review"}


def _safe_actress_dir(name):
    """A filesystem-safe single path segment for an actress folder."""
    s = (name or "").strip().replace("/", "-").replace(":", "-").replace("\\", "-")
    return s.strip(" .")


def scan_library(library):
    """Walk the tree; every directory that directly holds video(s) is one item.

    Reports what went wrong in LAST_WALK: os.walk swallows unreadable directories, so a
    half-finished walk is indistinguishable from a smaller library unless we count them."""
    items = []
    LAST_WALK.update(errors=0, first_error="", missing_root=False, ran=True)
    if not os.path.isdir(library):
        LAST_WALK["missing_root"] = True
        return items

    def _walk_err(e):
        LAST_WALK["errors"] += 1
        if not LAST_WALK["first_error"]:
            LAST_WALK["first_error"] = "%s: %s" % (getattr(e, "filename", "?"), e)

    lib_real = os.path.realpath(library)
    for dirpath, dirs, files in os.walk(library, onerror=_walk_err):
        # never descend into reserved folders (trash, duplicates, …) so trashed movies
        # don't get re-scanned back into the library
        dirs[:] = sorted(d for d in dirs if d.lower() not in FOLD_RESERVED)
        vid = _largest_video(files, dirpath)
        if not vid:
            continue
        folder = os.path.basename(dirpath.rstrip("/"))
        fname = os.path.splitext(os.path.basename(vid["primary"]))[0]
        # canonical FC2 code from the folder OR the filename; else keep the folder name
        # ...or the folder name itself, for the non-FC2 codes in the library (N0627,
        # MIFD-711). A name too short to be any code is a stray directory, not a release.
        code = norm_code(folder) or norm_code(fname) or folder
        if len(code) < 3:
            continue
        rel = os.path.relpath(dirpath, library)
        parts = rel.split(os.sep)
        # actress from path: LIBRARY/<Actress>/<CODE>/...  -> parts[0] when nested
        actress_path = None
        if len(parts) >= 2:
            actress_path = parts[0]
        try:
            st = os.stat(vid["primary"])
            mtime = int(st.st_mtime)
        except OSError:
            mtime = 0
        items.append({
            "code": code,
            "digits": norm_digits(code),
            "actress_path": actress_path,
            "primary": vid["primary"],
            "ext": vid["ext"],
            "size": vid["size"],
            "nparts": vid["nparts"],
            "parts": vid["parts"],
            "mtime": mtime,
            "rel": rel,
        })
    _stamp_added(items)
    return items


def _date_epoch(s):
    """'YYYY-MM-DD' -> unix epoch (noon, local), or 0. Used to give wishlist ghosts a
    sortable recency comparable to real file mtimes."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    if not m:
        return 0
    y, mo, d = map(int, m.groups())
    try:
        return int(time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1)))
    except (OverflowError, ValueError):
        return 0


# --------------------------------------------------------------- db enrichment
def enrich(items):
    """Overlay catalog.db metadata onto scanned items; build actress list+facets."""
    by_code = {}
    by_all = {}                       # code -> every on-disk item with that code
    for it in items:
        by_code.setdefault(it["code"], it)
        by_all.setdefault(it["code"], []).append(it)
    actresses = {}
    portraits = set()
    aka_map = {}
    disp_by_id = {}
    wa = {}                       # code -> [actress_id, ...] full cast (multi-actress)

    if os.path.exists(DB_PATH):
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        # actresses + portrait availability
        arows = con.execute(
            "SELECT id,name_en,name_jp,name_kana,age,height,bust,cup,waist,hip,"
            "birthdate,description,fc2ppvdb_uuid,(image_blob IS NOT NULL) AS has_portrait "
            "FROM actresses"
        ).fetchall()
        adict = {}
        for r in arows:
            adict[r["id"]] = r
            if r["has_portrait"]:
                portraits.add(r["id"])
        # ---- display disambiguation: same name, different fc2ppv-db person ----
        # The DB stores clean name_en/name_jp plus a stable fc2ppvdb_uuid. When two
        # actresses share a display name we append a short uuid (or #id) ONLY in the
        # display string, so the by-actress view / modals keep them apart without ever
        # polluting the stored name. See _disp_by_id below.
        _name_ct = {}
        for r in arows:
            base = (r["name_en"] or r["name_jp"] or "").strip().lower()
            if base:
                _name_ct[base] = _name_ct.get(base, 0) + 1
        disp_by_id = {}
        for r in arows:
            base = (r["name_en"] or r["name_jp"] or "").strip()
            if not base:
                continue
            if _name_ct.get(base.lower(), 0) > 1:
                tag = (r["fc2ppvdb_uuid"] or "")[:8] or f"#{r['id']}"
                disp_by_id[r["id"]] = f"{base} ({tag})"
            else:
                disp_by_id[r["id"]] = base
        # tags per code — translated to English display labels where we have a mapping
        _ensure_tagtx(con)
        tagmap = {}
        for r in con.execute("SELECT code,tag FROM tags"):
            disp = _tag_display(r["tag"])
            lst = tagmap.setdefault(r["code"], [])
            if disp not in lst:                     # translations can collapse duplicates
                lst.append(disp)
        # aliases per actress (so a movie is findable by any spelling she's known under)
        aka_map = {}
        for r in con.execute("SELECT actress_id,alias FROM aliases"):
            aka_map.setdefault(r["actress_id"], []).append(r["alias"])
        # multi-actress cast per code (join table; may not exist on older DBs)
        try:
            try:
                cur = con.execute("SELECT code,actress_id,role FROM work_actresses")
            except sqlite3.OperationalError:
                cur = con.execute("SELECT code,actress_id,'costar' AS role FROM work_actresses")
            for r in cur:
                wa.setdefault(r["code"], []).append((r["actress_id"], r["role"] or "costar"))
        except sqlite3.OperationalError:
            pass
        # works -> enrich
        # match by exact code, then by digits
        by_digits = {}
        for it in items:
            if it["digits"]:
                by_digits.setdefault(it["digits"], []).append(it)
        now_owned = []          # works whose file is on disk but the row still says missing
        try:
            _rows = con.execute(
                "SELECT code,actress_id,release_date,title,title_en,duration,"
                "producer,fav_count,availability,cover_url,censorship,fc2db_at,fc2dbnet_at "
                "FROM works")
        except sqlite3.OperationalError:            # pre-migration catalog
            _rows = con.execute(
                "SELECT code,actress_id,release_date,title,title_en,duration,"
                "producer,fav_count,availability,cover_url,censorship,NULL AS fc2db_at,"
                "NULL AS fc2dbnet_at FROM works")
        for w in _rows:
            targets = []
            if w["code"] in by_code:
                targets = by_all[w["code"]]     # every copy, not just the first one walked
            else:
                dg = norm_digits(w["code"])
                if dg and dg in by_digits:
                    targets = by_digits[dg]
            # A work with a file on disk is owned, whatever the row still claims. Rows go
            # stale because the importer only ever did INSERT OR IGNORE, which is a no-op
            # on an existing wishlist row.
            if targets and (w["availability"] or "") != "available":
                now_owned.append(w["code"])
            a = adict.get(w["actress_id"])
            for it in targets:
                it["db"] = True
                # has fc2ppv-db been asked about this movie? Without it, "no performer" and
                # "nobody has looked" are the same blank, and every unexplained blank reads
                # like the resolver is broken.
                it["fc2db_at"] = w["fc2db_at"] or ""
                it["fc2dbnet_at"] = w["fc2dbnet_at"] or ""
                it["release_date"] = w["release_date"] or ""
                it["title"] = w["title"] or ""
                it["title_en"] = w["title_en"] or ""
                it["cover_url"] = w["cover_url"] or ""
                it["censorship"] = w["censorship"] or "uncensored"
                it["fav_count"] = w["fav_count"]
                it["producer"] = w["producer"] or ""
                it["availability"] = "available"   # it is in `targets`, so it is on disk
                it["tags"] = tagmap.get(w["code"], [])
                if a:
                    it["actress_id"] = a["id"]
                    it["actress_en"] = a["name_en"]
                    it["actress_jp"] = a["name_jp"]
                    it["age"] = a["age"]
                    it["height"] = a["height"]
                    it["bust"] = a["bust"]
                    it["cup"] = a["cup"]
                    it["waist"] = a["waist"]
                    it["hip"] = a["hip"]
        # attach tags to ANY item by code (covers codes without a works row, e.g.
        # amateur movies the user tagged by hand in the editor)
        for it in items:
            if not it.get("tags"):
                tg = tagmap.get(it["code"])
                if tg:
                    it["tags"] = tg
        # build actress summary list (only those present in library get counts)
        for aid, r in adict.items():
            actresses[aid] = {
                "id": aid, "name_en": r["name_en"], "name_jp": r["name_jp"],
                "disp": disp_by_id.get(aid) or r["name_en"] or r["name_jp"],
                "fc2ppvdb_uuid": r["fc2ppvdb_uuid"],
                "age": r["age"], "height": r["height"], "bust": r["bust"],
                "cup": r["cup"], "waist": r["waist"], "hip": r["hip"],
                "birthdate": r["birthdate"], "description": r["description"] or "",
                "has_portrait": bool(r["has_portrait"]), "count": 0, "missing_count": 0,
            }
        # ---- reconcile availability with what is actually on disk ----
        # Done before the wishlist query below so a just-added movie stops producing a
        # ghost in this same pass. Deliberately one-way (missing -> available): the
        # reverse would mean a failed or partial scan — an unmounted SMB share, say —
        # silently flipping the whole library to "missing".
        if now_owned:
            try:
                con.executemany(
                    "UPDATE works SET availability='available', in_library=1 WHERE code=?",
                    [(c,) for c in now_owned])
                con.commit()
                sys.stderr.write(f"[db] {len(now_owned)} work(s) marked available "
                                 f"(found on disk)\n")
            except Exception as e:                       # read-only db, lock, etc.
                sys.stderr.write(f"[db] could not update availability: {e}\n")

        # ---- ghost items for MISSING works (wishlist) ----
        owned_aids = {it.get("actress_id") for it in items if it.get("actress_id")}
        for w in con.execute(
                "SELECT code,actress_id,release_date,title,producer,fav_count,cover_url,censorship "
                "FROM works WHERE availability='missing' AND actress_id IS NOT NULL"):
            if w["code"] in by_code or w["actress_id"] not in owned_aids:
                continue
            a = adict.get(w["actress_id"])
            # sort key: a wishlist item was never "added", so seed its recency from its
            # release date. Without this it defaults to 0 and every ghost sinks to the very
            # bottom of the "Newest added" sort, hiding them under long result lists.
            rel_epoch = _date_epoch(w["release_date"])
            ghost = {
                "code": w["code"], "digits": norm_digits(w["code"]),
                "actress_path": None, "primary": None, "ext": "", "size": 0,
                "nparts": 0, "mtime": rel_epoch, "added": rel_epoch,
                "rel": "", "missing": True, "db": True,
                "actress_id": w["actress_id"], "availability": "missing",
                "release_date": w["release_date"] or "", "title": w["title"] or "",
                "producer": w["producer"] or "", "fav_count": w["fav_count"],
                "cover_url": w["cover_url"] or "", "tags": tagmap.get(w["code"], []),
                "censorship": w["censorship"] or "uncensored",
            }
            if a:
                ghost.update({"actress_en": a["name_en"], "actress_jp": a["name_jp"],
                              "age": a["age"], "height": a["height"], "bust": a["bust"],
                              "cup": a["cup"], "waist": a["waist"], "hip": a["hip"]})
            items.append(ghost)
            by_code[w["code"]] = ghost
        con.close()

    # finalize display fields + count per actress
    fmt_counts, cup_counts, tag_counts, res_counts, studio_counts = {}, {}, {}, {}, {}
    for it in items:
        missing = bool(it.get("missing"))
        aid = it.get("actress_id")
        name = (disp_by_id.get(aid) or it.get("actress_en") or it.get("actress_jp")
                or it.get("actress_path") or "")
        it["actress"] = name
        it["identified"] = bool(name)
        it["display"] = it["code"]
        it["aka"] = " ".join(aka_map.get(aid, [])) if aid else ""
        # full cast (multi-actress) with role (main/costar), lead first
        cast = wa.get(it["code"])
        if cast:
            ordered = sorted(cast, key=lambda ar: (ar[1] != "main", ar[0] != aid))
            it["actresses"] = [{"id": x, "disp": disp_by_id.get(x) or "", "role": role}
                               for x, role in ordered]
        elif aid:
            it["actresses"] = [{"id": aid, "disp": name, "role": "main"}]
        else:
            it["actresses"] = []
        if missing:
            if aid in actresses:
                actresses[aid]["missing_count"] += 1
        else:
            for c in it["actresses"]:            # credit every actress in the cast
                if c["id"] in actresses:
                    actresses[c["id"]]["count"] += 1
        if missing:
            continue  # ghosts don't have a file / probe / library facets
        m = PROBE.get(it["code"]) or {}
        it["duration"] = m.get("duration")
        it["width"] = m.get("width")
        it["height_px"] = m.get("height")
        it["vcodec"] = m.get("vcodec")
        h = m.get("height") or 0
        if h:
            b = "4K" if h >= 1800 else "1080p" if h >= 1000 else "720p" if h >= 700 else "SD"
            res_counts[b] = res_counts.get(b, 0) + 1
        fmt = it["ext"].lstrip(".")
        fmt_counts[fmt] = fmt_counts.get(fmt, 0) + 1
        if it.get("cup"):
            cup_counts[it["cup"]] = cup_counts.get(it["cup"], 0) + 1
        for t in it.get("tags", []) or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
        pr = it.get("producer")
        if pr:
            studio_counts[pr] = studio_counts.get(pr, 0) + 1

    # actresses that appear via path only (no DB id) — synthesize entries
    path_only = {}
    for it in items:
        if not it.get("actress_id") and it.get("actress_path"):
            key = it["actress_path"]
            path_only.setdefault(key, 0)
            path_only[key] += 1
    alist = [a for a in actresses.values() if a["count"] > 0]
    for name, c in path_only.items():
        alist.append({"id": None, "name_en": name, "name_jp": None, "age": None,
                      "height": None, "bust": None, "cup": None, "waist": None,
                      "hip": None, "birthdate": None, "description": "",
                      "has_portrait": False, "count": c, "missing_count": 0})
    alist.sort(key=lambda a: (-(a["count"]), (a["name_en"] or a["name_jp"] or "").lower()))

    # flag duplicate codes (same code owned in >1 folder on disk)
    code_ct = {}
    for it in items:
        if not it.get("missing"):
            code_ct[it["code"]] = code_ct.get(it["code"], 0) + 1
    for it in items:
        if not it.get("missing") and code_ct.get(it["code"], 0) > 1:
            it["dup"] = True
    # Stable per-FOLDER id. One code is normally one folder, and then uid IS the code, so
    # /video/<uid>, /thumb/<uid>.jpg and the thumb cache are unchanged. Only when the same
    # code sits in two folders do the copies get distinct ids, so play, open, delete and
    # markers can address one folder instead of "whichever came first in the walk".
    for it in items:
        if it.get("dup"):
            h = hashlib.sha1((it.get("rel") or it.get("primary") or "").encode("utf-8")).hexdigest()[:8]
            it["uid"] = "%s~%s" % (it["code"], h)
        else:
            it["uid"] = it["code"]
    dup_codes = sum(1 for c, n in code_ct.items() if n > 1)

    _res_order = {"4K": 0, "1080p": 1, "720p": 2, "SD": 3}
    facets = {
        "formats": sorted(fmt_counts.items(), key=lambda kv: -kv[1]),
        "cups": sorted(cup_counts.items()),
        "res": sorted(res_counts.items(), key=lambda kv: _res_order.get(kv[0], 9)),
        "tags": sorted(tag_counts.items(), key=lambda kv: -kv[1]),
        "studios": sorted(studio_counts.items(), key=lambda kv: -kv[1])[:60],
        "total": sum(1 for it in items if not it.get("missing")),
        "missing": sum(1 for it in items if it.get("missing")),
        "identified": sum(1 for it in items if it["identified"] and not it.get("missing")),
        "total_bytes": sum(it["size"] for it in items),
        "with_db": sum(1 for it in items if it.get("db") and not it.get("missing")),
        "dup_codes": dup_codes,
    }
    by_uid = {it["uid"]: it for it in items if it.get("uid")}
    return by_code, by_uid, alist, facets


def _rebuild_tag_facet():
    """Recompute STATE['facets']['tags'] from current in-memory item tags, so a tag added
    via the movie editor or the organize modal shows up in the sidebar/filter immediately
    (facets are otherwise built once at boot)."""
    if not STATE.get("facets"):
        return
    tc = {}
    for it in STATE["items"]:
        if it.get("missing"):
            continue
        for t in it.get("tags") or []:
            tc[t] = tc.get(t, 0) + 1
    STATE["facets"]["tags"] = sorted(tc.items(), key=lambda kv: -kv[1])


def _save_fs_index(lib, items):
    """Persist the raw filesystem scan (pre-enrich) so boot can skip the slow walk."""
    try:
        tmp = INDEX_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"library": lib, "at": int(time.time()), "items": items}, f)
        os.replace(tmp, INDEX_CACHE)
    except Exception:
        pass


def _load_fs_index(lib):
    try:
        with open(INDEX_CACHE) as f:
            d = json.load(f)
        if d.get("library") == lib and isinstance(d.get("items"), list) and d["items"]:
            return d["items"]
    except Exception:
        pass
    return None


def rebuild_index(fs_items=None):
    """Rebuild STATE. fs_items!=None reuses a cached filesystem scan (instant);
    otherwise it re-walks the library (slow over SMB) and re-caches the result."""
    lib = STATE["library"]
    t0 = time.time()
    with _INDEX_LOCK:
        global _INDEX_SEQ
        _INDEX_SEQ += 1
        my_seq = _INDEX_SEQ
    if fs_items is None:
        items = scan_library(lib)          # the slow, SMB-I/O-bound part
        # A walk is only allowed to replace what we have if we believe it. An unmounted share,
        # a permission failure or a partial walk returns a SHORT list, and writing that to the
        # cache made movies disappear until a later full walk happened to succeed.
        known = len(STATE["items"]) or len(_load_fs_index(lib) or [])
        why = ""
        if LAST_WALK["missing_root"]:
            why = "library folder is not there (share unmounted?)"
        elif LAST_WALK["errors"]:
            why = "%d unreadable folder(s): %s" % (LAST_WALK["errors"], LAST_WALK["first_error"])
        elif known >= 20 and len(items) < known * 0.5:
            why = "walk found %d of about %d items" % (len(items), known)
        if why:
            STATE["scan_warning"] = why
            sys.stderr.write("[scan] REFUSED to replace the index - %s\n" % why)
            sys.stderr.flush()
            if STATE["items"]:
                return                      # keep the good state we already serve
            items = _load_fs_index(lib) or items   # cold start: fall back to the cache
            src = "cache"
        else:
            STATE["scan_warning"] = ""
            _save_fs_index(lib, items)     # cache raw scan BEFORE enrich mutates it
            src = "scan"
    else:
        items = fs_items
        src = "cache"
    _stamp_added(items)                      # idempotent 'date added' (also upgrades old caches)
    by_code, by_uid, alist, facets = enrich(items)  # fast: DB overlay + ghosts (mutates items)
    # Publish in ONE step. These five used to be assigned separately, so a request arriving
    # mid-swap could see the new grid with the previous path map. Several rebuilds can also
    # be in flight at once (boot, /api/rescan, organize, import, delete), and the slowest one
    # finishing last would win; a result older than what is already published is dropped.
    with _INDEX_LOCK:
        if my_seq < STATE.get("index_seq", 0):
            sys.stderr.write(f"[{src}] discarded: a newer walk already published\n")
            sys.stderr.flush()
            return
        STATE.update(items=items, by_code=by_code, by_uid=by_uid, actresses=alist,
                     facets=facets, scanned_at=int(time.time()), index_seq=my_seq,
                     data_json=None, data_gzip=None, data_gzip_of=None)
    sys.stderr.write(f"[{src}] {facets['total']} items in {time.time()-t0:.1f}s "
                     f"({facets['identified']} identified, "
                     f"{facets['total_bytes']/1e12:.2f} TB)\n")


# ----------------------------------------------- deletion (movies / actresses)
def _mount_point(path):
    """The volume mount point containing path, so a trash move stays on one volume
    (an instant rename, never a multi-GB cross-volume copy)."""
    p = os.path.abspath(path)
    while p and p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    return p or "/"


def _count_files(path):
    """Files under path (1 for a plain file), for verifying a trash move kept everything."""
    if os.path.isfile(path):
        return 1
    n = 0
    for _dp, _dn, fns in os.walk(path):
        n += len(fns)
    return n


def _trash_log(line):
    """Append one line to cache/trash.log — deletions are the one thing worth an audit trail."""
    try:
        with open(os.path.join(CACHE, "trash.log"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


# One delete at a time. A trash move that falls back to a copy takes minutes on SMB, and a
# second click used to start a SECOND move of the same folder: the first had created the
# destination but not yet removed the source, so the retry wrote a timestamped duplicate and
# the library ended up with two copies of the same movie in the trash.
_TRASH_LOCK = threading.Lock()


def _rename_or_explain(src, dst):
    """Move src to dst on the same volume. NEVER falls back to copying.

    shutil.move() swallows a failed os.rename() and copies instead. On the SMB library that is
    the difference between 0.4 s and several minutes per movie, and it is reached for a
    mundane reason: this share refuses to rename a folder while any file inside it is open
    (EACCES), which is exactly the case when you delete the movie you are watching."""
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno == errno.EXDEV:          # genuinely another volume: copying is the only way
            shutil.move(src, dst)
            return
        if e.errno in (errno.EACCES, errno.EPERM, errno.EBUSY, errno.ETXTBSY):
            raise OSError("%s is still open — close the player (or whatever is reading it) "
                          "and try again." % os.path.basename(src.rstrip("/")))
        raise


def _to_trash(paths):
    """Move paths to a RECOVERABLE trash by a same-volume rename (never a hard unlink, never
    a cross-volume copy). Files on the system volume go to ~/.Trash (the real Trash); files
    on another volume — e.g. the SMB library, where macOS can't use the real Trash — go to a
    '_vault_trash/' folder at the library root (same share, instant, and already reserved by
    the organize pass so it is never re-scanned). Returns (moved_paths, error_or_None)."""
    real = [p for p in dict.fromkeys(paths) if p and os.path.exists(p)]
    if not real:
        return [], None
    home_mp = _mount_point(os.path.expanduser("~"))
    lib = STATE.get("library") or ""
    moved = []
    try:
        for p in real:
            if _mount_point(p) == home_mp:
                dest = os.path.expanduser("~/.Trash")
            else:
                dest = os.path.join(lib or _mount_point(p), "_vault_trash")
            os.makedirs(dest, exist_ok=True)
            base = os.path.basename(p.rstrip("/")) or "item"
            dst = os.path.join(dest, base)
            if os.path.exists(dst):                       # collision -> timestamp it
                dst = os.path.join(dest, time.strftime("%Y%m%d-%H%M%S-") + base)
            before = _count_files(p)
            _rename_or_explain(p, dst)                    # same volume => rename, instant
            after = _count_files(dst)
            _trash_log("moved %s -> %s (%d file(s) before, %d after)" % (p, dst, before, after))
            if after < before:                            # never silently lose content
                raise OSError("%s arrived in the trash with %d of %d files"
                              % (dst, after, before))
            moved.append(dst)
    except Exception as e:
        return moved, str(e)
    return moved, None


def _delete_targets(it, library):
    """Paths to trash for one movie: the whole movie folder when it is a dedicated leaf
    (so poster/subs/nfo go with it), else just the video files — NEVER the library root
    and never a folder that also holds other movies."""
    primary = it.get("primary")
    if not primary:
        return []
    mdir = os.path.dirname(primary)
    parts = [p["path"] for p in (it.get("parts") or []) if p.get("path")] or [primary]
    try:
        libreal = os.path.realpath(library or "")
        if os.path.realpath(mdir) != libreal and not any(
                os.path.isdir(os.path.join(mdir, e)) for e in os.listdir(mdir)):
            return [mdir]
    except OSError:
        pass
    return parts


def _purge_work_rows(con, code):
    con.execute("DELETE FROM tags WHERE code=?", (code,))
    con.execute("DELETE FROM work_actresses WHERE code=?", (code,))
    con.execute("DELETE FROM works WHERE code=?", (code,))


def _purge_userdata(codes=(), actress_ids=()):
    """Drop deleted movies/actresses from the personal userdata sidecar so counts stay honest."""
    ud = _load_userdata()
    changed = False
    cs = set(map(str, codes))
    if cs:
        for k in ("fav", "seen"):
            new = [c for c in ud[k] if c not in cs]
            if len(new) != len(ud[k]):
                ud[k] = new
                changed = True
        for c in [c for c in ud["rating"] if c in cs]:
            ud["rating"].pop(c, None)
            changed = True
    ais = set(map(str, actress_ids))
    if ais:
        new = [a for a in ud["afav"] if a not in ais]
        if len(new) != len(ud["afav"]):
            ud["afav"] = new
            changed = True
    if changed:
        _save_userdata()


def _resync_after_delete(deleted_codes):
    """Prune trashed codes from the cached filesystem scan and re-enrich — no SMB re-walk."""
    lib = STATE["library"]
    raw = _load_fs_index(lib)
    if raw is not None:
        raw = [it for it in raw if it.get("code") not in deleted_codes]
        _save_fs_index(lib, raw)
        rebuild_index(fs_items=raw)
    else:
        rebuild_index()


# ----------------------------------------------- scene markers (sidecar *.scenes.json)
# An external analyzer (separate repo, "vault-scene-tagger") writes one <video>.scenes.json
# next to each video file: {schema:"vault-scenes/1", video, duration, markers:[{t,end?,label,
# source,confidence?}]}. The sidecar is the source of truth; the `markers` table is just a
# queryable cache. We also WRITE the sidecar for manual markers (source:"manual") — the merge
# rule is the analyzer owns cut/heuristic/ml rows and never touches manual ones.
SCENES_SCHEMA = "vault-scenes/1"
# Vault's marker vocabulary: the act + position labels of SCENES_SPEC §4 (the state and structure
# labels were dropped on 2026-09-15) — the only built-in labels the player's marker editor may set.
SCENE_LABELS = frozenset((
    "fingering", "blowjob", "titjob", "toy", "creampie",
    "missionary", "doggy", "cowgirl", "standing", "scoop-up",
))
# User-defined extra labels (Settings -> training mode) must look like the built-in ones.
_SCENE_LABEL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _normalize_scene_label(value):
    """"Foot Job" -> "foot-job"; returns "" if nothing usable is left."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _ensure_works_schema(con):
    """Provenance for censorship, so a rescan cannot silently flip what you set by hand."""
    try:
        con.execute("ALTER TABLE works ADD COLUMN censorship_src TEXT")
    except sqlite3.OperationalError:
        pass            # already there
    # fc2ppv-db is the primary source for the cast. Two columns make that true in practice:
    #   fc2db_at    when the page was last read, so "still to look up" stops meaning "has no
    #               actress yet" -- a movie whose performer came from a folder name was never
    #               offered to fc2ppv-db at all, and so never got its co-stars or tags either.
    #   actress_src who set works.actress_id, so fc2ppv-db may correct its own and the folder
    #               pass's guesses while never touching one you set by hand.
    fresh = False
    try:
        con.execute("ALTER TABLE works ADD COLUMN fc2db_at TEXT")
        fresh = True
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE works ADD COLUMN actress_src TEXT")
    except sqlite3.OperationalError:
        pass
    # fc2db.net's own "page has been read" — separate from fc2db_at on purpose
    try:
        con.execute("ALTER TABLE works ADD COLUMN fc2dbnet_at TEXT")
    except sqlite3.OperationalError:
        pass
    if fresh:
        # Backfill, or the first scan after this upgrade would re-read all ~3,500 pages.
        # A cast row (or a row fc2ppv-db itself created) means the page was already read.
        n = con.execute(
            "UPDATE works SET fc2db_at='backfilled' WHERE fc2db_at IS NULL AND ("
            " source='fc2ppv-db' OR EXISTS(SELECT 1 FROM work_actresses wa"
            " WHERE wa.code=works.code))").rowcount
        # Anything the user typed is protected from here on.
        m = con.execute(
            "UPDATE works SET actress_src='manual' WHERE actress_id IS NOT NULL AND ("
            " source='manual' OR actress_id IN (SELECT id FROM actresses"
            " WHERE source='manual'))").rowcount
        sys.stderr.write("[db] fc2ppv-db provenance: %d row(s) marked already read, "
                         "%d actress link(s) marked manual\n" % (n, m))
        con.commit()
    # 'owned' was a third availability value two inserts could write; nothing else knew it.
    try:
        n = con.execute("UPDATE works SET availability=CASE WHEN in_library=1 "
                        "THEN 'available' ELSE 'missing' END "
                        "WHERE availability='owned'").rowcount
        if n:
            sys.stderr.write("[db] normalized %d row(s) stuck at availability='owned'\n" % n)
    except sqlite3.OperationalError:
        pass


def _ensure_markers_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS markers("
                "code TEXT NOT NULL, part INTEGER NOT NULL DEFAULT 0, t REAL NOT NULL, "
                "tend REAL, label TEXT NOT NULL, source TEXT DEFAULT 'manual', confidence REAL)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_markers_code ON markers(code)")
    con.execute("CREATE TABLE IF NOT EXISTS marker_src("
                "code TEXT NOT NULL, part INTEGER NOT NULL, path TEXT, mtime INTEGER, "
                "PRIMARY KEY(code, part))")


def _scenes_path(video_path):
    return os.path.splitext(video_path)[0] + ".scenes.json"


def _read_scenes(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_scenes(path, video, duration, markers, existing=None):
    # Keep the analyzer's extra top-level fields (e.g. its "tagger" phase record) and its
    # generator: dropping them makes vault-scene-tagger think it never ran and re-decode the movie.
    data = {k: v for k, v in (existing or {}).items() if k != "markers"}
    data.update({"schema": SCENES_SCHEMA, "video": video, "duration": duration,
                 "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    if not isinstance(data.get("generator"), str) or not data["generator"]:
        data["generator"] = "vault"
    data["markers"] = sorted(markers, key=lambda m: float(m.get("t", 0) or 0))
    d, base = os.path.split(path)
    tmp = os.path.join(d, "." + base + ".tmp")   # hidden, same dir -> atomic rename
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _item_parts(it):
    return it.get("parts") or ([{"path": it.get("primary"),
                                 "name": os.path.basename(it.get("primary") or "")}]
                               if it.get("primary") else [])


def _import_scenes_for_item(con, it):
    """Sync each part's sidecar into the markers cache; skip parts whose sidecar is unchanged."""
    code = it.get("code")
    if not code or it.get("missing"):
        return
    for idx, pp in enumerate(_item_parts(it)):
        vp = pp.get("path")
        if not vp:
            continue
        sp = _scenes_path(vp)
        try:
            mt = int(os.path.getmtime(sp))
        except OSError:
            # sidecar gone -> drop any stale cached rows for this part
            if con.execute("SELECT 1 FROM marker_src WHERE code=? AND part=?",
                           (code, idx)).fetchone():
                con.execute("DELETE FROM markers WHERE code=? AND part=?", (code, idx))
                con.execute("DELETE FROM marker_src WHERE code=? AND part=?", (code, idx))
            continue
        row = con.execute("SELECT mtime FROM marker_src WHERE code=? AND part=?",
                          (code, idx)).fetchone()
        if row and row[0] == mt:
            continue                      # cache already current
        data = _read_scenes(sp)
        if data is None:
            continue
        con.execute("DELETE FROM markers WHERE code=? AND part=?", (code, idx))
        for m in (data.get("markers") or []):
            try:
                t = float(m["t"])
            except (KeyError, TypeError, ValueError):
                continue
            end = m.get("end")
            try:
                end = float(end) if end is not None else None
            except (TypeError, ValueError):
                end = None
            conf = m.get("confidence")
            try:
                conf = float(conf) if conf is not None else None
            except (TypeError, ValueError):
                conf = None
            con.execute("INSERT INTO markers(code,part,t,tend,label,source,confidence) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (code, idx, t, end, str(m.get("label") or "scene"),
                         str(m.get("source") or "ml"), conf))
        con.execute("INSERT INTO marker_src(code,part,path,mtime) VALUES(?,?,?,?) "
                    "ON CONFLICT(code,part) DO UPDATE SET path=excluded.path, mtime=excluded.mtime",
                    (code, idx, sp, mt))
    con.commit()


def _markers_for_code(con, code):
    return [{"part": r[0], "t": r[1], "end": r[2], "label": r[3],
             "source": r[4], "confidence": r[5]}
            for r in con.execute(
                "SELECT part,t,tend,label,source,confidence FROM markers "
                "WHERE code=? ORDER BY part,t", (code,))]


_FLAG_CACHE = {}


def _sidecar_signature():
    """Fingerprint of every part's sidecar (mtime, size, or absent). A stat per video: ~20 ms
    for the whole library once the NAS has the directories cached."""
    h = 0
    for it in STATE.get("items") or ():
        for pp in _item_parts(it):
            vp = pp.get("path")
            if not vp:
                continue
            try:
                st = os.stat(_scenes_path(vp))
                h = hash((h, vp, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return h


def _marker_flags(it):
    """(has_ai, has_manual, has_untouched_part) for a movie, read from its sidecars.

    "AI pre-tagged" = at least one marker still has an analyzer source (ml/heuristic/cut);
    "manually corrected" = at least one marker the user made (source manual).
    The third flag is PER PART: a release whose part 1 is reviewed but whose part 2 was never
    opened still has work left, and release-level flags hide that (a 6-parter with 4 untouched
    parts looked "corrected"). "To review" uses this one.
    """
    ai = manual = todo = False
    for pp in _item_parts(it):
        vp = pp.get("path")
        if not vp:
            continue
        sp = _scenes_path(vp)
        try:
            st = os.stat(sp)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            _FLAG_CACHE.pop(sp, None)
            continue
        hit = _FLAG_CACHE.get(sp)
        if not hit or hit[0] != key:
            data = _read_scenes(sp) or {}
            markers = [m for m in (data.get("markers") or []) if isinstance(m, dict)]
            hit = (key, any(m.get("source") == "manual" for m in markers),
                   any(m.get("source") not in (None, "manual") for m in markers))
            _FLAG_CACHE[sp] = hit
        manual = manual or hit[1]
        ai = ai or hit[2]
        todo = todo or (hit[2] and not hit[1])      # this part has AI markers and none of yours
    return ai, manual, todo


# ----------------------------------------------- actress names carried in folders
# Folders like "FC2-PPV-1668475 - sora mikuno" hold the performer's name only in the
# directory name. Before organize strips them back to a bare "<CODE>", we harvest that
# name INTO the catalog DB so nothing is lost — the folder becomes disposable, the DB
# becomes the record. (See _organize.)
_NAME_JUNK = ("1080p", "720p", "2160p", "1440p", "480p", "4k", "8k", "fhd", "uhd",
              "uncensored", "uncen", "censored", "leak", "leaked", "complete", "full",
              "pack", "part", "vol", "hd", "ppv", "reduced", "mosaic", "nomosaic")


def _name_from_folder(base):
    """Extract a plausible performer name from a '<CODE> - Name' style folder, or None.

    Precision over recall: a Latin-script suffix is only taken when a dash separates it
    from the code ('<CODE> - Aoi Rena'), so bracketed *titles* ('[<CODE>] some title')
    are not mistaken for names. CJK suffixes are accepted with or without a dash."""
    if not base:
        return None
    m = re.match(r"^\[?\s*FC2[-_\s]*PPV[-_\s]*\d{5,10}\s*\]?(.*)$", base, re.I)
    if not m:
        return None
    tail = m.group(1)
    had_dash = bool(re.match(r"^\s*[-–—]\s*", tail))
    rest = tail.strip(" -–—_[]()　\t").strip()
    if len(rest) < 2 or len(rest) > 40:
        return None
    low = rest.lower()
    if any(j in low for j in _NAME_JUNK):
        return None                       # a quality/format tag, not a name
    if re.fullmatch(r"[\d\W_]+", rest):
        return None                       # only digits / punctuation
    is_cjk = bool(re.search(r"[぀-ヿ一-鿿가-힣]", rest))
    if not is_cjk and not had_dash:
        return None                       # bare Latin suffix with no dash -> likely a title
    if not is_cjk and len(rest.split()) > 4:
        return None                       # too many words to be a name
    return rest


def folder_actress_pairs(library):
    """[(code, raw_name), …] for every movie folder that carries a name suffix."""
    out = []
    if not os.path.isdir(library):
        return out
    for dirpath, dirs, files in os.walk(library):
        dirs.sort()
        base = os.path.basename(dirpath.rstrip("/"))
        if base.lower() in ("_vault_trash", "_duplicates"):
            dirs[:] = []
            continue
        if not _largest_video(files, dirpath):
            continue
        name = _name_from_folder(base)
        if name:
            out.append((norm_code(base), name))
    return out


def _token_key(s):
    """word-order-insensitive key: sorted, punctuation-stripped, lowercased tokens.
    'Sora Mikumo' and 'Mikumo Sora' collapse to the same key."""
    toks = re.sub(r"[^\w\s]", " ", (s or "").lower()).split()
    return " ".join(sorted(toks))


def _find_actress(con, name):
    """Resolve a typed name to an existing actress id — matching exact name (en/jp/kana),
    an alias, OR the same name in a different WORD ORDER (the split that created duplicate
    records before). Returns None only when the person is genuinely new."""
    if not name or not name.strip():
        return None
    name = name.strip()
    r = con.execute("SELECT id FROM actresses WHERE name_en=? COLLATE NOCASE "
                    "OR name_jp=? OR name_kana=?", (name, name, name)).fetchone()
    if r:
        return r[0]
    r = con.execute("SELECT actress_id FROM aliases WHERE alias=? COLLATE NOCASE",
                    (name,)).fetchone()
    if r:
        return r[0]
    # word-order-insensitive fallback (multi-token romaji only, to avoid collapsing all
    # the different single-name amateurs like "Yui" onto each other)
    tk = _token_key(name)
    if tk and len(tk.split()) >= 2:
        for aid, nm in con.execute("SELECT id,name_en FROM actresses "
                                   "WHERE name_en IS NOT NULL"):
            if _token_key(nm) == tk:
                return aid
        for aid, al in con.execute("SELECT actress_id,alias FROM aliases"):
            if _token_key(al) == tk:
                return aid
    return None


def capture_folder_actresses(pairs, apply=False):
    """Link a work to an actress ONLY when the folder name already matches a known actress
    (exact match on name_en/jp/kana or an existing alias). Never creates an actress from a
    folder name — those spellings are too unreliable; unmatched names are simply dropped
    when the folder is normalized, and the actress is found online instead. Fill-if-empty:
    an existing link is never overwritten. Returns a per-item report."""
    if not os.path.exists(DB_PATH):
        return []
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA busy_timeout=4000")
    report = []
    try:
        for code, name in pairs:
            aid = _find_actress(con, name)          # existing actress only; None if unknown
            if aid is not None and apply:
                con.execute(
                    # 'available' -- not 'owned'. Disk reconcile and the wishlist query only
                    # know available/missing, so a row left at 'owned' was neither a library
                    # item nor a wishlist ghost until some later enrich happened to repair it.
                    "INSERT INTO works(code,actress_id,source,in_library,availability) "
                    "VALUES(?,?, 'folder', 1, 'available') "
                    "ON CONFLICT(code) DO UPDATE SET "
                    "actress_id=COALESCE(works.actress_id, excluded.actress_id)",
                    (code, aid))
            report.append({"code": code, "name": name,
                           "actress_id": aid, "matched": aid is not None})
        if apply:
            con.commit()
    finally:
        con.close()
    return report


# ------------------------------------------------------------------ ffprobe
def _probe_sig(path):
    """Identity of the file we probed: a replaced encode must not keep the old duration."""
    try:
        st = os.stat(path)
        return [int(st.st_size), int(st.st_mtime)]
    except OSError:
        return None


def probe_code(code):
    it = STATE["by_uid"].get(code) or STATE["by_code"].get(code)
    if not it:
        return None
    path = it["primary"]
    sig = _probe_sig(path)
    with PROBE_LOCK:
        hit = PROBE.get(code)
        # A cached entry is only good while the file is the same one we measured. Entries
        # written before signatures existed have none; keep honouring those.
        if hit is not None and (hit.get("sig") is None or sig is None or hit["sig"] == sig):
            return hit
    meta = {"duration": None, "width": None, "height": None, "vcodec": None}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name",
             "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
        if data.get("streams"):
            s = data["streams"][0]
            meta["width"] = s.get("width")
            meta["height"] = s.get("height")
            meta["vcodec"] = s.get("codec_name")
        dur = (data.get("format") or {}).get("duration")
        if dur:
            meta["duration"] = int(float(dur))
    except Exception:
        pass
    if meta["duration"] is None and meta["width"] is None:
        # a timeout, a missing ffprobe or an unreadable file -- caching that all-None dict
        # meant the movie never showed a duration again until the cache was cleared by hand
        return meta
    meta["sig"] = sig
    with PROBE_LOCK:
        PROBE[code] = meta
        _probe_save_counter[0] += 1
        due = _probe_save_counter[0] % 25 == 0
    if due:
        save_probe()
    return meta


_probe_save_counter = [0]


def _probe_duration(path):
    """Duration in seconds of one file (a part that is not the movie's primary), or None."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
        return round(float(out.stdout.strip()), 3)
    except Exception:
        return None


def probe_and_persist(code):
    return probe_code(code)


# ------------------------------------------------------------------- thumbs
def thumb_path(code):
    return os.path.join(THUMB_DIR, safe_key(code) + ".jpg")


def generate_thumb(code):
    it = STATE["by_uid"].get(code) or STATE["by_code"].get(code)
    if not it:
        return None
    out = thumb_path(code)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    path = it["primary"]
    meta = probe_code(it["code"])
    dur = (meta or {}).get("duration") or 0
    seek = max(4, int(dur * 0.20)) if dur else 90
    for ss in (seek, 20, 3):
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ss), "-i", path,
                 "-frames:v", "1", "-vf", "scale=480:-2",
                 "-q:v", "4", out],
                capture_output=True, timeout=90)
            if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                return out
        except Exception:
            continue
    return None


PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270">'
    b'<rect width="100%" height="100%" fill="#1a1a22"/>'
    b'<text x="50%" y="50%" fill="#555" font-family="sans-serif" '
    b'font-size="20" text-anchor="middle" dy=".3em">no preview</text></svg>')


# -------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        enc = None
        # gzip text-ish payloads when the client accepts it (JSON/HTML/JS/CSS/SVG)
        if (len(body) > 1024
                and "gzip" in self.headers.get("Accept-Encoding", "")
                and any(t in ctype for t in ("json", "javascript", "text/", "svg"))):
            body = gzip.compress(body, 5)
            enc = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        h = {"Cache-Control": "no-store"}
        if extra:
            h.update(extra)
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj).encode("utf-8"), h)

    def _userdata_post(self, body):
        ud = _load_userdata()
        # full replace/merge — used once to migrate any pre-existing per-browser data
        if isinstance(body.get("replace"), dict):
            r = body["replace"]
            for k in ("fav", "seen", "afav"):
                s = set(ud[k])
                s.update(str(x) for x in (r.get(k) or []))
                ud[k] = list(s)
            for code, n in (r.get("rating") or {}).items():
                try:
                    n = int(n)
                except Exception:
                    continue
                if n:
                    ud["rating"][str(code)] = n
            _save_userdata()
            return self._json({"ok": True})
        op = body.get("op")
        if op in ("fav", "seen"):
            code = str(body.get("code") or "")
            s = set(ud[op])
            s.add(code) if body.get("on") else s.discard(code)
            ud[op] = list(s)
        elif op == "afav":
            aid = str(body.get("id") or "")
            s = set(ud["afav"])
            s.add(aid) if body.get("on") else s.discard(aid)
            ud["afav"] = list(s)
        elif op == "rating":
            code = str(body.get("code") or "")
            try:
                n = int(body.get("n") or 0)
            except Exception:
                n = 0
            if n:
                ud["rating"][code] = n
            else:
                ud["rating"].pop(code, None)
        else:
            return self._json({"error": "bad op"}, 400)
        _save_userdata()
        return self._json({"ok": True})

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._serve_file(os.path.join(HERE, "index.html"),
                                        "text/html; charset=utf-8")
            if p.startswith("/icons/"):
                # marker pictograms; basename only, so no path can escape icons/
                name = os.path.basename(p[len("/icons/"):])
                if not re.match(r"^[a-z0-9-]+\.png$", name):
                    return self._send(404, "text/plain", "no icon")
                return self._serve_file(os.path.join(HERE, "icons", name), "image/png")
            if p == "/api/data":
                return self._api_data()
            if p == "/api/meta":
                return self._api_meta(q)
            if p == "/api/rescan":
                rebuild_index()
                return self._json({"ok": True, "count": len(STATE["items"])})
            if p == "/api/settings":
                return self._api_settings()
            if p == "/api/userdata":
                return self._json(_load_userdata())
            if p == "/api/markers":
                return self._api_markers(q)
            if p == "/api/scanprogress":
                pr = dict(scanmod.PROGRESS) if scanmod else {"running": False}
                return self._json(pr)
            if p == "/api/importprogress":
                return self._json(dict(IMPORT_PROGRESS))
            if p == "/api/model":
                return self._json(_model_info(fetch_latest=True))
            if p == "/api/modelprogress":
                return self._json(_model_info())
            if p.startswith("/thumb/"):
                return self._thumb(p[len("/thumb/"):])
            if p.startswith("/portrait/"):
                return self._portrait(p[len("/portrait/"):])
            if p.startswith("/video/"):
                return self._video(p[len("/video/"):], q.get("part", [None])[0])
            self._send(404, "text/plain", "not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        # do_GET wraps its dispatch; this one did not, so a locked database or a bug dropped
        # the socket instead of answering. Same contract for both verbs.
        try:
            origin = self.headers.get("Origin")
            if origin and urlparse(self.path).path != "/api/stash":
                # Browsers send Origin on same-origin POSTs too, so this compares cleanly.
                # Without it, a page you merely visit could drive this server.
                want = (self.headers.get("Host") or "").strip()
                if urlparse(origin).netloc != want:
                    return self._json({"error": "cross-origin request refused"}, 403)
            return self._post_routes()
        except BrokenPipeError:
            return
        except Exception as e:
            try:
                return self._json({"error": str(e)}, 500)
            except Exception:
                return

    def _post_routes(self):
        u = urlparse(self.path)
        p = u.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if p == "/api/userdata":    # per-user prefs only; never touches the catalog cache
            return self._userdata_post(body)
        # Rebuilding the projection means a stat per sidecar across the whole share, so only
        # invalidate when the POST can actually change what /api/data says. Opening a file,
        # testing a connection or reading progress cannot.
        if p not in _POST_KEEPS_DATA:
            STATE["data_json"] = None
            STATE["data_gzip"] = STATE["data_gzip_of"] = None
        if p == "/api/open":
            return self._open_external(body.get("code"), reveal=False)
        if p == "/api/reveal":
            return self._open_external(body.get("code"), reveal=True)
        if p == "/api/settags":
            return self._set_tags(body.get("code"), body.get("tags") or [])
        if p == "/api/setactress":
            return self._set_actress(body.get("code"), body.get("name_en"), body.get("name_jp"))
        if p == "/api/setmeta":
            return self._set_meta(body)
        if p == "/api/bulkactress":
            return self._bulk_actress(body.get("codes") or [], body.get("name_en"),
                                      body.get("name_jp"))
        if p == "/api/setcensor":
            return self._set_censor(body.get("code"), bool(body.get("uncensored")))
        if p == "/api/deletemovie":
            return self._delete_movies(body.get("codes") or [body.get("code")])
        if p == "/api/marker":
            return self._marker_edit(body)
        if p == "/api/importmarkers":
            return self._import_markers_all()
        if p == "/api/deleteactress":
            return self._delete_actress(body.get("id"))
        if p == "/api/scan":
            return self._scan_one(body.get("code"), bool(body.get("force")))
        if p == "/api/scanlib":
            return self._scan_library(body)
        if p == "/api/wishlistcovers":
            return self._wishlist_covers()
        if p == "/api/foldactress":
            return self._fold_actress(bool(body.get("apply")), body.get("skip"),
                                      body.get("allow"))
        if p == "/api/organize":
            return self._organize(bool(body.get("apply")), body.get("skip"),
                                  body.get("enrich"), body.get("allow"),
                                  body.get("plan_id"))
        if p == "/api/enrichactress":
            return self._enrich_actress(body.get("actress_id"), body.get("code"),
                                        bool(body.get("force")), bool(body.get("dry")))
        if p == "/api/tagedit":
            return self._tag_edit(body.get("key"), body.get("en"), body.get("jp"))
        if p == "/api/tagdelete":
            return self._tag_delete(body.get("tag"))
        if p == "/api/actressedit":
            return self._actress_edit(body)
        if p == "/api/actressrename":
            return self._actress_rename(body.get("id"), body.get("name_en"),
                                        body.get("name_jp"))
        if p == "/api/actressmerge":
            return self._actress_merge(body.get("target"), body.get("sources") or [])
        if p == "/api/dedupe":
            return self._dedupe(bool(body.get("apply")), body.get("groups"))
        if p == "/api/setlibrary":
            return self._set_library(body.get("path"))
        if p == "/api/setdownload":
            return self._set_download(body.get("path"))
        if p == "/api/settraining":
            return self._set_training(body.get("on"))
        if p == "/api/model":
            return self._download_model()
        if p == "/api/scenelabel":
            return self._scene_label(body)
        if p == "/api/import":
            if body.get("preview"):
                return self._import_preview()
            return self._import_run(bool(body.get("confirm_lib_deletes")))
        if p == "/api/clearcache":
            return self._clear_cache(body.get("targets") or [])
        if p == "/api/sourcesave":
            return self._sources_save(body.get("sources"))
        if p == "/api/fc2db/save":
            return self._fc2db_save(body.get("cookie"), body.get("ua"),
                                     body.get("mode"), body.get("solver"))
        if p == "/api/solver/check":
            return self._solver_check()
        if p == "/api/fc2dbnet/test":
            return self._fc2dbnet_test(body.get("code"))
        if p == "/api/fc2db/test":
            return self._fc2db_test(body.get("code"))
        if p in ("/api/scanall", "/api/fc2db/scan", "/api/fc2dbnet/scan"):
            # one pipeline: the old single-source routes now check every source too
            return self._scan_everything(body.get("scope") or "missing")
        if p == "/api/stash":
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", (self.headers.get("X-Stash") or "stash")) + ".json"
            with open(os.path.join(CACHE, name), "wb") as f:
                f.write(raw)
            return self._json({"ok": True, "bytes": len(raw), "file": name},
                              extra={"Access-Control-Allow-Origin": "*"})
        self._json({"error": "unknown"}, 404)

    def do_OPTIONS(self):
        # Only /api/stash is meant to be called by another origin (a local Stash plugin).
        # Answering every path with Allow-Origin:* let any site the browser visits preflight
        # and then call delete, organize, import or setlibrary on this no-auth server.
        if urlparse(self.path).path != "/api/stash":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Stash")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- endpoints ---------------------------------------------------------
    def _serve_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, "text/plain", "missing")
        self._send(200, ctype, body, {"Cache-Control": "no-cache"})

    def _api_data(self):
        # serve a cached serialization; it only changes when the index/state does
        # (do_POST and rebuild_index clear STATE["data_json"]) or when a sidecar changes on
        # disk: vault-scene-tagger writes those behind our back, and the marker flags
        # ("To review", "AI pre-tagged") are baked into the cached payload.
        body = STATE.get("data_json")
        if body is not None and STATE.get("data_sig") == _sidecar_signature():
            return self._send_data_json(body)
        with _DATA_BUILD_LOCK:
            # another thread may have built it while we waited on the lock
            body = STATE.get("data_json")
            if body is not None and STATE.get("data_sig") == _sidecar_signature():
                return self._send_data_json(body)
            return self._build_data_json()

    def _send_data_json(self, body):
        """Send the cached projection, reusing the cached gzip of it.

        The payload is byte-identical between requests, so re-compressing 6 MB on every
        reload was pure waste (~70 ms of CPU each time, for the same 1 MB result)."""
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            gz = STATE.get("data_gzip")
            if gz is None or STATE.get("data_gzip_of") is not body:
                gz = gzip.compress(body, 5)
                STATE["data_gzip"], STATE["data_gzip_of"] = gz, body
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(gz)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(gz)
            return
        return self._send(200, "application/json; charset=utf-8", body,
                          {"Cache-Control": "no-store"})

    def _build_data_json(self):
        # ship a compact projection (omit absolute paths)
        sig = _sidecar_signature()      # taken first: a sidecar written mid-build rebuilds next time
        out_items = []
        for it in STATE["items"]:
            ai_tags, manual_tags, to_review = _marker_flags(it)
            out_items.append({
                "code": it["code"],
                "actress": it.get("actress") or "",
                "actress_id": it.get("actress_id"),
                "actress_en": it.get("actress_en") or "",
                "actress_jp": it.get("actress_jp") or "",
                "actresses": it.get("actresses") or [],
                "aka": it.get("aka") or "",
                "identified": it["identified"],
                "fc2db_at": it.get("fc2db_at") or "",
                "fc2dbnet_at": it.get("fc2dbnet_at") or "",
                "age": it.get("age"),
                "cup": it.get("cup") or "",
                "height": it.get("height"),
                "bust": it.get("bust"), "waist": it.get("waist"), "hip": it.get("hip"),
                "size": it["size"],
                "ext": it["ext"].lstrip("."),
                "nparts": it["nparts"],
                "parts": ([{"name": pp["name"], "size": pp["size"]}
                           for pp in (it.get("parts") or [])]
                          if it.get("nparts", 0) > 1 else []),
                "dur": it.get("duration"),
                "w": it.get("width"), "h": it.get("height_px"),
                "vcodec": it.get("vcodec") or "",
                "mtime": it["mtime"],
                "added": it.get("added") or it["mtime"],
                "release": it.get("release_date") or "",
                "title": it.get("title") or "",
                "title_en": it.get("title_en") or "",
                "cover": it.get("cover_url") or "",
                "producer": it.get("producer") or "",
                "fav": it.get("fav_count"),
                "tags": it.get("tags") or [],
                "avail": it.get("availability") or "",
                "uncensored": (it.get("censorship") or "uncensored") != "censored",
                "db": bool(it.get("db")),
                "missing": bool(it.get("missing")),
                "dup": bool(it.get("dup")),
                "uid": it.get("uid") or it["code"],
                "ai_tags": ai_tags,
                "manual_tags": manual_tags,
                "to_review": to_review,
            })
        body = json.dumps({
            "items": out_items,
            "actresses": STATE["actresses"],
            "facets": STATE["facets"],
            "training": bool(STATE.get("training")),
            "scanned_at": STATE["scanned_at"],
            "library": STATE["library"],
            "scan_warning": STATE.get("scan_warning") or "",
        }).encode("utf-8")
        STATE["data_json"], STATE["data_sig"] = body, sig
        return self._send_data_json(body)

    def _api_meta(self, q):
        codes = (q.get("codes", [""])[0]).split(",")
        codes = [c for c in codes if c]
        result = {}
        need = []
        with PROBE_LOCK:
            for c in codes:
                if c in PROBE:
                    result[c] = PROBE[c]
                else:
                    need.append(c)
        if need:
            futs = {c: PROBE_POOL.submit(probe_and_persist, c) for c in need}
            for c, fut in futs.items():
                try:
                    result[c] = fut.result(timeout=70)
                except Exception:
                    result[c] = None
        self._json(result)

    def _thumb(self, name):
        code = name[:-4] if name.lower().endswith(".jpg") else name
        code = code.strip("/")
        if not (STATE["by_uid"].get(code) or STATE["by_code"].get(code)):
            return self._send(404, "text/plain", "no code")
        out = thumb_path(code)
        if not (os.path.exists(out) and os.path.getsize(out) > 0):
            with _thumb_lock:
                fut = _thumb_inflight.get(code)
                if fut is None:
                    fut = THUMB_POOL.submit(generate_thumb, code)
                    _thumb_inflight[code] = fut
            try:
                fut.result(timeout=95)
            finally:
                with _thumb_lock:
                    _thumb_inflight.pop(code, None)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            with open(out, "rb") as f:
                body = f.read()
            return self._send(200, "image/jpeg", body,
                              {"Cache-Control": "public, max-age=86400"})
        return self._send(200, "image/svg+xml", PLACEHOLDER,
                          {"Cache-Control": "no-store"})

    def _portrait(self, name):
        aid = name[:-4] if name.lower().endswith(".jpg") else name
        try:
            aid = int(aid.strip("/"))
        except ValueError:
            return self._send(404, "text/plain", "bad id")
        con = sqlite3.connect(DB_PATH)
        row = con.execute("SELECT image_blob FROM actresses WHERE id=?",
                          (aid,)).fetchone()
        con.close()
        if row and row[0]:
            return self._send(200, "image/jpeg", bytes(row[0]),
                              {"Cache-Control": "public, max-age=86400"})
        return self._send(200, "image/svg+xml", PLACEHOLDER)

    def _item_ref(self, ref):
        """Resolve a uid (one folder) or a bare code (first/only folder) to one item.
        Anything that opens, streams, deletes or reads files must go through this."""
        ref = (ref or "").strip("/")
        hit = STATE["by_uid"].get(ref) or STATE["by_code"].get(ref)
        if hit is None and "~" in ref:
            # a uid only carries its ~suffix while the code is duplicated; once the other copy
            # goes away the survivor is addressed by the bare code again, and an open tab or a
            # bookmark still holds the old id.
            hit = STATE["by_code"].get(ref.split("~", 1)[0])
        return hit

    def _video(self, name, part=None):
        code = name.strip("/")
        it = self._item_ref(code)
        if not it:
            return self._send(404, "text/plain", "no code")
        path = it["primary"]
        parts = it.get("parts") or []
        if part is not None and parts:
            try:
                idx = int(part)
                if 0 <= idx < len(parts):
                    path = parts[idx]["path"]
            except (ValueError, TypeError):
                pass
        try:
            fsize = os.path.getsize(path)
        except OSError:
            return self._send(404, "text/plain", "gone")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, fsize - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                if start > end or start >= fsize:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{fsize}")
                    self.end_headers()
                    return
                end = min(end, fsize - 1)
                status = 206
        # open before any header goes out: once a 206 has started, an error can no longer be
        # reported and the player just hangs (seen when macOS revoked NAS access, EPERM)
        try:
            open(path, "rb").close()
        except OSError as e:
            return self._send(503, "text/plain", "cannot read video: %s" % (e.strerror or e))
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            # hint the OS we'll read this range sequentially (no-op on filesystems
            # that don't support it, e.g. SMB)
            try:
                os.posix_fadvise(f.fileno(), start, length, os.POSIX_FADV_SEQUENTIAL)
            except (AttributeError, OSError):
                pass
            try:
                self.wfile.flush()
            except Exception:
                pass
            sock = self.connection            # raw socket for zero-copy sendfile
            offset, remaining = start, length
            used_sendfile = False
            try:
                while remaining > 0:
                    sent = os.sendfile(sock.fileno(), f.fileno(), offset,
                                       min(remaining, 8 * 1024 * 1024))
                    if sent == 0:
                        break
                    offset += sent
                    remaining -= sent
                    used_sendfile = True
            except (BrokenPipeError, ConnectionResetError):
                return
            except (OSError, AttributeError):
                # sendfile unsupported (or partial) -> fall back to buffered copy
                if used_sendfile:
                    return
            if remaining > 0 and not used_sendfile:
                f.seek(offset)
                chunk = 1024 * 512
                while remaining > 0:
                    buf = f.read(min(chunk, remaining))
                    if not buf:
                        break
                    try:
                        self.wfile.write(buf)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(buf)

    def _set_tags(self, code, tags):
        it = STATE["by_code"].get(code or "")
        if not it:
            return self._json({"error": "no code"}, 404)
        clean = []
        seen = set()
        for t in tags:
            t = _tag_display(str(t).strip())     # honor merges/aliases on manual entry too
            if t and t.lower() not in seen:
                seen.add(t.lower())
                clean.append(t)
        con = sqlite3.connect(DB_PATH)
        # ensure a works row exists so the tag has a home even for amateur codes
        con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                    "VALUES(?,1,'available','manual')", (code,))
        con.execute("DELETE FROM tags WHERE code=?", (code,))
        for t in clean:
            con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, t))
        con.commit()
        con.close()
        # tags are keyed by code (shared across all parts); reflect on every item with this
        # code, including any split-folder copies, so they stay in sync without a reload
        for x in STATE["items"]:
            if x.get("code") == code:
                x["tags"] = list(clean)
        _rebuild_tag_facet()
        return self._json({"ok": True, "tags": clean})

    def _apply_work_fields(self, code, changed):
        """Reflect scanner DB changes into in-memory STATE items."""
        for x in STATE["items"]:
            if x["code"] != code:
                continue
            if "title" in changed:
                x["title"] = changed["title"]
            if "title_en" in changed:
                x["title_en"] = changed["title_en"]
            if "cover_url" in changed:
                x["cover_url"] = changed["cover_url"]
            if "release_date" in changed:
                x["release_date"] = changed["release_date"]
            if "tags" in changed:
                x["tags"] = sorted(set((x.get("tags") or []) + changed["tags"]))

    def _scan_one(self, code, force):
        if not scanmod:
            return self._json({"error": "scanner unavailable"}, 500)
        if not code or code not in STATE["by_code"]:
            return self._json({"error": "no code"}, 404)
        # Take the scan slot like every other scan button, BEFORE writing anything. The old
        # guard only skipped the fc2ppv-db half: the shell pass still ran on this request
        # thread and wrote the catalog alongside a library scan, and when it changed a title
        # the toast said "Updated: title" without ever mentioning the refused half.
        if not claim_scan("scanning " + code):
            return self._json({"error": "a library scan is running — try again when it "
                                        "finishes"}, 409)
        con = sqlite3.connect(DB_PATH, timeout=30)
        notes, hit = [], False
        changed = {}
        try:
            # Same priority as every other scan: fc2ppv-db, then fc2db.net, then 123av/ffjav.
            # One deliberate click re-reads both cast sites whether or not they were read
            # before. The shell pass comes LAST and only fills what is still empty: it used
            # to run first with force=True, which let the lowest-priority source overwrite
            # the title, cover and date before the others had a say.
            ready, why = self._fc2db_ready(fresh=True)
            mode, solver, _l = self._fc2db_choice()
            if ready:
                prog = {}          # a local progress dict: the slot is ours, the counters
                scanmod.scan_fc2db(  # belong to whatever the toolbar last showed
                    con, [code],
                    None if mode == "flaresolverr" else secret_get("cookie"),
                    None if mode == "flaresolverr" else secret_get("ua"),
                    THUMB_DIR, progress=prog,
                    solver_url=solver if mode == "flaresolverr" else None)
                if prog.get("hits"):
                    hit = True
                elif prog.get("failed"):
                    notes.append(prog.get("error") or "fc2ppv-db lookup failed")
                else:
                    notes.append("not in fc2ppv-db")
            else:
                notes.append("fc2ppv-db skipped — " + why)
            if "fc2db.net" in enabled_sources():
                prog = {}
                scanmod.scan_fc2dbnet(con, [code], progress=prog,
                                      solver_url=solver or None)
                if prog.get("hits"):
                    hit = True
                elif prog.get("failed"):
                    notes.append(prog.get("error") or "fc2db.net lookup failed")
                else:
                    notes.append("not on fc2db.net")
            changed = scanmod.scan_code(con, code, force=False,
                                        sources=enabled_sources())
        finally:
            con.close()
            release_scan()
        self._apply_work_fields(code, changed)
        if hit:
            try:                      # re-enrich from the catalog; no SMB re-walk
                rebuild_index(_load_fs_index(STATE["library"]))
            except Exception:
                pass
        it = STATE["by_code"].get(code, {})
        return self._json({"ok": True, "changed": changed, "fc2db_hit": hit,
                           "note": " · ".join(notes),
                           "item": {
            "code": code, "title": it.get("title") or "", "title_en": it.get("title_en") or "",
            "release": it.get("release_date") or "", "cover": it.get("cover_url") or "",
            "tags": it.get("tags") or []}})

    # ---------------------------------------------------------------- settings
    def _tag_pairs(self, con):
        """Unified tag list: one row per canonical tag = {en, jp, count}. English preferred;
        a Japanese-only tag has en='' and shows under its Japanese."""
        _ensure_tagtx(con)
        counts = {r[0]: r[1] for r in con.execute("SELECT tag, COUNT(*) FROM tags GROUP BY tag")}
        j2e = {r[0]: r[1] for r in con.execute("SELECT raw,display FROM tag_translations") if r[1]}
        e2j = {}
        for jp, en in j2e.items():
            e2j.setdefault(en.lower(), jp)
        rows = {}

        def add(en, jp, n):
            key = (en or jp or "").lower()
            if not key:
                return
            r = rows.setdefault(key, {"en": en or "", "jp": jp or "", "count": 0})
            r["count"] += n
            if en and not r["en"]:
                r["en"] = en
            if jp and not r["jp"]:
                r["jp"] = jp
        for tag, n in counts.items():                       # tags actually in use
            if _is_jp(tag):
                add(j2e.get(tag, ""), tag, n)
            else:
                add(tag, e2j.get(tag.lower(), ""), n)
        for jp, en in j2e.items():                          # dictionary entries (may be unused)
            if jp not in counts and en.lower() not in {t.lower() for t in counts}:
                add(en, jp, 0)
        return sorted(rows.values(), key=lambda r: (-r["count"], (r["en"] or r["jp"]).lower()))

    def _api_settings(self):
        tags, actresses, cache = [], [], {}
        if os.path.exists(DB_PATH):
            con = sqlite3.connect(DB_PATH)
            con.row_factory = sqlite3.Row
            tags = self._tag_pairs(con)
            actresses = [dict(r) for r in con.execute(
                "SELECT a.id, a.name_en, a.name_jp, a.age, a.cup, a.height, "
                "COUNT(w.code) total, "
                "SUM(CASE WHEN w.in_library=1 THEN 1 ELSE 0 END) owned "
                "FROM actresses a LEFT JOIN works w ON w.actress_id=a.id "
                "GROUP BY a.id ORDER BY owned DESC, total DESC, a.name_en")]
            con.close()
        for name, path in (("index", INDEX_CACHE), ("probe", PROBE_CACHE),
                           ("seen", SEEN_CACHE)):
            try:
                cache[name] = os.path.getsize(path)
            except OSError:
                cache[name] = 0
        try:
            cache["thumbs"] = sum(
                os.path.getsize(os.path.join(THUMB_DIR, f))
                for f in os.listdir(THUMB_DIR)) if os.path.isdir(THUMB_DIR) else 0
            cache["thumbs_n"] = len(os.listdir(THUMB_DIR)) if os.path.isdir(THUMB_DIR) else 0
        except OSError:
            cache["thumbs"] = cache["thumbs_n"] = 0
        return self._json({"ok": True, "library": STATE["library"],
                           "download": STATE.get("download", ""),
                           "training_mode": bool(STATE.get("training")),
                           "custom_labels": list(STATE.get("custom_labels") or []),
                           "tags": tags, "actresses": actresses,
                           "sources": _load_sources(), "cache": cache,
                           "fc2db": self._fc2db_status(),
                           "counts": {"actresses": len(actresses), "tags": len(tags)}})

    def _tag_edit(self, key, en, jp):
        """Set a tag's English + Japanese forms. `key` is its current stored value.
        Renames the in-use tag to the new canonical (English if present, else Japanese) and
        keeps the JP<->EN mapping so scrapers canonicalize future imports."""
        key = (key or "").strip()
        en = (en or "").strip()
        jp = (jp or "").strip()
        if not os.path.exists(DB_PATH):
            return self._json({"error": "no catalog db"}, 500)
        if not (en or jp):
            return self._json({"error": "need an English or Japanese value"}, 400)
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=8000")
        _ensure_tagtx(con)
        if jp:
            if en:
                con.execute("INSERT INTO tag_translations(raw,display) VALUES(?,?) "
                            "ON CONFLICT(raw) DO UPDATE SET display=excluded.display", (jp, en))
            else:
                con.execute("DELETE FROM tag_translations WHERE raw=? COLLATE NOCASE", (jp,))
        canon = en or jp
        if key and canon and canon.lower() != key.lower():   # rename/merge the in-use tag
            con.execute("UPDATE OR IGNORE tags SET tag=? WHERE tag=?", (canon, key))
            con.execute("DELETE FROM tags WHERE tag=?", (key,))
            # record the merge so future scraper runs map the old name to the new one
            # (honored for this install and shipped in the catalog for other users)
            con.execute("INSERT INTO tag_translations(raw,display) VALUES(?,?) "
                        "ON CONFLICT(raw) DO UPDATE SET display=excluded.display", (key, canon))
        con.commit()
        con.close()
        global _tagtx
        _tagtx = None
        rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True, "en": en, "jp": jp})

    def _tag_delete(self, tag):
        tag = (tag or "").strip()
        if not tag or not os.path.exists(DB_PATH):
            return self._json({"error": "no tag / db"}, 400)
        con = sqlite3.connect(DB_PATH, timeout=30)
        n = con.execute("DELETE FROM tags WHERE tag=?", (tag,)).rowcount
        con.commit()
        con.close()
        rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True, "removed": n})

    def _actress_edit(self, body):
        """Edit an actress's full profile by id (name + age/cup/measurements + description)."""
        aid = body.get("id")
        if not aid or not os.path.exists(DB_PATH):
            return self._json({"error": "no id / db"}, 400)
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            old = con.execute("SELECT name_en,name_jp FROM actresses WHERE id=?", (int(aid),)).fetchone()
            if not old:
                return self._json({"error": "no such actress"}, 404)
            en = (body.get("name_en") or "").strip() or None
            jp = (body.get("name_jp") or "").strip() or None
            sets, vals = [], []
            if "name_en" in body:
                sets.append("name_en=?"); vals.append(en)
            if "name_jp" in body:
                sets.append("name_jp=?"); vals.append(jp)
            for col, cast in (("age", int), ("cup", str), ("height", int),
                              ("bust", int), ("waist", int), ("hip", int)):
                if col in body:
                    v = body.get(col)
                    v = v.strip() if isinstance(v, str) else v
                    v = None if v in (None, "") else v
                    if v is not None and cast is int:
                        try:
                            v = int(v)
                        except (ValueError, TypeError):
                            continue
                    sets.append(f"{col}=?"); vals.append(v)
            if "description" in body:
                sets.append("description=?"); vals.append((body.get("description") or "").strip() or None)
            if sets:
                con.execute(f"UPDATE actresses SET {','.join(sets)} WHERE id=?", vals + [int(aid)])
            for nm in (old or ()):                 # keep old spellings as aliases
                if nm and nm not in (en, jp):
                    con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) VALUES(?,?)",
                                (int(aid), nm))
            con.commit()
        finally:
            con.close()
        rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True})

    def _actress_rename(self, aid, name_en, name_jp):
        if not aid or not os.path.exists(DB_PATH):
            return self._json({"error": "no id / db"}, 400)
        en = (name_en or "").strip() or None
        jp = (name_jp or "").strip() or None
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            # keep the old spelling as an alias so nothing that referenced it breaks
            old = con.execute("SELECT name_en,name_jp FROM actresses WHERE id=?",
                             (aid,)).fetchone()
            con.execute("UPDATE actresses SET name_en=?, name_jp=? WHERE id=?",
                        (en, jp, int(aid)))
            for nm in (old or ()):
                if nm and nm not in (en, jp):
                    con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) "
                                "VALUES(?,?)", (int(aid), nm))
            con.commit()
        except sqlite3.IntegrityError:
            con.close()
            return self._json({"error": "that name already belongs to another actress — "
                                        "merge them instead"}, 409)
        con.close()
        rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True})

    def _actress_merge(self, target, sources):
        if not dedupemod:
            return self._json({"error": "dedupe unavailable"}, 500)
        if not target or not sources:
            return self._json({"error": "need target + sources"}, 400)
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        res = dedupemod.merge_group(con, [int(target)] + [int(s) for s in sources],
                                    target=int(target))
        con.close()
        rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True, "result": res})

    def _dedupe(self, apply, groups):
        if not dedupemod:
            return self._json({"error": "dedupe unavailable"}, 500)
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        if not apply:
            found = dedupemod.find_duplicate_groups(con)
            hints = dedupemod.alias_matches(con)
            con.close()
            return self._json({"ok": True, "groups": found, "alias_hints": hints})
        done = []
        for g in (groups or dedupemod.find_duplicate_groups(con)):
            ids = g.get("ids") if isinstance(g, dict) else g
            if ids and len(ids) > 1:
                done.append(dedupemod.merge_group(con, ids))
        con.close()
        rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True, "merged": len(done)})

    def _set_library(self, path):
        path = (path or "").strip()
        if not path or not os.path.isdir(path):
            return self._json({"error": "not a folder: %s" % path}, 400)
        STATE["library"] = path
        save_config(path)
        # a different library invalidates the cached scan -> full re-walk
        try:
            os.remove(INDEX_CACHE)
        except OSError:
            pass
        threading.Thread(target=rebuild_index, daemon=True).start()
        return self._json({"ok": True, "library": path, "rescanning": True})

    def _set_download(self, path):
        path = (path or "").strip()
        if not path or not os.path.isdir(path):
            return self._json({"error": "not a folder: %s" % path}, 400)
        STATE["download"] = path
        save_config(download=path)
        return self._json({"ok": True, "download": path})

    def _set_training(self, on):
        """Settings -> Library -> training mode: allow editing scene markers in the player."""
        if not isinstance(on, bool):
            return self._json({"error": "on must be true or false"}, 400)
        STATE["training"] = on
        STATE["data_json"] = None      # the sidebar's training filters appear/disappear with it
        save_config(training_mode=on)
        return self._json({"ok": True, "training_mode": on})

    def _download_model(self):
        """Settings -> Scene model: fetch the newest scene model release."""
        if vault_model.PROGRESS["running"]:
            return self._json({"error": "a model download is already running"}, 409)

        def run():
            try:
                vault_model.download()
            except Exception as e:
                print("Vault: model download failed: %s" % e, file=sys.stderr)
        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "model": _model_info()})

    def _scene_label(self, body):
        """Add or remove a custom scene-marker label (training mode only).

        Custom labels extend the built-in SCENES_SPEC vocabulary for the marker editor. Removing
        one only takes it off the list; markers that already use it keep their label.
        """
        if not STATE.get("training"):
            return self._json({"error": "Editing labels needs training mode (Settings → Library)"}, 403)
        op = body.get("op")
        label = _normalize_scene_label(body.get("label"))
        if not label or len(label) > 32 or not _SCENE_LABEL_RE.match(label):
            return self._json({"error": "A label is lowercase words joined by hyphens, max 32 characters"}, 400)
        custom = list(STATE.get("custom_labels") or [])
        existed = label in SCENE_LABELS or label in custom
        if op == "add":
            if not existed:
                custom = sorted(custom + [label])
        elif op == "remove":
            if label not in custom:
                return self._json({"error": "“%s” is not a custom label" % label}, 404)
            custom.remove(label)
        else:
            return self._json({"error": "bad op"}, 400)
        if custom != (STATE.get("custom_labels") or []):
            STATE["custom_labels"] = custom
            save_config(custom_labels=custom)
        return self._json({"ok": True, "label": label, "existed": existed, "custom_labels": custom})

    # ------------------------------------------------------------- Import pipeline
    def _script(self, name):
        for base in (RENAMERS, HERE):
            pth = os.path.join(base, name)
            if os.path.exists(pth):
                return pth
        return None

    _SIZE = r"[\d.]+[KMGT]?B|-"

    def _import_preview(self):
        dl = STATE.get("download")
        if not dl or not os.path.isdir(dl):
            return self._json({"error": "no download folder set — set it in Settings → Library"}, 400)
        if not STATE.get("library") or not os.path.isdir(STATE["library"]):
            return self._json({"error": "library folder not set"}, 400)
        rn, mg = self._script("rename_fc2.py"), self._script("migrate_fc2.py")
        if not rn or not mg:
            return self._json({"error": "pipeline scripts (rename_fc2.py / migrate_fc2.py) not found"}, 500)
        env = dict(os.environ, VAULT_IMPORT_SRC=dl, VAULT_LIBRARY=STATE["library"])
        try:
            ro = subprocess.run([sys.executable, rn, dl], capture_output=True, text=True,
                                env=env, timeout=240).stdout
            mo = subprocess.run([sys.executable, mg], capture_output=True, text=True,
                                env=env, timeout=240).stdout
        except Exception as e:
            return self._json({"error": "preview failed: %s" % e}, 500)
        rename = {
            "folders": len(re.findall(r"^MATCH ", ro, re.M)),
            "loose": len(re.findall(r"^LOOSE ", ro, re.M)),
            "deletes": len(re.findall(r"delete file ", ro)),
            "collisions": re.findall(r"target folder '([^']+)' already exists", ro),
        }
        rows = []
        for line in mo.splitlines():
            m = re.match(r"^(\S+)\s+(%s)\s+(%s)\s+(MOVE \(add\)|REPLACE TARGET|DELETE SOURCE)"
                         % (self._SIZE, self._SIZE), line)
            if m:
                rows.append({"code": m.group(1), "src": m.group(2), "lib": m.group(3),
                             "action": m.group(4)})

        def cnt(pat):
            m = re.search(pat, mo)
            return int(m.group(1)) if m else 0
        counts = {"move": cnt(r"MOVE \(add to library\)\s*:\s*(\d+)"),
                  "replace": cnt(r"REPLACE TARGET \(lib del\):\s*(\d+)"),
                  "delete_source": cnt(r"DELETE SOURCE\s*:\s*(\d+)")}
        return self._json({"ok": True, "download": dl, "library": STATE["library"],
                           "rename": rename, "migrate": {"rows": rows, "counts": counts}})

    def _stream_cmd(self, cmd, env, phase):
        IMPORT_PROGRESS.update(phase=phase, line="")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env, bufsize=1)
        for line in p.stdout:
            line = line.rstrip()
            if not line or line.startswith("[dry]"):
                continue
            if re.match(r"\s*(MOVE|REPLACE|DELETE SOURCE|rename|delete|mkdir|LOOSE)", line):
                IMPORT_PROGRESS["done"] += 1
            IMPORT_PROGRESS["line"] = line[-140:]
        p.wait()
        return p.returncode

    def _import_run(self, confirm):
        # Claim the slot here, in the request thread. Setting it inside the worker let two
        # quick clicks both pass this test and start two import pipelines over one folder.
        with _JOB_LOCK:
            if IMPORT_PROGRESS.get("running"):
                return self._json({"error": "an import is already running",
                                   "progress": dict(IMPORT_PROGRESS)}, 409)
            IMPORT_PROGRESS.update(running=True, phase="Starting…", line="", done=0, total=0)
        dl = STATE.get("download")
        if not dl or not os.path.isdir(dl):
            IMPORT_PROGRESS["running"] = False
            return self._json({"error": "no download folder set"}, 400)
        rn, mg = self._script("rename_fc2.py"), self._script("migrate_fc2.py")
        if not rn or not mg:
            IMPORT_PROGRESS["running"] = False
            return self._json({"error": "pipeline scripts not found"}, 500)
        env = dict(os.environ, VAULT_IMPORT_SRC=dl, VAULT_LIBRARY=STATE["library"])

        def run():
            P = IMPORT_PROGRESS
            P.update(running=True, phase="Renaming & cleaning…", line="", done=0, total=0)
            try:
                self._stream_cmd([sys.executable, rn, dl, "--apply"], env, "Renaming & cleaning…")
                margs = [sys.executable, mg, "--apply"]
                if confirm:
                    margs.append("--confirm-lib-deletes")
                self._stream_cmd(margs, env, "Migrating into library…")
            except Exception as e:
                P.update(line="error: %s" % e)
            finally:
                P.update(running=False, phase="Done")
                try:
                    os.remove(INDEX_CACHE)
                except OSError:
                    pass
                try:
                    rebuild_index()
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "started": True})

    def _clear_cache(self, targets):
        targets = set(targets) if targets else {"index", "probe", "thumbs"}
        removed = []
        if "index" in targets:
            for pth in (INDEX_CACHE,):
                try:
                    os.remove(pth); removed.append("index")
                except OSError:
                    pass
        if "probe" in targets:
            try:
                os.remove(PROBE_CACHE); removed.append("probe")
            except OSError:
                pass
            PROBE.clear()
        if "thumbs" in targets and os.path.isdir(THUMB_DIR):
            n = 0
            for f in os.listdir(THUMB_DIR):
                try:
                    os.remove(os.path.join(THUMB_DIR, f)); n += 1
                except OSError:
                    pass
            removed.append("thumbs(%d)" % n)
        threading.Thread(target=rebuild_index, daemon=True).start()
        return self._json({"ok": True, "cleared": removed, "rescanning": True})

    def _sources_save(self, sources):
        if not isinstance(sources, list):
            return self._json({"error": "sources must be a list"}, 400)
        clean = []
        for s in sources:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            clean.append({"name": str(s.get("name"))[:60],
                          "url": str(s.get("url") or "")[:300],
                          "provides": str(s.get("provides") or "")[:120],
                          "builtin": bool(s.get("builtin")),
                          "enabled": bool(s.get("enabled", True))})
        _save_sources(clean)
        return self._json({"ok": True, "sources": clean})

    # ------------------------------------------------- fc2ppv-db (session or FlareSolverr)
    def _solver_reachable(self, solver, max_age=30.0):
        """Cached reachability. The raw probe waits up to 4 s, and Settings asks on every
        open — a sleeping resolver made the dialog hang each time."""
        if not (solver and scanmod and hasattr(scanmod, "solver_ready")):
            return False
        now = time.time()
        if _SOLVER_PROBE["url"] == solver and now - _SOLVER_PROBE["at"] < max_age:
            return _SOLVER_PROBE["ok"]
        ok = bool(scanmod.solver_ready(solver))
        _SOLVER_PROBE.update(url=solver, at=now, ok=ok)
        return ok

    def _fc2db_ready(self, fresh=False):
        """(can we talk to fc2ppv-db, why not). The toolbar scan uses this to decide whether
        to run its actress pass; it must never be a hard requirement, because 123av/ffjav
        work without any fc2ppv-db session at all."""
        mode, solver, _locked = self._fc2db_choice()
        if mode == "flaresolverr":
            if not solver:
                return False, "no FlareSolverr URL set"
            if not self._solver_reachable(solver, max_age=0 if fresh else 30.0):
                return False, "FlareSolverr is not reachable at " + solver
            return True, ""
        if not secret_get("cookie"):
            return False, "no fc2ppv-db session saved"
        return True, ""

    def _fc2db_choice(self):
        """Active transport. Env VAULT_FLARESOLVERR wins over the saved URL."""
        cfg = load_config()
        mode = cfg.get("fc2db_mode") if cfg.get("fc2db_mode") in ("cookie", "flaresolverr") else "cookie"
        solver = (os.environ.get("VAULT_FLARESOLVERR") or cfg.get("flaresolverr") or "").strip()
        return mode, solver, bool(os.environ.get("VAULT_FLARESOLVERR"))

    def _fc2db_status(self):
        mode, solver, locked = self._fc2db_choice()
        reachable = self._solver_reachable(solver)
        return {"connected": bool(secret_get("cookie")),
                "encrypted": _kc_available(),
                "mode": mode,
                "solver": solver,
                "solver_locked": locked,
                "reachable": reachable}

    def _fc2db_save(self, cookie, ua, mode=None, solver=None):
        mode = (mode or "").strip()
        if mode and mode not in ("cookie", "flaresolverr"):
            return self._json({"error": "mode must be cookie or flaresolverr"}, 400)
        if solver is not None:
            solver = str(solver).strip()[:300]
            if solver and "://" not in solver:
                solver = "http://" + solver
            if solver and not solver.startswith(("http://", "https://")):
                return self._json({"error": "resolver URL must start with http:// or https://"}, 400)
            if solver and not _is_private_url(solver):
                # Vault has no auth and listens on the LAN: without this, any visitor could
                # point the resolver at an outside host and have the server POST to it.
                return self._json({"error": "the resolver must be on your own network "
                                            "(localhost or a private address)"}, 400)
            if not save_config(flaresolverr=solver):
                return self._json({"error": "could not save config"}, 500)
        if mode and not save_config(fc2db_mode=mode):
            return self._json({"error": "could not save config"}, 500)
        ok = True
        # A FlareSolverr save must not wipe a pasted session; that remains the fallback.
        if mode != "flaresolverr" and cookie is not None:
            cookie = (cookie or "").strip()
            ua = (ua or "").strip()
            # secret_set("") DELETES the keychain item; the settings form always posts `ua`,
            # so saving a cookie with the UA box left blank wiped a working User-Agent.
            ok = secret_set("cookie", cookie)
            if ua:
                ok = secret_set("ua", ua) and ok
        return self._json({"ok": ok, "connected": bool(secret_get("cookie")),
                           "encrypted": _kc_available(),
                           "mode": mode or self._fc2db_choice()[0]})

    def _solver_check(self):
        """Is the saved resolver answering? Generic — no site, no challenge solved. Probes
        fresh (and refreshes the 30 s cache the scan dialog reads) and reports the
        FlareSolverr version, so "reachable" is a fact rather than a guess."""
        _mode, solver, locked = self._fc2db_choice()
        if not solver:
            return self._json({"ok": False, "url": "", "locked": locked,
                               "note": "no resolver URL saved"})
        endpoint = scanmod._solver_endpoint(solver) if scanmod else ""
        version, ok = "", False
        try:
            d = scanmod._solver_post(endpoint, {"cmd": "sessions.list"}, timeout=6)
            ok = d.get("status") == "ok"
            version = d.get("version") or ""
        except Exception:
            ok = False
        _SOLVER_PROBE.update(url=solver, at=time.time(), ok=ok)
        return self._json({"ok": ok, "url": solver, "locked": locked, "version": version,
                           "note": ("FlareSolverr %s answers at %s" % (version, solver)).replace("  ", " ")
                                   if ok else "nothing answers at " + solver})

    def _fc2dbnet_test(self, code):
        """One fc2db.net page, end to end: direct first, the resolver only if blocked."""
        if not scanmod or not hasattr(scanmod, "fetch_fc2dbnet"):
            return self._json({"error": "scanner unavailable"}, 500)
        code = (code or "").strip() or "FC2-PPV-4988506"
        _mode, solver, _l = self._fc2db_choice()
        data = scanmod.fetch_fc2dbnet(code, solver_url=solver or None)
        if data.get("_error"):
            return self._json({"ok": False, "code": code, "note": data["_error"]})
        if data.get("_missing"):
            return self._json({"ok": False, "code": code,
                               "note": "fc2db.net works — it just has no page for " + code})
        ok = bool(data)
        cast = [a.get("name") for a in data.get("actresses") or [] if a.get("name")]
        return self._json({"ok": ok, "code": code,
                           "note": "fc2db.net works — the server can read it" if ok
                                   else "the page came back empty",
                           "data": {"title_jp": data.get("title_jp"), "cast": cast,
                                    "release_date": data.get("release_date")}})

    def _fc2db_test(self, code):
        if not scanmod or not hasattr(scanmod, "fetch_fc2ppvdb"):
            return self._json({"error": "scanner unavailable"}, 500)
        mode, solver, _locked = self._fc2db_choice()
        code = (code or "").strip() or "FC2-PPV-4964424"
        if mode == "flaresolverr":
            if not solver:
                return self._json({"error": "set the FlareSolverr URL first"}, 400)
            if not scanmod.solver_ready(solver):
                return self._json({"error": "FlareSolverr is not reachable at " + solver}, 503)
            data = scanmod.fetch_fc2ppvdb(code, solver_url=solver)
            if data.get("_error"):
                return self._json({"ok": False, "code": code, "note": data["_error"]})
            if data.get("_missing"):
                # the resolver worked; fc2ppv-db just has no page for this code
                return self._json({"ok": False, "code": code, "mode": mode, "absent": True,
                                   "note": "FlareSolverr works — but fc2ppv-db has no page "
                                           "for " + code + ". Try another code."})
            note_ok = "FlareSolverr works — the server can read fc2ppv-db"
            note_bad = ("no data — the resolver reached the site but the page had "
                        "no title or actress. The check may still be in the way.")
        else:
            cookie, ua = secret_get("cookie"), secret_get("ua")
            if not cookie:
                return self._json({"error": "no session saved — paste your cookie first"}, 400)
            data = scanmod.fetch_fc2ppvdb(code, cookie=cookie, ua=ua)
            if data.get("_missing"):
                return self._json({"ok": False, "code": code, "mode": mode, "absent": True,
                                   "note": "the session works — but fc2ppv-db has no page "
                                           "for " + code + ". Try another code."})
            note_ok = "session works — the server can read fc2ppv-db"
            note_bad = ("no data — the session was rejected (Cloudflare). "
                        "The cookie has likely expired — re-copy a fresh one "
                        "from the same browser. If it still fails, your browser "
                        "may be on a different IP than the server (e.g. a VPN).")
        ok = bool(data.get("title_jp") or data.get("actresses"))
        # never ship the transport error key, or a huge page, to the browser
        data.pop("_error", None)
        data.pop("_missing", None)
        return self._json({"ok": ok, "code": code, "mode": mode, "data": data or None,
                           "note": note_ok if ok else note_bad})

    def _start_pipeline(self, titles, pool, fc2db_ready, net_ready, reread=False, note=""):
        """THE scan. Every scan button in Vault runs this (the player runs the same three
        passes for one code), so no button quietly checks fewer sources than another.
        The passes run in PRIORITY order:

          1. fc2ppv-db — the primary source; it overwrites, over `pool`
          2. fc2db.net — fills what is still empty, over `pool`
          3. 123av / ffjav — fill what is STILL empty, over `titles`

        Order is what makes the priority real. fc2db.net and the shell sources both only
        fill empty fields, so whichever runs first wins — with the shell pass first (as it
        was), ffjav's search-page cover and date always beat fc2db.net's official ones, and
        the effective priority was fc2ppv-db > ffjav > fc2db.net.

        Passes 1 and 2 each skip codes their own stamp says were already read, unless
        `reread`. Unticked sources are skipped inside each pass. The CALLER claims the slot;
        this worker owns `running` and the job's counters (reset once, here), and lowers
        `running` only after the index has been rebuilt."""
        mode, solver, _l = self._fc2db_choice()

        def run():
            P = scanmod.PROGRESS
            _c = sqlite3.connect(DB_PATH, timeout=60)
            _c.execute("PRAGMA busy_timeout=60000")
            P["hold"] = True
            P["note"] = note
            P.update(hits=0, absent=0, failed=0, error="")   # one set of totals per job
            try:
                if fc2db_ready and pool:
                    need = list(pool) if reread else self._codes_needing(_c, pool, "fc2db_at")
                    if need:
                        P["phase"] = "fc2ppv-db"
                        scanmod.scan_fc2db(
                            _c, need,
                            None if mode == "flaresolverr" else secret_get("cookie"),
                            None if mode == "flaresolverr" else secret_get("ua"),
                            THUMB_DIR,
                            solver_url=solver if mode == "flaresolverr" else None)
                if net_ready and pool:
                    need = list(pool) if reread else self._codes_needing(_c, pool, "fc2dbnet_at")
                    if need:
                        P["phase"] = "fc2db.net"
                        scanmod.scan_fc2dbnet(_c, need, solver_url=solver or None)
                if titles:
                    P["phase"] = "titles"
                    scanmod.scan_library(titles, con=_c, sources=enabled_sources())
            finally:
                P["phase"] = ""
                _c.close()
                try:
                    rebuild_index(_load_fs_index(STATE["library"]))   # publish first ...
                except Exception:
                    pass
                P["hold"] = False                                     # ... then "done"
                P["running"] = False

        def guarded():
            try:
                run()
            finally:
                release_scan()      # a worker that dies early must not wedge the slot
        threading.Thread(target=guarded, daemon=True).start()

    def _scan_everything(self, scope):
        """Settings' library-wide scan: every enabled source, the whole library.

        "missing" — titles for movies with a gap; each cast source only where its page was
        never read. "all" — the same titles pass (shell sources only ever fill gaps in a
        library scan) and both cast sources re-read every page. Every check that can refuse
        happens BEFORE the slot is claimed, so there is no early return that can leak it."""
        if not scanmod:
            return self._json({"error": "scanner unavailable"}, 500)
        items = [it for it in STATE["items"] if not it.get("missing")]
        pool = [it["code"] for it in items]
        titles = [it["code"] for it in items
                  if not (it.get("title_en") and it.get("title") and it.get("cover_url")
                          and it.get("release_date"))]
        fc2db_ready, fc2db_why = self._fc2db_ready(fresh=True)
        net_ready = "fc2db.net" in enabled_sources()
        if not claim_scan("starting…"):
            return self._json({"error": "a scan is already running"}, 409)
        self._start_pipeline(titles=titles, pool=pool, fc2db_ready=fc2db_ready,
                             net_ready=net_ready, reread=(scope == "all"),
                             note="" if fc2db_ready else "fc2ppv-db pass skipped — " + fc2db_why)
        return self._json({"ok": True, "started": True, "count": len(pool),
                           "titles": len(titles), "fc2db": fc2db_ready, "fc2dbnet": net_ready,
                           "fc2db_why": fc2db_why})

    def _enrich_actress(self, actress_id, code, force, dry=False):
        """Scrape javdatabase + jav.guru for an actress and apply the profile IF the
        code-overlap check verifies the identity (or force=True). dry=True previews only
        (no writes). Resolves the actress from an explicit id, or a movie code's link."""
        if not scanmod or not hasattr(scanmod, "enrich_actress"):
            return self._json({"error": "enricher unavailable"}, 500)
        if not os.path.exists(DB_PATH):
            return self._json({"error": "no catalog db"}, 500)
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=8000")
        try:
            if not actress_id and code:
                r = con.execute("SELECT actress_id FROM works WHERE code=?", (code,)).fetchone()
                actress_id = r[0] if r and r[0] else None
            if not actress_id:
                return self._json({"error": "no actress linked — set her name first, "
                                            "then enrich"}, 404)
            # the codes you actually own (files on disk) — the ground truth for verifying
            # her identity and for telling wishlist entries from owned ones.
            lib_codes = {c for c, it in STATE["by_code"].items() if not it.get("missing")}
            report = scanmod.enrich_actress(con, int(actress_id), force=force, dry=dry,
                                           sources=enabled_sources(),
                                           library_codes=lib_codes)
        finally:
            con.close()
        if report.get("applied"):
            # re-overlay the (updated) DB onto a fresh copy of the raw filesystem scan
            rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True, "report": report})

    ORG_CAP = 4000                             # max actions shipped for per-row selection

    def _fold_actress(self, apply, skip=None, allow=None):
        """Fold each owned movie's <CODE> folder to match the catalog actress:
        LIBRARY/<Actress>/<CODE>/…  when identified, LIBRARY/<CODE>/…  when not.
        DB-driven (uses works.actress_id via STATE), so it also relocates movies
        reassigned in the UI. Preview by default; only moves whole <CODE> folders and
        prunes emptied actress folders — never deletes a movie."""
        lib = STATE.get("library")
        if not lib or not os.path.isdir(lib):
            return self._json({"error": "no library"}, 400)
        lib_real = os.path.realpath(lib)
        skip = set(skip or [])
        # Organize was switched to an allow-list; this path was not. It applied every move
        # whose id was absent from `skip`, and the client can only send the ids it rendered
        # — so past ORG_CAP the rows nobody saw moved anyway. `allow`, when the client sends
        # it, is the whole of what may run.
        allow = None if allow is None else set(allow)
        plan, conflicts = [], []
        for it in STATE["items"]:
            if it.get("missing"):
                continue
            primary = it.get("primary")
            if not primary:
                continue
            cur_dir = os.path.dirname(primary)
            cur_real = os.path.realpath(cur_dir)
            if cur_real == lib_real:
                continue                                   # loose file at root — Organize folds it first
            relparts = os.path.relpath(cur_dir, lib).split(os.sep)
            if any(seg.lower() in FOLD_RESERVED for seg in relparts):
                continue                                   # never touch trash/dupes/unsorted
            base = os.path.basename(cur_dir)
            identified = it.get("identified") and (it.get("actress") or "").strip()
            if identified:
                adir = _safe_actress_dir(it.get("actress"))
                if not adir:
                    continue
                target_dir = os.path.join(lib, adir, base)
            else:
                target_dir = os.path.join(lib, base)       # unidentified -> flat at root
            if os.path.realpath(target_dir) == cur_real:
                continue                                    # already in the right place
            entry = {"id": it.get("code") or base, "code": it.get("code"),
                     "actress": (it.get("actress") if identified else None),
                     "from": os.path.relpath(cur_dir, lib),
                     "to": os.path.relpath(target_dir, lib),
                     "cur_dir": cur_dir, "target_dir": target_dir}
            (conflicts if os.path.exists(target_dir) else plan).append(entry)

        if not apply:
            cap = self.ORG_CAP
            return self._json({"ok": True, "total": len(plan), "conflicts": len(conflicts),
                               "actions": [{"id": e["id"], "code": e["code"],
                                            "actress": e["actress"], "from": e["from"],
                                            "to": e["to"]} for e in plan[:cap]],
                               "conflict_list": [{"from": e["from"], "to": e["to"]}
                                                 for e in conflicts[:50]],
                               "truncated": len(plan) > cap})
        # apply
        moved, pruned, parents = 0, 0, set()
        for e in plan:
            if allow is not None and e["id"] not in allow:
                continue
            if e["id"] in skip:
                continue
            src, dst = e["cur_dir"], e["target_dir"]
            if not os.path.isdir(src) or os.path.exists(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.rename(src, dst)
                except OSError:
                    shutil.move(src, dst)                   # cross-device fallback
                moved += 1
                parents.add(os.path.dirname(src))
            except OSError:
                continue
        # prune actress folders left empty by the moves (never removes a folder with content)
        for parent in parents:
            if os.path.realpath(parent) == lib_real:
                continue
            if os.path.basename(parent).lower() in FOLD_RESERVED:
                continue
            try:
                left = [x for x in os.listdir(parent) if x != ".DS_Store"]
                if not left:
                    ds = os.path.join(parent, ".DS_Store")
                    if os.path.exists(ds):
                        os.remove(ds)
                    os.rmdir(parent)
                    pruned += 1
            except OSError:
                pass
        rebuild_index()
        return self._json({"ok": True, "moved": moved, "pruned": pruned})

    def _organize(self, apply, skip=None, enrich=None, allow=None, expect=None):
        if not orgmod:
            return self._json({"error": "organizer unavailable"}, 500)
        lib = STATE["library"]
        pairs = folder_actress_pairs(lib)     # names that live only in "<CODE> - Name" folders
        if apply:
            # 1) manual, per-code actress + tags typed in the modal (may create actresses:
            #    it's explicit human input, not a folder-name guess).
            self._apply_org_enrich(enrich)
            # 2) LINK when a folder name already matches a known actress (never invents one).
            capture_folder_actresses(pairs, apply=True)
            # 3) THEN normalize folders/files (minus any changes the user unchecked).
            # Only the rows the user actually saw and ticked run. `expect` is the preview's
            # fingerprint: if the library changed since, nothing runs and they preview again.
            res = orgmod.apply_plan(lib, skip=set(skip or []),
                                    allow=(list(allow) if allow is not None else None),
                                    expect=expect)
            if res.get("stale"):
                return self._json({"error": "The library changed since that preview — "
                                            "run the preview again.",
                                   "stale": True, "plan_id": res["plan_id"]}, 409)
            rebuild_index()                   # re-read the (now-tidy) filesystem
            return self._json({"ok": True, "applied": res})
        # dry run
        actions = orgmod.build_plan(lib)
        counts = {"folder": 0, "file": 0, "junk": 0, "duplicate": 0}
        for a in actions:
            counts[a["type"]] = counts.get(a["type"], 0) + 1
        n = len(lib)

        def rel(p):
            return p[n:].lstrip("/") if p.startswith(lib) else p

        out = [{"id": orgmod.action_id(a, lib), "type": a["type"], "code": a.get("code"),
                "from": rel(a["from"]), "to": rel(a["to"])}
               for a in actions[:self.ORG_CAP]]
        return self._json({"ok": True, "total": len(actions), "counts": counts,
                           "actions": out, "truncated": len(actions) > self.ORG_CAP,
                           "shown": len(out),
                           "plan_id": orgmod.plan_fingerprint(actions, lib)})

    def _apply_org_enrich(self, enrich):
        """Apply the modal's per-code manual actress + tags. {code: {actress, tags[]}}."""
        if not enrich or not os.path.exists(DB_PATH):
            return
        con = sqlite3.connect(DB_PATH)
        con.execute("PRAGMA busy_timeout=8000")
        try:
            for code, info in enrich.items():
                if not code:
                    continue
                name = (info.get("actress") or "").strip()
                tags = info.get("tags") or []
                if isinstance(tags, str):
                    tags = re.split(r"[,;]", tags)
                tags = [t.strip() for t in tags if t and t.strip()]
                if not name and not tags:
                    continue
                con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                            "VALUES(?,1,'available','manual')", (code,))
                if name:
                    aid = _find_actress(con, name)
                    if aid is None:                    # explicit human input -> may create
                        con.execute("INSERT OR IGNORE INTO actresses(name_en,source) "
                                    "VALUES(?, 'manual')", (name,))
                        row = con.execute("SELECT id FROM actresses WHERE name_en=? "
                                          "COLLATE NOCASE", (name,)).fetchone()
                        aid = row[0] if row else None
                    if aid is not None:
                        con.execute("UPDATE works SET actress_id=COALESCE(actress_id,?), "
                                    "actress_src='manual' WHERE code=?", (aid, code))
                tags = [_tag_display(t) for t in tags]     # honor merges/aliases
                for t in tags:
                    con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, t))
                if tags:                                   # reflect into every item w/ this code
                    for it in STATE["items"]:
                        if it.get("code") != code:
                            continue
                        seen = {x.lower() for x in (it.get("tags") or [])}
                        it["tags"] = (it.get("tags") or []) + [t for t in tags
                                                               if t.lower() not in seen]
            con.commit()
        finally:
            con.close()
        _rebuild_tag_facet()

    def _scan_candidates(self, scope):
        """Codes eligible for a metadata scan under `scope`, newest-added first."""
        items = [it for it in STATE["items"] if not it.get("missing")]
        if scope == "all":
            cands = items
        else:                                                   # "missing": no English title yet
            cands = [it for it in items if not it.get("title_en")]
        cands = sorted(cands, key=lambda it: -(it.get("added") or it.get("mtime") or 0))
        out = [{"code": it["code"], "title_en": it.get("title_en") or "",
                "title": it.get("title") or "",
                "actress": it.get("actress_en") or it.get("actress") or "",
                # what the fc2ppv-db pass actually keys on. The dialog used to count rows
                # with an empty actress name, which is a different set entirely: a
                # folder-named movie with no stamp IS fetched, a nameless one with a stamp
                # is NOT, and the checkbox promised the opposite of both.
                "fc2db_read": bool(it.get("fc2db_at")),
                "fc2dbnet_read": bool(it.get("fc2dbnet_at")),
                "cover": bool(it.get("cover_url"))}
               for it in cands[:self.ORG_CAP]]
        return out, len(cands), len(items)

    @staticmethod
    def _codes_needing(con, codes, column):
        """Of `codes`, the ones whose `column` stamp is still NULL. Each source keeps its own
        stamp: fc2ppv-db's "nobody listed" must never stop fc2db.net from being asked."""
        assert column in ("fc2db_at", "fc2dbnet_at")
        done = set()
        for i in range(0, len(codes), 400):
            chunk = codes[i:i + 400]
            qs = ",".join("?" * len(chunk))
            try:
                done |= {r[0] for r in con.execute(
                    "SELECT code FROM works WHERE %s IS NOT NULL AND code IN (%s)"
                    % (column, qs), chunk)}
            except sqlite3.OperationalError:          # pre-migration catalog: nothing read
                pass
        return [c for c in codes if c not in done]

    @staticmethod
    def _codes_needing_fc2db(con, codes):
        """Of `codes`, the ones whose fc2ppv-db page has never been read.

        This used to be "has no performer", which quietly made fc2ppv-db the LAST resort: a
        movie whose actress came from its folder name was never offered to it, so it never
        got that page's co-stars, tags, cover or date either. Asking the DB rather than STATE
        keeps it right even when the index was built before the titles pass."""
        done = set()
        for i in range(0, len(codes), 400):          # SQLite caps variables per statement
            chunk = codes[i:i + 400]
            qs = ",".join("?" * len(chunk))
            try:
                done |= {r[0] for r in con.execute(
                    "SELECT code FROM works WHERE fc2db_at IS NOT NULL AND code IN (%s)" % qs,
                    chunk)}
            except sqlite3.OperationalError:          # pre-migration catalog
                done |= {r[0] for r in con.execute(
                    "SELECT code FROM works WHERE actress_id IS NOT NULL AND code IN (%s)" % qs,
                    chunk)}
        return [c for c in codes if c not in done]

    def _wishlist_covers(self):
        """Backfill titles/covers for all wishlist (missing) works from javfc2.xyz.
        Reuses scanmod.PROGRESS so the scan-progress UI reflects it; no login needed."""
        if not scanmod or not hasattr(scanmod, "backfill_wishlist"):
            return self._json({"error": "backfill unavailable"}, 500)
        if not claim_scan("starting…"):
            return self._json({"error": "a scan is already running",
                               "progress": dict(scanmod.PROGRESS)}, 409)

        def run():
            P = scanmod.PROGRESS
            P.update(running=True, done=0, total=0, hits=0, code="(finding wishlist…)")
            try:
                con = sqlite3.connect(DB_PATH)
                con.execute("PRAGMA busy_timeout=8000")
                n = scanmod.backfill_wishlist(
                    con, sleep=0.4,
                    progress=lambda i, t, c: P.update(done=i, total=t, code=c or ""))
                con.close()
                P.update(hits=n)
            except Exception as e:
                P.update(code="error: %s" % e)
            finally:
                P.update(running=False)
                try:
                    rebuild_index()                 # reflect fetched covers/titles
                except Exception:
                    pass
        def guarded():
            try:
                run()
            finally:
                release_scan()      # a worker that dies early must not wedge the slot
        threading.Thread(target=guarded, daemon=True).start()
        return self._json({"ok": True, "started": True})

    def _scan_library(self, body):
        if not scanmod:
            return self._json({"error": "scanner unavailable"}, 500)
        scope = (body.get("scope") if isinstance(body, dict) else body) or "missing"

        # PREVIEW: re-index (discovers freshly-added folders), then list the candidates.
        if isinstance(body, dict) and body.get("preview"):
            if scanmod.PROGRESS.get("running"):
                return self._json({"error": "a scan is already running"}, 409)
            try:
                rebuild_index()
            except Exception:
                pass
            cands, ncands, ntotal = self._scan_candidates(scope)
            _, nmissing, _ = self._scan_candidates("missing")
            ready, why = self._fc2db_ready()
            return self._json({"ok": True, "scope": scope, "candidates": cands,
                               "truncated": ncands > self.ORG_CAP, "ncands": ncands,
                               "missing": nmissing, "total": ntotal,
                               "fc2db_ready": ready, "fc2db_why": why,
                               "fc2dbnet_ready": "fc2db.net" in enabled_sources()})

        if not claim_scan("(indexing files…)"):
            return self._json({"error": "a scan is already running",
                               "progress": dict(scanmod.PROGRESS)}, 409)
        codes = body.get("codes") if isinstance(body, dict) else None
        if isinstance(codes, str):      # a string here used to be walked character by
            release_scan()              # character, one catalog row per letter
            return self._json({"error": "codes must be a list"}, 400)
        if codes is not None:
            codes = [str(c).strip() for c in codes if str(c).strip()]
        # The toolbar scan fetches titles/covers/dates (123av + ffjav). fc2ppv-db is what
        # supplies the ACTRESS, so it belongs in the same button -- opt out with fc2db:false.
        want_fc2db = True if not isinstance(body, dict) else bool(body.get("fc2db", True))
        # probe fresh, not from the 30 s cache: the modal may have been sitting open while
        # the resolver came up (or went down), and a stale "no" skipped the pass in silence
        fc2db_ready, fc2db_why = (self._fc2db_ready(fresh=True) if want_fc2db
                                  else (False, "not requested"))
        # third pass: fc2db.net, a gap-filler with its own stamp. Plain urllib, so it only
        # needs its Settings box ticked — the resolver is a fallback, not a requirement.
        want_net = True if not isinstance(body, dict) else bool(body.get("fc2dbnet", True))
        net_ready = want_net and "fc2db.net" in enabled_sources()
        if codes is None:                                       # scope-based (legacy path)
            cl = [c["code"] for c in self._scan_candidates(scope)[0]]
        else:                                                   # explicit selection from modal
            cl = list(codes)
        self._start_pipeline(titles=cl, pool=cl, fc2db_ready=fc2db_ready, net_ready=net_ready,
                             note="" if fc2db_ready else "fc2ppv-db pass skipped — " + fc2db_why)
        return self._json({"ok": True, "started": True, "scope": scope,
                           "count": len(codes) if codes is not None else len(cl),
                           "fc2db": fc2db_ready, "fc2db_why": fc2db_why,
                           "fc2dbnet": net_ready})

    def _set_censor(self, code, uncensored):
        it = STATE["by_code"].get(code or "")
        if not it:
            return self._json({"error": "no code"}, 404)
        val = "uncensored" if uncensored else "censored"
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                    "VALUES(?,1,'available','manual')", (code,))
        _ensure_works_schema(con)
        con.execute("UPDATE works SET censorship=?, censorship_src='manual' WHERE code=?",
                    (val, code))
        con.commit()
        con.close()
        for x in STATE["items"]:
            if x["code"] == code:
                x["censorship"] = val
        return self._json({"ok": True, "uncensored": uncensored})

    def _bulk_actress(self, codes, name_en, name_jp):
        """Assign ONE actress to many movies at once (actress only — no per-movie fields).
        Reuses _find_actress so a name in any word order links to the existing record."""
        codes = [c for c in codes if c]
        en = (name_en or "").strip()
        jp = (name_jp or "").strip()
        if not codes:
            return self._json({"error": "no movies selected"}, 400)
        if not en and not jp:
            return self._json({"error": "enter an actress name"}, 400)
        if not os.path.exists(DB_PATH):
            return self._json({"error": "no catalog db"}, 500)
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=8000")
        try:
            aid = _find_actress(con, en) or (_find_actress(con, jp) if jp else None)
            if not aid:
                aid = con.execute(
                    "INSERT INTO actresses(name_en,name_jp,source) VALUES(?,?,'manual')",
                    (en or None, jp or None)).lastrowid
            for nm in (en, jp):
                if nm:
                    con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) "
                                "VALUES(?,?)", (aid, nm))
            for code in codes:
                con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                            "VALUES(?,1,'available','manual')", (code,))
                con.execute("UPDATE works SET actress_id=?, actress_src='manual' "
                            "WHERE code=?", (aid, code))
            con.commit()
            r = con.execute("SELECT name_en,name_jp,age,height,bust,cup,waist,hip "
                            "FROM actresses WHERE id=?", (aid,)).fetchone()
        finally:
            con.close()
        en2, jp2 = (r[0] or ""), (r[1] or "")
        disp = en2 or jp2
        cset = set(codes)
        for x in STATE["items"]:
            if x["code"] in cset:
                x["actress_id"] = aid
                x["actress_en"], x["actress_jp"] = en2 or None, jp2 or None
                x["actress"], x["identified"] = disp, bool(disp)
                x["age"], x["height"], x["bust"] = r[2], r[3], r[4]
                x["cup"], x["waist"], x["hip"] = r[5], r[6], r[7]
        return self._json({"ok": True, "actress_id": aid, "count": len(codes),
                           "name_en": en2, "name_jp": jp2, "actress": disp,
                           "age": r[2], "height": r[3], "bust": r[4],
                           "cup": r[5], "waist": r[6], "hip": r[7]})

    def _set_meta(self, body):
        """Edit a movie's title + its actress (name both langs, age, cup, measurements)."""
        code = body.get("code")
        it = STATE["by_code"].get(code or "")
        if not it:
            return self._json({"error": "no code"}, 404)
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")

        # 1) movie titles (English + Japanese)
        if "title_en" in body or "title" in body:
            con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                        "VALUES(?,1,'available','manual')", (code,))
        if "title_en" in body:
            te = (body.get("title_en") or "").strip()
            con.execute("UPDATE works SET title_en=? WHERE code=?", (te, code))
            for x in STATE["items"]:
                if x["code"] == code:
                    x["title_en"] = te
        if "title" in body:
            tj = (body.get("title") or "").strip()
            con.execute("UPDATE works SET title=? WHERE code=?", (tj, code))
            for x in STATE["items"]:
                if x["code"] == code:
                    x["title"] = tj

        # 2) actress name + profile
        en = (body.get("name_en") or "").strip()
        jp = (body.get("name_jp") or "").strip()
        prev_aid = it.get("actress_id")
        aid = prev_aid
        want = en or jp or aid
        affected = []
        if want:
            try:
                created = False
                if not aid:                          # find (any word order/jp/alias) or create
                    aid = _find_actress(con, en) or (_find_actress(con, jp) if jp else None)
                    if not aid:
                        aid = con.execute(
                            "INSERT INTO actresses(name_en,name_jp,source) VALUES(?,?,'manual')",
                            (en or None, jp or None)).lastrowid
                        created = True
                    con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                                "VALUES(?,1,'available','manual')", (code,))
                    con.execute("UPDATE works SET actress_id=?, actress_src='manual' "
                                "WHERE code=?", (aid, code))
                    # linked to an existing record with a different spelling -> keep both
                    for nm in (en, jp):
                        if nm:
                            con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) "
                                        "VALUES(?,?)", (aid, nm))
                # Write the profile fields ONLY when we're editing this same actress, or we
                # just created her. When the edit SWITCHED the movie to a different existing
                # actress, the form's age/cup/measurements are stale (they belong to the old
                # actress) — writing them would wipe/clobber the real record. In that case we
                # skip the writes and just pull her stored values below.
                editing_this = created or aid == prev_aid
                sets, vals = [], []
                if editing_this:
                    if "name_en" in body:
                        sets.append("name_en=?"); vals.append(en or None)
                    if "name_jp" in body:
                        sets.append("name_jp=?"); vals.append(jp or None)
                    for col, cast in (("age", int), ("cup", str), ("height", int),
                                      ("bust", int), ("waist", int), ("hip", int)):
                        if col in body:
                            v = body.get(col)
                            v = None if v in (None, "") else v
                            if v is not None and cast is int:
                                try:
                                    v = int(v)
                                except (ValueError, TypeError):
                                    continue
                            sets.append(f"{col}=?"); vals.append(v)
                if sets and aid:
                    con.execute(f"UPDATE actresses SET {','.join(sets)} WHERE id=?", vals + [aid])
            except sqlite3.IntegrityError:
                con.close()
                return self._json({"error": "that English name already belongs to another actress"}, 409)

            r = con.execute("SELECT name_en,name_jp,age,height,bust,cup,waist,hip FROM actresses "
                            "WHERE id=?", (aid,)).fetchone()
            en2, jp2 = (r[0] or ""), (r[1] or "")
            disp = en2 or jp2
            for x in STATE["items"]:
                if x.get("actress_id") == aid or x["code"] == code:
                    x["actress_id"] = aid
                    x["actress_en"], x["actress_jp"] = en2 or None, jp2 or None
                    x["actress"], x["identified"] = disp, bool(disp)
                    x["age"], x["height"], x["bust"] = r[2], r[3], r[4]
                    x["cup"], x["waist"], x["hip"] = r[5], r[6], r[7]
                    affected.append(x["code"])
        con.commit()
        con.close()
        itx = STATE["by_code"].get(code, {})
        return self._json({"ok": True, "affected": affected, "item": {
            "code": code, "title_en": itx.get("title_en") or "", "title": itx.get("title") or "",
            "actress_id": itx.get("actress_id"),
            "name_en": itx.get("actress_en") or "", "name_jp": itx.get("actress_jp") or "",
            "actress": itx.get("actress") or "", "age": itx.get("age"), "cup": itx.get("cup") or "",
            "height": itx.get("height"), "bust": itx.get("bust"),
            "waist": itx.get("waist"), "hip": itx.get("hip")}})

    def _set_actress(self, code, name_en, name_jp):
        it = STATE["by_code"].get(code or "")
        if not it:
            return self._json({"error": "no code"}, 404)
        en = (name_en or "").strip()
        jp = (name_jp or "").strip()
        con = sqlite3.connect(DB_PATH)
        aid = it.get("actress_id")
        try:
            if aid:                                   # rename the linked actress
                con.execute("UPDATE actresses SET name_en=?, name_jp=? WHERE id=?",
                            (en or None, jp or None, aid))
            else:                                     # find (alias/word-order aware) or create
                aid = _find_actress(con, en) or (_find_actress(con, jp) if jp else None)
                if aid is None:
                    cur = con.execute(
                        "INSERT INTO actresses(name_en,name_jp,source) VALUES(?,?,'manual')",
                        (en or None, jp or None))
                    aid = cur.lastrowid
                con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                            "VALUES(?,1,'available','manual')", (code,))
                con.execute("UPDATE works SET actress_id=?, actress_src='manual' "
                            "WHERE code=?", (aid, code))
            con.commit()
        except sqlite3.IntegrityError:
            con.close()
            return self._json({"error": "that English name already belongs to another actress"}, 409)
        r = con.execute("SELECT name_en,name_jp,age,height,bust,cup,waist,hip FROM actresses "
                        "WHERE id=?", (aid,)).fetchone()
        con.close()
        en2, jp2 = (r[0] or ""), (r[1] or "")
        disp = en2 or jp2
        affected = []
        for x in STATE["items"]:
            if x.get("actress_id") == aid or x["code"] == code:
                x["actress_id"] = aid
                x["actress_en"] = en2 or None
                x["actress_jp"] = jp2 or None
                x["actress"] = disp
                x["identified"] = bool(disp)
                x["age"], x["height"], x["bust"] = r[2], r[3], r[4]
                x["cup"], x["waist"], x["hip"] = r[5], r[6], r[7]
                affected.append(x["code"])
        return self._json({"ok": True, "actress_id": aid, "name_en": en2, "name_jp": jp2,
                           "actress": disp, "affected": affected})

    def _delete_movies(self, codes):
        """Trash each movie's files (recoverable) and remove its catalog rows, in one
        transaction + one re-sync. A wishlist ghost (no files) just loses its rows."""
        refs = [c for c in dict.fromkeys(codes or []) if c]
        if not refs or not os.path.exists(DB_PATH):
            return self._json({"error": "no code / db"}, 400)
        # A ref is a uid (exactly one folder) or a bare code (every folder with that code,
        # which is the old behaviour and still right for the common one-folder case).
        fs, cset = [], set()
        for ref in refs:
            hit = STATE["by_uid"].get(ref)
            if hit is not None and not hit.get("missing"):
                fs.append(hit); cset.add(hit["code"]); continue
            code = STATE["by_code"].get(ref, {}).get("code") or ref
            cset.add(code)
            fs += [it for it in STATE["items"]
                   if it.get("code") == code and not it.get("missing")]
        seen = set()
        fs = [it for it in fs if id(it) not in seen and not seen.add(id(it))]
        # keep a catalog row whose other copy is still on disk
        doomed = {id(it) for it in fs}
        survivors = {it["code"] for it in STATE["items"]
                     if not it.get("missing") and id(it) not in doomed}
        codes = [c for c in cset if c not in survivors]
        trashed, tbytes = [], 0
        if fs:
            targets = []
            for it in fs:
                targets += _delete_targets(it, STATE["library"])
                tbytes += it.get("size") or 0
            if not _TRASH_LOCK.acquire(blocking=False):
                return self._json({"error": "another delete is still running"}, 409)
            try:
                # re-check inside the lock: the delete that held it may have been this
                # same movie, in which case there is nothing left to move
                targets = [t for t in targets if os.path.exists(t)]
                ok, err = _to_trash(targets)
            finally:
                _TRASH_LOCK.release()
            if err:
                return self._json({"error": "Could not move files to Trash: " + err}, 500)
            trashed = ok
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            for c in codes:
                _purge_work_rows(con, c)
            con.commit()
        finally:
            con.close()
        _purge_userdata(codes=codes)
        _resync_after_delete(cset)
        return self._json({"ok": True, "codes": codes, "deleted": len(codes),
                           "had_files": bool(fs), "trashed": len(trashed), "bytes": tbytes})

    def _delete_actress(self, aid):
        """Delete an actress. Movies where she is the SOLE performer are trashed; movies she
        shares with others keep the file and just lose her from the cast (a promoted co-star
        becomes the new lead). Then her record, aliases and portrait go."""
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            aid = 0
        if not aid or not os.path.exists(DB_PATH):
            return self._json({"error": "no id / db"}, 400)
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            row = con.execute("SELECT name_en,name_jp FROM actresses WHERE id=?", (aid,)).fetchone()
            if not row:
                return self._json({"error": "no such actress"}, 404)
            # every code she is attached to, as lead or co-star
            codes = set()
            for (c,) in con.execute("SELECT code FROM work_actresses WHERE actress_id=?", (aid,)):
                codes.add(c)
            for (c,) in con.execute("SELECT code FROM works WHERE actress_id=?", (aid,)):
                codes.add(c)
            sole, shared = [], []       # sole -> delete the movie; shared -> just unlink her
            for c in codes:
                cast = set()
                pr = con.execute("SELECT actress_id FROM works WHERE code=?", (c,)).fetchone()
                if pr and pr[0] is not None:
                    cast.add(pr[0])
                for (oa,) in con.execute("SELECT actress_id FROM work_actresses WHERE code=?", (c,)):
                    cast.add(oa)
                (shared if (cast - {aid}) else sole).append(c)
            # trash the files of the sole-hers movies first (fail before any DB change)
            trashed, tbytes = [], 0
            if sole:
                soleset = set(sole)
                targets = []
                for it in STATE["items"]:
                    if it.get("code") in soleset and not it.get("missing"):
                        targets += _delete_targets(it, STATE["library"])
                        tbytes += it.get("size") or 0
                ok, err = _to_trash(targets)
                if err:
                    return self._json({"error": "Could not move files to Trash: " + err}, 500)
                trashed = ok
            # DB: drop sole movies; unlink her from shared ones (promote a remaining lead)
            for c in sole:
                _purge_work_rows(con, c)
            for c in shared:
                con.execute("DELETE FROM work_actresses WHERE code=? AND actress_id=?", (c, aid))
                pr = con.execute("SELECT actress_id FROM works WHERE code=?", (c,)).fetchone()
                if pr and pr[0] == aid:
                    nxt = con.execute(
                        "SELECT actress_id FROM work_actresses WHERE code=? "
                        "ORDER BY (role='main') DESC, actress_id LIMIT 1", (c,)).fetchone()
                    con.execute("UPDATE works SET actress_id=? WHERE code=?",
                                (nxt[0] if nxt else None, c))
            con.execute("DELETE FROM aliases WHERE actress_id=?", (aid,))
            # links were left behind, and a dead fc2ppvdb link is not inert: the scanner
            # matches a uuid through this table and would hand back a deleted actress id.
            con.execute("DELETE FROM links WHERE actress_id=?", (aid,))
            con.execute("DELETE FROM crawl WHERE actress_id=?", (aid,))
            con.execute("DELETE FROM actresses WHERE id=?", (aid,))
            con.commit()
        finally:
            con.close()
        _purge_userdata(codes=sole, actress_ids=[aid])
        _resync_after_delete(set(sole))
        return self._json({"ok": True, "id": aid,
                           "deleted_movies": len(sole), "unlinked": len(shared),
                           "trashed": len(trashed), "bytes": tbytes})

    def _api_markers(self, q):
        code = (q.get("code") or [""])[0]
        it = self._item_ref(code)
        if not it:
            return self._json({"error": "no code"}, 404)
        parts = [pp.get("name") for pp in _item_parts(it)] or [code]
        if not os.path.exists(DB_PATH):
            return self._json({"markers": [], "parts": parts, "training": bool(STATE.get("training")),
                           "custom_labels": list(STATE.get("custom_labels") or [])})
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _ensure_markers_schema(con)
            if not it.get("missing"):
                _import_scenes_for_item(con, it)      # lazy sync from sidecar
            rows = _markers_for_code(con, code)
        finally:
            con.close()
        return self._json({"markers": rows, "parts": parts, "training": bool(STATE.get("training")),
                           "custom_labels": list(STATE.get("custom_labels") or [])})

    def _marker_edit(self, body):
        """Add, update (move / relabel), confirm or delete one marker of a movie part.

        ``op: "confirm_all"`` confirms every AI marker of the part at once (no ``t`` needed).

        Written to the sidecar (source of truth), then the markers cache is re-synced.
        Existing markers are addressed by their current start ``t`` (nearest within 0.75 s,
        preferring one whose ``label`` matches). Markers are start points: a marker the user
        adds or changes is ``source: "manual"`` with no ``end``/``confidence``, so the analyzer
        never overwrites it. For training, a changed AI marker keeps its original values under
        ``was``, and a deleted AI marker is appended to the sidecar's top-level ``rejected``
        list (extras that readers ignore; the analyzer uses them to not re-suggest it).
        Only allowed in training mode (Settings -> Library); otherwise markers are view-only.
        """
        if not STATE.get("training"):
            return self._json({"error": "Marker editing needs training mode (Settings → Library)"}, 403)
        code = body.get("code")
        op = body.get("op")
        if not code or not os.path.exists(DB_PATH):
            return self._json({"error": "no code / db"}, 400)
        it = self._item_ref(code)
        if not it or it.get("missing"):
            return self._json({"error": "no such movie"}, 404)
        parts = _item_parts(it)
        try:
            part = int(body.get("part") or 0)
        except (TypeError, ValueError):
            part = 0
        if part < 0 or part >= len(parts):
            part = 0
        vp = parts[part].get("path")
        if not vp:
            return self._json({"error": "no file"}, 500)
        sp = _scenes_path(vp)
        data = _read_scenes(sp) or {}
        markers = [m for m in (data.get("markers") or []) if isinstance(m, dict)]

        def _num(key):
            try:
                v = float(body.get(key))
            except (TypeError, ValueError):
                return None
            return v if v == v and v not in (float("inf"), float("-inf")) else None

        def _label(key):
            v = str(body.get(key) or "").strip().lower()
            return v if (v in SCENE_LABELS or v in (STATE.get("custom_labels") or ())) else None

        def _to_manual(m):
            # A human has now vouched for this marker; keep the AI original for training.
            if m.get("source") != "manual" and "was" not in m:
                m["was"] = {k: m[k] for k in ("t", "end", "label", "source", "confidence") if k in m}
            m["source"] = "manual"
            m.pop("end", None)
            m.pop("confidence", None)

        if op == "confirm_all":
            ai = [m for m in markers if m.get("source") in ("ml", "heuristic")]
            if not ai:
                return self._json({"error": "No AI markers to confirm"}, 400)
            for m in ai:
                _to_manual(m)
            t = None
        else:
            t = _num("t")
            if t is None:
                return self._json({"error": "bad t"}, 400)
        dur = data.get("duration")
        if not dur:
            # probe_code measures the movie's primary file; any other part needs its own length,
            # or every marker of that part gets clamped to the primary's duration.
            dur = ((probe_code(code) or {}).get("duration") if vp == it.get("primary")
                   else _probe_duration(vp))

        def _clamp(v):
            v = max(0.0, v)
            return round(min(v, float(dur)) if dur else v, 2)

        def _find(at, label):
            cands = []
            for m in markers:
                try:
                    d = abs(float(m.get("t")) - at)
                except (TypeError, ValueError):
                    continue
                if d <= 0.75:
                    cands.append((m.get("label") != label, d, m))
            return min(cands, key=lambda c: (c[0], c[1]))[2] if cands else None

        if op == "confirm_all":
            pass
        elif op == "add":
            label = _label("label")
            if not label:
                return self._json({"error": "Pick a label from the list"}, 400)
            markers.append({"t": _clamp(t), "label": label, "source": "manual"})
        elif op in ("update", "delete", "confirm"):
            m = _find(t, body.get("label"))
            if m is None:
                return self._json({"error": "Marker not found (reload the movie)"}, 404)
            if op == "delete":
                markers = [x for x in markers if x is not m]
                if m.get("source") != "manual":
                    rejected = data.get("rejected") if isinstance(data.get("rejected"), list) else []
                    rejected.append(dict(m, rejected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                    data["rejected"] = rejected
            elif op == "confirm":
                _to_manual(m)
            else:
                new_t = _num("new_t")
                new_label = None
                if body.get("new_label") not in (None, ""):
                    new_label = _label("new_label")
                    if not new_label:
                        return self._json({"error": "Pick a label from the list"}, 400)
                if new_t is None and new_label is None:
                    return self._json({"error": "nothing to change"}, 400)
                _to_manual(m)
                if new_t is not None:
                    m["t"] = _clamp(new_t)
                if new_label:
                    m["label"] = new_label
        else:
            return self._json({"error": "bad op"}, 400)
        try:
            _write_scenes(sp, os.path.basename(vp), dur, markers, existing=data)
        except Exception as e:
            return self._json({"error": "Could not write sidecar: " + str(e)}, 500)
        STATE["data_json"] = None      # marker flags shown in the sidebar changed
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _ensure_markers_schema(con)
            con.execute("DELETE FROM marker_src WHERE code=? AND part=?", (code, part))  # force resync
            _import_scenes_for_item(con, it)
            rows = _markers_for_code(con, code)
        finally:
            con.close()
        return self._json({"ok": True, "markers": rows})

    def _import_markers_all(self):
        if not os.path.exists(DB_PATH):
            return self._json({"error": "no db"}, 500)
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _ensure_markers_schema(con)
            for it in STATE["items"]:
                if not it.get("missing"):
                    _import_scenes_for_item(con, it)
            n = con.execute("SELECT COUNT(*) FROM markers").fetchone()[0]
            withm = con.execute("SELECT COUNT(DISTINCT code) FROM markers").fetchone()[0]
        finally:
            con.close()
        return self._json({"ok": True, "markers": n, "movies": withm})

    def _open_external(self, code, reveal):
        # This launches an application on the Mac. The endpoint list said localhost; the
        # handler never checked. Loopback and the LAN are allowed (the phone uses this);
        # anything routed in from outside is not.
        peer = (self.client_address or ("",))[0]
        try:
            ip = ipaddress.ip_address(peer)
            local = ip.is_loopback or ip.is_private or ip.is_link_local
        except ValueError:
            local = False
        if not local:
            return self._json({"error": "open/reveal is only available on this network"}, 403)
        it = self._item_ref(code)
        if not it:
            return self._json({"error": "no code"}, 404)
        path = it["primary"]
        try:
            if reveal:
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["open", path])
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 500)


CONFIG_PATH = os.path.join(DATA_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_config(library=None, port=None, download=None, training_mode=None, custom_labels=None,
                flaresolverr=None, fc2db_mode=None):
    cfg = load_config()
    if custom_labels is not None:
        cfg["custom_labels"] = list(custom_labels)
    if training_mode is not None:
        cfg["training_mode"] = bool(training_mode)
    if library is not None:
        cfg["library"] = library
    if port is not None:
        cfg["port"] = port
    if download is not None:
        cfg["download"] = download
    if flaresolverr is not None:
        cfg["flaresolverr"] = flaresolverr
    if fc2db_mode is not None:
        cfg["fc2db_mode"] = fc2db_mode
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception:
        return False


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Vault — a local visual browser for a movie library")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--library", default=None, help="path to the folder that holds your movies")
    ap.add_argument("--install-service", action="store_true",
                    help="start Vault at login and keep it running (launchd / systemd --user / Task Scheduler)")
    ap.add_argument("--uninstall-service", action="store_true", help="stop the login service and remove it")
    ap.add_argument("--service-status", action="store_true")
    args = ap.parse_args()
    if args.uninstall_service or args.service_status:
        try:
            print(vault_service.uninstall() if args.uninstall_service else vault_service.status())
        except RuntimeError as e:
            sys.exit("Vault: %s" % e)
        return
    # library/port precedence: CLI > env > config.json > built-in
    library = (args.library or os.environ.get("VAULT_LIBRARY")
               or cfg.get("library") or DEFAULT_LIBRARY)
    port = (args.port or (int(os.environ["VAULT_PORT"]) if os.environ.get("VAULT_PORT") else None)
            or cfg.get("port") or 8730)
    if args.install_service:
        if not library or not os.path.isdir(library):
            sys.exit("Vault: tell the service where the movies are first:\n"
                     "  python3 serve.py --install-service --library \"/path/to/your/movies\"")
        save_config(library=library, port=port)
        env = {k: os.environ[k] for k in ("VAULT_DATA",) if os.environ.get(k)}
        try:
            print(vault_service.install(os.path.abspath(__file__), port, library,
                                        os.path.join(DATA_DIR, "server.log"), env))
        except RuntimeError as e:
            sys.exit("Vault: %s" % e)
        print("Open http://127.0.0.1:%d/ once it answers." % port)
        return
    if not library or not os.path.isdir(library):
        sys.stderr.write(
            "\nVault: no valid library folder configured.\n"
            "Point it at your movies with any of:\n"
            "  python3 serve.py --library \"/path/to/your/movies\"\n"
            "  VAULT_LIBRARY=\"/path/to/your/movies\" python3 serve.py\n"
            f"  or set \"library\" in {CONFIG_PATH}\n\n")
        sys.exit(1)
    # remember the choice for next launch (merge — don't clobber 'download' etc.)
    save_config(library=library, port=port)
    STATE["library"] = library
    STATE["training"] = bool(cfg.get("training_mode", False))   # scene-marker editing; off by default
    STATE["custom_labels"] = sorted({l for l in (cfg.get("custom_labels") or [])
                                     if isinstance(l, str) and _SCENE_LABEL_RE.match(l) and l not in SCENE_LABELS})
    STATE["download"] = cfg.get("download", "")
    load_probe()
    if os.path.exists(DB_PATH):                 # one-time, idempotent schema top-up
        try:
            _c = sqlite3.connect(DB_PATH, timeout=30)
            _ensure_works_schema(_c)
            _c.commit()
            _c.close()
        except Exception:
            pass
    atexit.register(save_probe)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (save_probe(), sys.exit(0)))
    if os.path.exists(DB_PATH):                 # one-time: fold JP tags into English canon
        try:
            _con = sqlite3.connect(DB_PATH)
            _ensure_tagtx(_con)
            n = _canonicalize_tags(_con)
            _con.close()
            if n:
                sys.stderr.write(f"[boot] canonicalized {n} tag(s) JP->EN\n")
        except Exception:
            pass
    cached = _load_fs_index(library)
    if cached:
        sys.stderr.write(f"[boot] loading cached index for {library} …\n")
        rebuild_index(fs_items=cached)                       # instant
        # refresh from the real filesystem in the background (picks up changes)
        threading.Thread(target=rebuild_index, daemon=True).start()
    else:
        sys.stderr.write(f"[boot] first scan of {library} …\n")
        rebuild_index()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    sys.stderr.write(f"[boot] Vault {VAULT_VERSION} — http://127.0.0.1:{port}/  "
                     f"({len(STATE['items'])} movies)\n")
    sys.stderr.write(f"[boot] listening on 0.0.0.0 — reachable from LAN/VPN, "
                     f"no auth\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        save_probe()


if __name__ == "__main__":
    main()
