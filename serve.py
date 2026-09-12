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
import gzip
import json
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
# Writable data dir. Dev / portable-folder use keeps everything next to serve.py (HERE).
# A macOS .app (read-only in /Applications) sets VAULT_DATA to a user folder
# (~/Library/Application Support/Vault) so cache/config/DB land somewhere writable.
DATA_DIR = os.environ.get("VAULT_DATA") or HERE
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
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

# the scraper registry shown in Settings. The 'builtin' ones are wired into scan.py; custom
# entries the user adds are stored and displayed (and tried by the generic title fetch).
DEFAULT_SOURCES = [
    {"name": "123av", "url": "https://123av.com/en/v/fc2-ppv-{num}", "provides": "English title",
     "builtin": True, "enabled": True},
    {"name": "ffjav", "url": "https://ffjav.com/?s={code}", "provides": "JP title, cover, date",
     "builtin": True, "enabled": True},
    {"name": "javdatabase", "url": "https://www.javdatabase.com/idols/{slug}/",
     "provides": "actress body info + JP name", "builtin": True, "enabled": True},
    {"name": "jav.guru", "url": "https://jav.guru/actress/{slug}/",
     "provides": "actress filmography (verification + wishlist)", "builtin": True, "enabled": True},
]


def _load_sources():
    try:
        with open(SOURCES_CFG) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
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
    "by_code": {},        # code -> item
    "actresses": [],      # list of actress dicts (with portrait flag)
    "facets": {},
    "scanned_at": 0,
}
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


# dirs the by-actress folder pass must never move into/out of
FOLD_RESERVED = {"_vault_trash", "_duplicates", "_unsorted", "_vault", "@eadir"}


def _safe_actress_dir(name):
    """A filesystem-safe single path segment for an actress folder."""
    s = (name or "").strip().replace("/", "-").replace(":", "-").replace("\\", "-")
    return s.strip(" .")


def scan_library(library):
    """Walk the tree; every directory that directly holds video(s) is one item."""
    items = []
    if not os.path.isdir(library):
        return items
    lib_real = os.path.realpath(library)
    for dirpath, dirs, files in os.walk(library):
        dirs.sort()
        vid = _largest_video(files, dirpath)
        if not vid:
            continue
        folder = os.path.basename(dirpath.rstrip("/"))
        fname = os.path.splitext(os.path.basename(vid["primary"]))[0]
        # canonical FC2 code from the folder OR the filename; else keep the folder name
        code = norm_code(folder) or norm_code(fname) or folder
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
    for it in items:
        by_code.setdefault(it["code"], it)
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
            for r in con.execute("SELECT code,actress_id FROM work_actresses"):
                wa.setdefault(r["code"], []).append(r["actress_id"])
        except sqlite3.OperationalError:
            pass
        # works -> enrich
        # match by exact code, then by digits
        by_digits = {}
        for it in items:
            if it["digits"]:
                by_digits.setdefault(it["digits"], []).append(it)
        now_owned = []          # works whose file is on disk but the row still says missing
        for w in con.execute(
            "SELECT code,actress_id,release_date,title,title_en,duration,"
            "producer,fav_count,availability,cover_url,censorship FROM works"
        ):
            targets = []
            if w["code"] in by_code:
                targets = [by_code[w["code"]]]
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
        # full cast (multi-actress), primary first
        cast = wa.get(it["code"])
        if cast:
            ordered = ([aid] if aid in cast else []) + [x for x in cast if x != aid]
            it["actresses"] = [{"id": x, "disp": disp_by_id.get(x) or ""} for x in ordered]
        elif aid:
            it["actresses"] = [{"id": aid, "disp": name}]
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
    return by_code, alist, facets


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
    if fs_items is None:
        items = scan_library(lib)          # the slow, SMB-I/O-bound part
        _save_fs_index(lib, items)         # cache raw scan BEFORE enrich mutates it
        src = "scan"
    else:
        items = fs_items
        src = "cache"
    _stamp_added(items)                      # idempotent 'date added' (also upgrades old caches)
    by_code, alist, facets = enrich(items)  # fast: DB overlay + ghosts (mutates items)
    STATE["items"] = items
    STATE["by_code"] = by_code
    STATE["actresses"] = alist
    STATE["facets"] = facets
    STATE["scanned_at"] = int(time.time())
    STATE["data_json"] = None                 # invalidate the /api/data serialization cache
    sys.stderr.write(f"[{src}] {facets['total']} items in {time.time()-t0:.1f}s "
                     f"({facets['identified']} identified, "
                     f"{facets['total_bytes']/1e12:.2f} TB)\n")


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
                    "INSERT INTO works(code,actress_id,source,in_library,availability) "
                    "VALUES(?,?, 'folder', 1, 'owned') "
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
def probe_code(code):
    with PROBE_LOCK:
        if code in PROBE:
            return PROBE[code]
    it = STATE["by_code"].get(code)
    if not it:
        return None
    path = it["primary"]
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
    with PROBE_LOCK:
        PROBE[code] = meta
        _probe_save_counter[0] += 1
        due = _probe_save_counter[0] % 25 == 0
    if due:
        save_probe()
    return meta


