"""Standardize tags (Settings → Tags → Standardize tags). Off by default.

The sources tag the same thing many ways — Raw / Bareback, Cuckold / NTR, 3P written three
ways — add shop wording ("Sold Out", "With Bonus"), repeat what Vault already has as a field
(Uncensored, cup sizes, ages) and leave Japanese one-offs no one translated. These rules fold
that into one tag set of roughly 190. They are opt-in, per catalog, because some users want
the tags exactly as the sources give them.

  * Switching on rewrites the tags already in the catalog and remembers every row it changed
    (table tag_rules_undo), so switching off puts them back. The switch is stored in the
    catalog itself (catalog_meta 'tag_rules').
  * While on, every tag a scan or a catalog merge stores goes through canon() first. Tags you
    type yourself on a movie are left as you typed them.
  * Merges: MERGE[target] = the names folded into it. Removed: DROP, and any Japanese tag that
    no translation (Settings → Tags) turns into English. Your own translations and merges run
    first; these rules apply to their result.
  * ALWAYS_DROP (tags that imply a minor) is applied whether the switch is on or not.

Stdlib only."""
import re

META_KEY = "tag_rules"
UNDO = ("CREATE TABLE IF NOT EXISTS tag_rules_undo (code TEXT NOT NULL, tag TEXT NOT NULL, "
        "kind TEXT NOT NULL, PRIMARY KEY (code, tag, kind))")
# Japanese, Korean and full-width forms: a stored tag with these was never translated
_JP = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ가-힯Ａ-Ｚａ-ｚ０-９]")