_probe_save_counter = [0]


def probe_and_persist(code):
    return probe_code(code)


# ------------------------------------------------------------------- thumbs
def thumb_path(code):
    return os.path.join(THUMB_DIR, safe_key(code) + ".jpg")


def generate_thumb(code):
    it = STATE["by_code"].get(code)
    if not it:
        return None
    out = thumb_path(code)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    path = it["primary"]
    meta = probe_code(code)
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

    def do_GET(self):
        u = urlparse(self.path)
        p = unquote(u.path)
        q = parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._serve_file(os.path.join(HERE, "index.html"),
                                        "text/html; charset=utf-8")
            if p == "/api/data":
                return self._api_data()
            if p == "/api/meta":
                return self._api_meta(q)
            if p == "/api/rescan":
                rebuild_index()
                return self._json({"ok": True, "count": len(STATE["items"])})
            if p == "/api/settings":
                return self._api_settings()
            if p == "/api/scanprogress":
                pr = dict(scanmod.PROGRESS) if scanmod else {"running": False}
                return self._json(pr)
            if p == "/api/importprogress":
                return self._json(dict(IMPORT_PROGRESS))
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
        u = urlparse(self.path)
        p = u.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        STATE["data_json"] = None   # any POST may mutate state -> invalidate the /api/data cache
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
        if p == "/api/scan":
            return self._scan_one(body.get("code"), bool(body.get("force")))
        if p == "/api/scanlib":
            return self._scan_library(body)
        if p == "/api/wishlistcovers":
            return self._wishlist_covers()
        if p == "/api/foldactress":
            return self._fold_actress(bool(body.get("apply")), body.get("skip"))
        if p == "/api/organize":
            return self._organize(bool(body.get("apply")), body.get("skip"),
                                  body.get("enrich"))
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
        if p == "/api/import":
            if body.get("preview"):
                return self._import_preview()
            return self._import_run(bool(body.get("confirm_lib_deletes")))
        if p == "/api/clearcache":
            return self._clear_cache(body.get("targets") or [])
        if p == "/api/sourcesave":
            return self._sources_save(body.get("sources"))
        if p == "/api/fc2db/save":
            return self._fc2db_save(body.get("cookie"), body.get("ua"))
        if p == "/api/fc2db/test":
            return self._fc2db_test(body.get("code"))
        if p == "/api/fc2db/scan":
            return self._fc2db_scan(body.get("scope") or "missing")
        if p == "/api/stash":
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", (self.headers.get("X-Stash") or "stash")) + ".json"
            with open(os.path.join(CACHE, name), "wb") as f:
                f.write(raw)
            return self._json({"ok": True, "bytes": len(raw), "file": name},
                              extra={"Access-Control-Allow-Origin": "*"})
        self._json({"error": "unknown"}, 404)

    def do_OPTIONS(self):
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
        # (do_POST and rebuild_index clear STATE["data_json"])
        body = STATE.get("data_json")
        if body is not None:
            return self._send(200, "application/json; charset=utf-8", body,
                              {"Cache-Control": "no-store"})
        # ship a compact projection (omit absolute paths)
        out_items = []
        for it in STATE["items"]:
            out_items.append({
                "code": it["code"],
                "actress": it.get("actress") or "",
                "actress_id": it.get("actress_id"),
                "actress_en": it.get("actress_en") or "",
                "actress_jp": it.get("actress_jp") or "",
                "actresses": it.get("actresses") or [],
                "aka": it.get("aka") or "",
                "identified": it["identified"],
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
            })
        body = json.dumps({
            "items": out_items,
            "actresses": STATE["actresses"],
            "facets": STATE["facets"],
            "scanned_at": STATE["scanned_at"],
            "library": STATE["library"],
        }).encode("utf-8")
        STATE["data_json"] = body
        self._send(200, "application/json; charset=utf-8", body,
                   {"Cache-Control": "no-store"})

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
        if code not in STATE["by_code"]:
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

    def _video(self, name, part=None):
        code = name.strip("/")
        it = STATE["by_code"].get(code)
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
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            changed = scanmod.scan_code(con, code, force=force)
        finally:
            con.close()
        self._apply_work_fields(code, changed)
        it = STATE["by_code"].get(code, {})
        return self._json({"ok": True, "changed": changed, "item": {
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
                           "tags": tags, "actresses": actresses,
                           "sources": _load_sources(), "cache": cache,
                           "fc2db": {"connected": bool(secret_get("cookie")),
                                     "encrypted": _kc_available()},
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
        if IMPORT_PROGRESS.get("running"):
            return self._json({"error": "an import is already running",
                               "progress": dict(IMPORT_PROGRESS)}, 409)
        dl = STATE.get("download")
        if not dl or not os.path.isdir(dl):
            return self._json({"error": "no download folder set"}, 400)
        rn, mg = self._script("rename_fc2.py"), self._script("migrate_fc2.py")
        if not rn or not mg:
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

    # ------------------------------------------------- fc2ppv-db (session-gated)
    def _fc2db_save(self, cookie, ua):
        cookie = (cookie or "").strip()
        ua = (ua or "").strip()
        ok = secret_set("cookie", cookie) and secret_set("ua", ua)
        return self._json({"ok": ok, "connected": bool(cookie),
                           "encrypted": _kc_available()})

    def _fc2db_test(self, code):
        if not scanmod or not hasattr(scanmod, "fetch_fc2ppvdb"):
            return self._json({"error": "scanner unavailable"}, 500)
        cookie, ua = secret_get("cookie"), secret_get("ua")
        if not cookie:
            return self._json({"error": "no session saved — paste your cookie first"}, 400)
        # a code known to exist on the site, or one of yours
        code = (code or "").strip() or "FC2-PPV-4964424"
        data = scanmod.fetch_fc2ppvdb(code, cookie=cookie, ua=ua)
        ok = bool(data.get("title_jp") or data.get("actresses"))
        return self._json({"ok": ok, "code": code, "data": data or None,
                           "note": ("session works — the server can read fc2ppv-db"
                                    if ok else
                                    "no data — the session was rejected (Cloudflare). "
                                    "The cookie has likely expired — re-copy a fresh one "
                                    "from the same browser. If it still fails, your browser "
                                    "may be on a different IP than the server (e.g. a VPN).")})

    def _fc2db_scan(self, scope):
        if not scanmod or not hasattr(scanmod, "scan_fc2db"):
            return self._json({"error": "scanner unavailable"}, 500)
        if scanmod.PROGRESS.get("running"):
            return self._json({"error": "a scan is already running"}, 409)
        cookie, ua = secret_get("cookie"), secret_get("ua")
        if not cookie:
            return self._json({"error": "connect fc2ppv-db first"}, 400)
        items = [it for it in STATE["items"] if not it.get("missing")]
        if scope == "missing":
            # fc2ppv-db's job is the ACTRESS — so "missing" = no actress yet (even if a title
            # was already filled by the shell scan). Re-run it with fresh sessions and it
            # progressively completes the library, skipping ones already identified.
            codes = [it["code"] for it in items if not it.get("actress_id")]
        else:
            codes = [it["code"] for it in items]

        def run():
            con = sqlite3.connect(DB_PATH, timeout=60)
            con.execute("PRAGMA busy_timeout=60000")
            try:
                scanmod.scan_fc2db(con, codes, cookie, ua, THUMB_DIR)
            finally:
                con.close()
            try:
                rebuild_index(_load_fs_index(STATE["library"]))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "started": True, "count": len(codes)})

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
                                           library_codes=lib_codes)
        finally:
            con.close()
        if report.get("applied"):
            # re-overlay the (updated) DB onto a fresh copy of the raw filesystem scan
            rebuild_index(_load_fs_index(STATE["library"]))
        return self._json({"ok": True, "report": report})

    ORG_CAP = 4000                             # max actions shipped for per-row selection

    def _fold_actress(self, apply, skip=None):
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

    def _organize(self, apply, skip=None, enrich=None):
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
            res = orgmod.apply_plan(lib, skip=set(skip or []))
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
                           "actions": out, "truncated": len(actions) > self.ORG_CAP})

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
                            "VALUES(?,1,'owned','manual')", (code,))
                if name:
                    aid = _find_actress(con, name)
                    if aid is None:                    # explicit human input -> may create
                        con.execute("INSERT OR IGNORE INTO actresses(name_en,source) "
                                    "VALUES(?, 'manual')", (name,))
                        row = con.execute("SELECT id FROM actresses WHERE name_en=? "
                                          "COLLATE NOCASE", (name,)).fetchone()
                        aid = row[0] if row else None
                    if aid is not None:
                        con.execute("UPDATE works SET actress_id=COALESCE(actress_id,?) "
                                    "WHERE code=?", (aid, code))
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
                "cover": bool(it.get("cover_url"))}
               for it in cands[:self.ORG_CAP]]
        return out, len(cands), len(items)

    def _wishlist_covers(self):
        """Backfill titles/covers for all wishlist (missing) works from javfc2.xyz.
        Reuses scanmod.PROGRESS so the scan-progress UI reflects it; no login needed."""
        if not scanmod or not hasattr(scanmod, "backfill_wishlist"):
            return self._json({"error": "backfill unavailable"}, 500)
        if scanmod.PROGRESS.get("running"):
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
        threading.Thread(target=run, daemon=True).start()
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
            return self._json({"ok": True, "scope": scope, "candidates": cands,
                               "truncated": ncands > self.ORG_CAP,
                               "missing": nmissing, "total": ntotal})

        if scanmod.PROGRESS.get("running"):
            return self._json({"error": "a scan is already running",
                               "progress": dict(scanmod.PROGRESS)}, 409)
        codes = body.get("codes") if isinstance(body, dict) else None

        def run():
            if codes is None:                                   # scope-based (legacy path)
                scanmod.PROGRESS.update(running=True, done=0, total=0, hits=0,
                                        code="(indexing files…)")
                try:
                    rebuild_index()
                except Exception:
                    pass
                cl, _, _ = self._scan_candidates(scope)
                cl = [c["code"] for c in cl]
            else:                                               # explicit selection from modal
                cl = list(codes)
            scanmod.scan_library(cl)                            # manages PROGRESS itself
            try:
                rebuild_index()                                 # reflect fetched metadata
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()
        return self._json({"ok": True, "started": True, "scope": scope,
                           "count": len(codes) if codes is not None else None})

    def _set_censor(self, code, uncensored):
        it = STATE["by_code"].get(code or "")
        if not it:
            return self._json({"error": "no code"}, 404)
        val = "uncensored" if uncensored else "censored"
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("INSERT OR IGNORE INTO works(code,in_library,availability,source) "
                    "VALUES(?,1,'available','manual')", (code,))
        con.execute("UPDATE works SET censorship=? WHERE code=?", (val, code))
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
                con.execute("UPDATE works SET actress_id=? WHERE code=?", (aid, code))
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
                    con.execute("UPDATE works SET actress_id=? WHERE code=?", (aid, code))
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
                con.execute("UPDATE works SET actress_id=? WHERE code=?", (aid, code))
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

    def _open_external(self, code, reveal):
        it = STATE["by_code"].get(code or "")
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


def save_config(library=None, port=None, download=None):
    cfg = load_config()
    if library is not None:
        cfg["library"] = library
    if port is not None:
        cfg["port"] = port
    if download is not None:
        cfg["download"] = download
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Vault — a local visual browser for a movie library")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--library", default=None, help="path to the folder that holds your movies")
    args = ap.parse_args()
    # library/port precedence: CLI > env > config.json > built-in
    library = (args.library or os.environ.get("VAULT_LIBRARY")
               or cfg.get("library") or DEFAULT_LIBRARY)
    port = (args.port or (int(os.environ["VAULT_PORT"]) if os.environ.get("VAULT_PORT") else None)
            or cfg.get("port") or 8730)
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
    STATE["download"] = cfg.get("download", "")
    load_probe()
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
    sys.stderr.write(f"[boot] http://127.0.0.1:{port}/  "
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