MERGE = {
    "Adultery": [
        "Immoral", "背徳",
    ],
    "Airhead": [
        "天然ちゃん", "Quirky",
    ],
    "Anal": [
        "Anal Play", "First Anal", "ATM", "AF", "Anal Creampie", "Anal Stretching", "Anal Beads",
        "Buttplug",
    ],
    "Announcer": [
        "Female Announcer", "地方アナウンサー", "女〇アナ",
    ],
    "Athletic": [
        "Athlete", "Muscular", "Sporty", "Sports", "筋トレ", "国体選手", "Swim Club", "元バレー部", "体操部",
    ],
    "Bareback": [
        "Raw", "NN", "ゴム無し", "初めての生", "生膣", "生外生中",
    ],
    "Bath": [
        "浴室", "湯船いちゃこら",
    ],
    "BDSM": [
        "SM", "Soft SM", "ライトSM", "Training", "Bondage", "縛り", "亀甲縛り", "手錠", "手枷", "首輪",
        "Blindfold", "Brutal", "Cock Slapping", "Sadist",
    ],
    "Beautiful Body": [
        "ナイスバディー", "健康的ボディ", "グラマラス", "抱き心地",
    ],
    "Beautiful Legs": [
        "Legs", "Thighs", "美腰",
    ],
    "Beautiful Pussy": [
        "Pink Pussy", "ピンクまんこ", "美マンコ", "Slit", "Labia",
    ],
    "Beautiful Tits": [
        "美胸", "美居乳", "Cleavage", "胸フェチ", "乳",
    ],
    "Beautiful Woman": [
        "S-Class", "S-Class Beauty", "SSS-Class", "S級美人", "S級女", "Classic Beauty", "Charming",
        "Very Charming", "Elegant", "ゴージャス", "美貌", "美形", "綺麗系", "Beautiful Girl", "Top Class",
        "Angel", "イケ女", "日本美女",
    ],
    "Big Clit": [
        "Clitoris", "デカクリトリス", "巨クリ", "超巨大クリトリス",
    ],
    "Big Tits": [
        "Boobs", "Saggy Tits", "水パイ", "天然Jカップ",
    ],
    "Black Hair": [
        "Long Black Hair",
    ],
    "Blowjob": [
        "フェラ音", "フェラ好き", "アナコンダフェラ", "ひょっとこフェラ", "観覧車フェラ", "Ball Licking", "手コキフェラ", "PtoM", "お掃除",
        "お掃除マンコ", "Cleanup Blowjob",
    ],
    "Breeding": [
        "Impregnation", "Fertile Day", "Trying to Conceive", "排卵日中出し", "排卵日", "排卵", "孕みッ子", "托卵",
        "No Pill", "遺伝子", "精子提供",
    ],
    "Car Sex": [
        "In Car",
    ],
    "Celebrity": [
        "Showbiz", "タレント", "芸能のタマゴ",
    ],
    "Cheerful": [
        "元気はつらつ", "ノリノリ", "ニコニコ", "Smile", "微笑み",
    ],
    "Christmas": [
        "クリスマスマーケット",
    ],
    "Chubby": [
        "Plump", "Voluptuous", "エロムチ", "マシュマロ", "プニ", "肉厚", "巨女",
    ],
    "Clothed": [
        "着衣ハメ", "脱衣着衣",
    ],
    "College Girl": [
        "Student", "短大生", "大学デビュー", "専門", "介護専門生",
    ],
    "Condom Removal": [
        "Remove Condom", "Stealthing", "ゴム破れ",
    ],
    "Cosplay": [
        "Anime Cosplay", "Cat Ears", "バニーちゃん", "サンタ", "巫女", "猫耳メイド",
    ],
    "Country Girl": [
        "Rural", "田舎女", "お上りさん",
    ],
    "Cowgirl": [
        "Reverse Cowgirl", "Piston Cowgirl", "スパイダー騎乗位", "馬乗り",
    ],
    "Creampie": [
        "Unauthorized Creampie", "暴発中出し", "無申告中出し", "膣奥射精", "精液流し込み", "生中だし", "中出し懇願",
        "Accidental discharge",
    ],
    "Crying": [
        "Teary", "Sobbing", "涙", "お目目うるうる",
    ],
    "Cum in Mouth": [
        "精液歯磨き", "ザーメン歯磨き", "潮飲まされ",
    ],
    "Cumshot on Body": [
        "Belly Cumshot", "Tits Cumshot", "胸射",
    ],
    "Cute": [
        "Baby Face", "狸顔", "小動物系", "小動物", "子猫", "萌え", "ぶりっこ",
    ],
    "Dancer": [
        "Dance",
    ],
    "Dating App": [
        "Dating Site", "オフパコ", "オフ会",
    ],
    "Debt": [
        "Loan", "Repayment", "借金返済", "金欠",
    ],
    "Debut": [
        "First Time", "First Shoot", "Newcomer", "Debut Work", "完全初撮影", "Training Session",
    ],
    "Dialect": [
        "Kansai", "Kansai Dialect",
    ],
    "Doggy Style": [
        "後背位",
    ],
    "Exposure": [
        "Exposed",
    ],
    "Face Shown": [
        "Reverse", "Face Exposed", "顔晒し", "顔バレ", "顔〇し",
    ],
    "Facial": [
        "Bukkake",
    ],
    "Fair Skin": [
        "Beautiful Skin", "Translucent Skin", "白肌", "白い肌", "色白美",
    ],
    "Fingering": [
        "バック手マン",
    ],
    "Flexible": [
        "Y-Balance", "Y時バランス", "ストレッチ", "バレエ",
    ],
    "Flight Attendant": [
        "CA",
    ],
    "Food Play": [
        "Yam Torture", "Ginger Torture", "Vegetable Play", "山芋かゆかゆ責め", "山芋", "かゆかゆ責め", "フィギング",
        "きゅうり責め",
    ],
    "Footjob": [
        "Foot Licking", "足裏",
    ],
    "Foreign": [
        "Foreigner", "White", "Russian", "Chinese", "Taiwanese", "ラトビア", "欧風ボディ",
    ],
    "Gal": [
        "元ギャル", "トー横ギャル", "Toyoko Kids",
    ],
    "Gravure": [
        "Exclusive Photoshoot", "撮影会", "写真会", "グラビア級", "Race Queen",
    ],
    "Group Sex": [
        "4P", "5P", "５P", "6P", "7P", "Orgy", "Multiple Partners",
    ],
    "Gyno Exam": [
        "Gyno", "婦人科", "産婦人科", "検診", "診察", "分娩台",
    ],
    "Hairy": [
        "Pubic Hair", "Unshaved", "Coarse hair", "With hair", "Has Pubic Hair", "陰毛未処理", "ナチュラルヘア",
        "毛", "ケツ毛",
    ],
    "Handjob": [
        "手コキ抜き", "ベロチュー手コキ",
    ],
    "Has Boyfriend": [
        "Girlfriend", "Ex-Girlfriend",
    ],
    "Highly Educated": [
        "Intellectual", "才色兼備", "知性", "慶應", "名門大学", "エリート", "ガリ勉",
    ],
    "Hostess": [
        "ラウンジ嬢", "ガルバ嬢", "コンカフェ嬢", "コンカフェ",
    ],
    "Hot Spring": [
        "Open-Air Bath", "旅館",
    ],
    "Idol": [
        "Former Idol", "Underground Idol", "Idol Lookalike",
    ],
    "Inexperienced": [
        "経験のない", "エッチの経験少な目",
    ],
    "Influencer": [
        "YouTuber", "Streamer",
    ],
    "Innocent": [
        "Pure", "Naive", "Plain", "Prim and Proper", "Innocent Looking", "純真無垢", "純朴", "純情ピュア",
        "清純ピュア", "清廉", "清潔感", "清楚フェイスなのに乱れる", "可憐", "地味女", "垢抜けない", "芋女",
    ],
    "Internal Filming": [
        "Speculum", "Portio", "Endoscope", "子宮拡張",
    ],
    "Interracial": [
        "Black", "黑人",
    ],
    "Interview": [
        "インタビュー",
    ],
    "Job Hunter": [
        "就活", "リクルートスーツ", "リクルート",
    ],
    "Kiss": [
        "Kissing",
    ],
    "Lingerie": [
        "Sexy Lingerie", "ベビードール", "Garter Belt",
    ],
    "Lotion": [
        "Oil", "Slippery", "Body Wash", "マットプレイ",
    ],
    "Lovey-Dovey": [
        "Close Contact", "甘えん坊", "甘えんぼ", "甘え",
    ],
    "Maid": [
        "Maid Cosplay",
    ],
    "Married Woman": [
        "Young Wife", "新妻", "新婚", "Has Kids", "経産婦", "Sexless", "Divorced", "離婚",
    ],
    "Masochist": [
        "Submissive", "Obedient", "マゾ", "被虐", "言いなり", "メス堕ち", "弄ばれる",
    ],
    "Massage": [
        "Erotic Massage", "猥褻施療",
    ],
    "Masturbation": [
        "電マオナニー", "電マオナ", "立ちオナニー", "強制オナニー", "相互オナニー",
    ],
    "Mature": [
        "milf", "Beautiful Mature", "Mature Beauty", "五十路",
    ],
    "Men's Massage": [
        "Massage Girl",
    ],
    "Missionary": [
        "Mating Press",
    ],
    "Moaning": [
        "Moaning Voice", "エロ声", "オホ声", "ヲホ声", "濁音オホ声", "濁音", "Screaming",
    ],
    "Multiple Rounds": [
        "2 Ejaculations", "3 Ejaculations", "7 Ejaculations", "二連続射精", "二回戦", "射精祭り", "追撃",
    ],
    "Nice Butt": [
        "Butt", "Big Ass", "Ass Fetish", "桃尻", "小尻", "巨ケツ", "安産型デカケツ", "安産型", "ケツ", "尻出し",
    ],
    "Nipples": [
        "敏感乳首",
    ],
    "No Underwear": [
        "No Panties", "No Bra",
    ],
    "NTR": [
        "Cuckold",
    ],
    "Nurse": [
        "看護師の卵すずちゃん", "Dental Assistant", "医療系",
    ],
    "Nympho": [
        "Slut", "Pervert", "ど淫乱", "淫乱女", "淫乱妻", "ド変態", "スケベ", "性欲強め", "Frustrated", "自ら求める",
    ],
    "Ojousama": [
        "Princess Type", "ハイソ",
    ],
    "Orgasm": [
        "Multiple Orgasms", "Continuous Orgasm", "Real Orgasm", "Internal Orgasm", "Forced Orgasm",
        "Convulsion", "Ahegao", "連続アクメ", "絶頂快楽", "絶叫アクメ", "強制アクメ", "痙攣イキ", "ガクガク", "腹イキ", "のけぞり",
        "悶絶",
    ],
    "Outdoor": [
        "青空SEX", "野外プレイ", "白昼堂々", "窓際",
    ],
    "Pajamas": [
        "部屋着",
    ],
    "Panty Shot": [
        "Panty Check", "Panties Peeking Out", "Panties", "パン染み",
    ],
    "Pantyhose": [
        "Tights", "Stockings", "Fishnets",
    ],
    "Petite": [
        "ミニ系", "ミニ", "Small Face",
    ],
    "Pickup": [
        "Pickup Artist",
    ],
    "Piercing": [
        "Tongue Piercing", "へそピ",
    ],
    "Pink Nipples": [
        "桃色乳首", "ピンク色乳首", "色素ピンク",
    ],
    "Reverse Threesome": [
        "Harem",
    ],
    "Saliva": [
        "よだれ",
    ],
    "Secret Account Guy": [
        "Ex Secret Account Guy",
    ],
    "Sensitive": [
        "Very Sensitive", "超敏感", "感度良好", "感度良し", "感度ヨシ",
    ],
    "Sex Worker": [
        "Sex Work", "Delivery Health", "Soapland", "立ちんぼ", "裏オプション",
    ],
    "Shaved": [
        "Hairless", "Shaving", "藻無", "藻なし",
    ],
    "Short Hair": [
        "Bob Cut", "Short Bob", "短髪",
    ],
    "Shy": [
        "Bashful", "恥ずかしがり", "照れ屋", "恥らしい", "恥らう君が見たいんだ",
    ],
    "Slender": [
        "Abs", "腹筋フェチ", "ガリ", "ガリ細", "極細", "超スレンダー", "スレンダー体型", "細身巨乳", "細見巨乳",
    ],
    "Socks": [
        "Knee-High Socks", "Loose Socks", "裸靴下",
    ],
    "Sportswear": [
        "Sports Bra",
    ],
    "Spread": [
        "開脚", "強制開脚", "強制くぱぁ", "まんぐり返し", "大の字", "Ｍ字",
    ],
    "Squirting": [
        "Squirting While Fucked", "電マ潮吹き", "手マン潮吹き", "潮吹きオナニー", "顔面潮吹き",
    ],
    "Standing": [
        "Standing Doggy", "Standing Carry",
    ],
    "Sugar Dating": [
        "Rental Girlfriend",
    ],
    "Sumata": [
        "Intercrural", "すまた",
    ],
    "Sweaty": [
        "汗", "汗だくSEX", "腋汗",
    ],
    "Swimsuit": [
        "School Swimsuit", "Micro Bikini", "競泳水着", "魔改造スク水",
    ],
    "Tall": [
        "長身美女", "足長", "足が長い", "逆身長差",
    ],
    "Tanned": [
        "Suntanned", "小麦色",
    ],
    "Teacher": [
        "Nursery Teacher", "Private Tutor",
    ],
    "Threesome": [
        "３P", "3Ｐ", "３Ｐ", "敏感３ｐ", "Skewered", "Multiple",
    ],
    "Tight Pussy": [
        "Tight", "キツマン", "締め付けマンコ", "超名器", "締め付けボディ", "締め付けボディー",
    ],
    "Toys": [
        "Toy Play", "Vibrator", "Egg Vibrator", "Dildo", "Clit Sucker", "ウーマナイザー", "吸うやつ（バイブ）",
        "電動バイブ", "電ドリ", "異物挿入",
    ],
    "Uniform": [
        "Sailor Uniform", "Gym Clothes", "Bloomers",
    ],
    "Virgin": [
        "Deflowering",
    ],
    "Voice Actress": [
        "声優の卵", "Anime Voice",
    ],
    "Working Girl": [
        "会社員", "働く女性", "New Graduate", "新入社員", "Part-Timer", "アルバイト", "Cafe Worker",
    ],
}

# removed: filters nothing, repeats a field Vault has (censorship, cup, age, height, resolution),
# shop wording, seller names / series, or says nothing
DROP = [
    "Amateur", "Personal Shoot", "Amateur Personal Shoot", "Original", "Fully Original",
    "Uncensored", "Light Mosaic", "F-cup", "E Cup", "G Cup", "D Cup", "C Cup", "Bカップ", "Kカップ",
    "Gカップ美乳", "18 Years Old", "19 Years Old", "20 Years Old", "23 Years Old", "18", "１８", "１９",
    "18なりたて", "172cm", "１７６cm", "１５０㎝", "１４９㎝", "4K", "８K", "High Quality", "超高画質", "With Bonus",
    "Review Bonus", "Bonus", "Special Edition", "Limited", "Sold Out", "Almost Sold Out",
    "Exclusive", "Anti-Piracy", "SSR", "Masterpiece", "Best Ever", "Hall of Fame", "Treasured",
    "Gem", "Favorite Amateur", "期間限定", "数量限定", "本数限定出演予定", "激レア", "希少", "独占", "神動画", "鉄板", "人気",
    "Subtitled", "One-Take", "One-Day Shoot", "Fixed Camera", "Cameraman Present", "Doujin AV",
    "Close-up", "総集編", "海外版", "スマホ撮影", "0930", "Scums Wish", "Autumn Mushroom Hunting",
    "Musume Gacha", "DIGITALTATTOO", "FC2-PPV", "AV女優の秘密のお仕事", "デストロン", "肉便企画", "Sexy", "Erotic",
    "Fetish", "Real", "Real Sex", "Genuine", "No Fakery", "Serious", "Private", "Secret", "SEX",
    "S", "M", "Pink", "Continuous", "Unstable", "Gap Moe", "At Home", "Date", "JAPANESE",
    "japanease", "日本人", "Semen", "Ejaculation", "Thin Hair", "Silky Hair", "Cool", "True Nature",
    "Forbidden", "FPS",
]

# implies a minor: never stored, whether the switch is on or not
ALWAYS_DROP = [
    "Loli", "口り", "J○", "Junior", "思春期", "初潮", "発育中", "未成熟な身体", "未成熟", "１○代", "学.生", "学制", "ジューダイ",
    "ティーン", "放課後", "スクールカースト",
]

_SRC = {s.lower(): t for t, srcs in MERGE.items() for s in srcs}
_DROP = {t.lower() for t in DROP}
_ALWAYS = {t.lower() for t in ALWAYS_DROP}


def rule(tag):
    """The standardized form of one stored tag: the merge target, None to remove it, or the
    tag itself."""
    t = (tag or "").strip()
    k = t.lower()
    if not t or k in _ALWAYS or k in _DROP:
        return None
    if k in _SRC:
        return _SRC[k]
    if _JP.search(t):
        return None
    return t


def is_on(con):
    try:
        r = con.execute("SELECT value FROM catalog_meta WHERE key=?", (META_KEY,)).fetchone()
    except Exception:
        return False
    return bool(r and r[0] == "on")


def _set(con, on):
    con.execute("CREATE TABLE IF NOT EXISTS catalog_meta (key TEXT PRIMARY KEY, value TEXT)")
    if on:
        con.execute("INSERT OR REPLACE INTO catalog_meta VALUES (?, 'on')", (META_KEY,))
    else:
        con.execute("DELETE FROM catalog_meta WHERE key=?", (META_KEY,))


def canon(con, tag):
    """For anything about to be stored by a scan or a merge: None = don't store."""
    t = (tag or "").strip()
    if not t or t.lower() in _ALWAYS:
        return None
    return rule(t) if is_on(con) else t


def cleanup_always(con):
    """Remove ALWAYS_DROP tags from the catalog. -> rows removed."""
    n = 0
    for (tag,) in con.execute("SELECT DISTINCT tag FROM tags").fetchall():
        if (tag or "").strip().lower() in _ALWAYS:
            n += con.execute("DELETE FROM tags WHERE tag=?", (tag,)).rowcount
    return n


def _plan(con):
    """[(code, old, new-or-None)] for every stored tag the rules change."""
    out = []
    for code, tag in con.execute("SELECT code, tag FROM tags").fetchall():
        new = rule(tag)
        if new != tag:
            out.append((code, tag, new))
    return out


def preview(con):
    """What switching on would do (or did, once on): counts plus the rules that touch this
    catalog, for the Settings tab."""
    rows = set(con.execute("SELECT code, tag FROM tags").fetchall())
    if is_on(con):                       # show the catalog as it was before switching on
        try:
            rows -= set(con.execute("SELECT code, tag FROM tag_rules_undo WHERE kind='added'"))
            rows |= set(con.execute("SELECT code, tag FROM tag_rules_undo WHERE kind='removed'"))
        except Exception:
            pass
    before = {t for _, t in rows}
    after, movies = set(), set()
    merged, removed = {}, {}
    for code, tag in rows:
        new = rule(tag)
        if new:
            after.add(new)
        if new != tag:
            movies.add(code)
            if new:
                merged.setdefault(new, {}).setdefault(tag, 0)
                merged[new][tag] += 1
            else:
                removed[tag] = removed.get(tag, 0) + 1
    undo = 0
    try:
        undo = con.execute("SELECT COUNT(*) FROM tag_rules_undo WHERE kind='removed'").fetchone()[0]
    except Exception:
        pass
    return {"on": is_on(con), "tags_before": len(before), "tags_after": len(after),
            "movies": len(movies), "undo_rows": undo,
            "merges": sorted(({"target": t, "sources": sorted(s.items(), key=lambda x: -x[1])}
                              for t, s in merged.items()), key=lambda m: m["target"].lower()),
            "removed": sorted(removed.items(), key=lambda x: (-x[1], x[0].lower()))}


def apply(con):
    """Switch on: standardize every stored tag, remembering what changed. Idempotent; also the
    catch-up pass while on (anything stored around canon() is fixed here). -> stats."""
    con.execute(UNDO)
    have = set(con.execute("SELECT code, tag FROM tags").fetchall())
    removed = added = 0
    for code, tag, new in _plan(con):
        con.execute("DELETE FROM tags WHERE code=? AND tag=?", (code, tag))
        removed += 1
        if tag.lower() not in _ALWAYS:
            con.execute("INSERT OR IGNORE INTO tag_rules_undo VALUES (?, ?, 'removed')", (code, tag))
        if new and (code, new) not in have:
            con.execute("INSERT OR IGNORE INTO tags (code, tag) VALUES (?, ?)", (code, new))
            con.execute("INSERT OR IGNORE INTO tag_rules_undo VALUES (?, ?, 'added')", (code, new))
            have.add((code, new))
            added += 1
    _set(con, True)
    con.commit()
    return {"removed": removed, "added": added}


def restore(con):
    """Switch off: put back the tags apply() changed. Tags you deleted since (tombstones) and
    ALWAYS_DROP tags stay gone; tags stored while the switch was on stay as they are."""
    con.execute(UNDO)
    try:
        dead = {k for (k,) in con.execute(
            "SELECT key FROM catalog_tombstones WHERE kind='tag'")}
    except Exception:
        dead = set()
    works = {c for (c,) in con.execute("SELECT code FROM works")}
    gone = back = 0
    for code, tag in con.execute(
            "SELECT code, tag FROM tag_rules_undo WHERE kind='added'").fetchall():
        gone += con.execute("DELETE FROM tags WHERE code=? AND tag=?", (code, tag)).rowcount
    for code, tag in con.execute(
            "SELECT code, tag FROM tag_rules_undo WHERE kind='removed'").fetchall():
        if code in works and tag.lower() not in dead and tag.lower() not in _ALWAYS:
            back += con.execute("INSERT OR IGNORE INTO tags (code, tag) VALUES (?, ?)",
                                (code, tag)).rowcount
    con.execute("DELETE FROM tag_rules_undo")
    _set(con, False)
    con.commit()
    return {"restored": back, "removed": gone}


if __name__ == "__main__":       # make_release: ship the catalog with the sources' own tags
    import sqlite3
    import sys
    c = sqlite3.connect(sys.argv[2])
    if sys.argv[1] == "ship":
        if is_on(c):
            print("tag rules: restored", restore(c))
        cleanup_always(c)
        c.execute("DROP TABLE IF EXISTS tag_rules_undo")
        c.commit()
