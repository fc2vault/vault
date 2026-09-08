#!/usr/bin/env python3
"""Ingest the bigboobs archive scrape (arc_a.json + arc_b.json) into catalog.db:
fill actress age/measurements/aliases/description, upsert works with
release_date/producer/fav/censorship, and derive EN genre tags from titles."""
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db", "catalog.db")
SCRATCH = sys.argv[1] if len(sys.argv) > 1 else "."

TAGMAP = [
    (r"中出|中だ|膣内射精|種付|孕ま|妊娠", "Creampie"), (r"潮吹|潮まみ|イキ潮|潮スプ|大量潮", "Squirting"),
    (r"ハメ撮", "POV"), (r"素人", "Amateur"), (r"顔出", "Face Shown"),
    (r"フェラ|咥え|しゃぶ|イラマ|口内", "Blowjob"), (r"パイズリ", "Titfuck"), (r"騎乗位", "Cowgirl"),
    (r"爆乳|美巨乳|巨乳|神乳|Hカップ|Ｈカップ|Gカップ|Ｇカップ|Iカップ|Ｉカップ|Jカップ|Ｊカップ", "Big Tits"),
    (r"貧乳|ちっぱい|微乳", "Small Tits"), (r"スレンダー|華奢|細身", "Slender"),
    (r"人妻|若妻|嫁|美人妻", "Married Woman"), (r"女子大生|女子大|大学生|JD|ＪＤ|音大", "College Girl"),
    (r"メンエス|メンズエステ|エステ", "Massage"), (r"3P|３P|4P|４P|5P|５P|乱交|ハーレム|複数プレイ", "Group Sex"),
    (r"顔射|ぶっかけ|顔面", "Facial"), (r"ごっくん", "Swallow"), (r"アナル", "Anal"),
    (r"コスプレ|制服|コス|メイド|ナース|バニー|セーラー", "Cosplay"), (r"妊婦", "Pregnant"),
    (r"黒人|異人種|白人|海外", "Interracial"), (r"NTR|寝取|ネトリ|不倫|セフレ", "NTR/Cheating"),
    (r"手コキ|モモコキ", "Handjob"), (r"オナニー|自慰", "Masturbation"), (r"初撮|デビュー", "Debut"),
    (r"パイパン", "Shaved"), (r"電マ|バイブ|ディルド|ローター|おもちゃ", "Toys"),
    (r"お嬢様|箱入り", "Ojousama"), (r"美脚", "Legs"),
    (r"OL|事務員|受付|店員|看護|ナース|歯科|保育士", "Working Girl"), (r"ギャル", "Gal"),
    (r"露天|温泉|お風呂|風呂", "Bath"), (r"野外|露出", "Outdoor"),
]
TAGMAP = [(re.compile(p), t) for p, t in TAGMAP]


def derive_tags(title, cens):
    out = []
    if cens:
        out.append("Uncensored")
    for rx, tag in TAGMAP:
        if rx.search(title or ""):
            out.append(tag)
    return out


def parse_age(bd):
    m = re.search(r"現在\s*(\d+)\s*歳", bd or "")
    return int(m.group(1)) if m else None


def parse_birth_iso(bd):
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", bd or "")
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_size(sz):
    h = re.search(r"T(\d{2,3})", sz or "")
    b = re.search(r"B(\d{2,3})", sz or "")
    cup = re.search(r"([A-Z])カップ", sz or "")
    w = re.search(r"W(\d{2,3})", sz or "")
    hip = re.search(r"H(\d{2,3})", sz or "")
    return (int(h.group(1)) if h else None, int(b.group(1)) if b else None,
            cup.group(1) if cup else None, int(w.group(1)) if w else None,
            int(hip.group(1)) if hip else None)


def main():
    con = sqlite3.connect(DB)
    aid_map = {r[0]: r[1] for r in
               con.execute("SELECT archive_id,id FROM actresses WHERE archive_id IS NOT NULL")}
    recs = []
    for fn in ("arc_a.json", "arc_b.json"):
        with open(os.path.join(SCRATCH, fn)) as f:
            recs += json.load(f)

    n_act = n_work_new = n_work_upd = n_tags = 0
    for rec in recs:
        archive_id, name, aliases, bd, sz, feat = rec[0], rec[1], rec[2], rec[3], rec[4], rec[5]
        works = rec[6] if len(rec) > 6 else []
        actress_id = aid_map.get(archive_id)
        if actress_id is None:
            continue
        age = parse_age(bd)
        birth = parse_birth_iso(bd)
        h, b, cup, w, hip = parse_size(sz)
        con.execute(
            """UPDATE actresses SET
                 age=COALESCE(?,age), birthdate=COALESCE(?,birthdate),
                 height=COALESCE(?,height), bust=COALESCE(?,bust), cup=COALESCE(NULLIF(?,''),cup),
                 waist=COALESCE(?,waist), hip=COALESCE(?,hip),
                 measurements_raw=COALESCE(NULLIF(?,''),measurements_raw),
                 description=COALESCE(NULLIF(?,''),description)
               WHERE id=?""",
            (age, birth, h, b, cup, w, hip, sz, feat, actress_id))
        n_act += 1
        for alias in (aliases or "").split("・"):
            alias = alias.strip()
            if alias:
                con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) VALUES(?,?)",
                            (actress_id, alias))
        for wk in works:
            code_num, date8, fav, cens, producer, title = (wk + [""] * 6)[:6]
            code = "FC2-PPV-" + str(code_num)
            rel = f"{date8[0:4]}-{date8[4:6]}-{date8[6:8]}" if len(date8) == 8 else ""
            cur = con.execute("SELECT code FROM works WHERE code=?", (code,)).fetchone()
            if cur:
                con.execute(
                    """UPDATE works SET
                         actress_id=COALESCE(actress_id,?),
                         release_date=COALESCE(NULLIF(?,''),release_date),
                         title=COALESCE(NULLIF(?,''),title),
                         producer=COALESCE(NULLIF(?,''),producer),
                         fav_count=COALESCE(?,fav_count),
                         censorship=? WHERE code=?""",
                    (actress_id, rel, title, producer, fav or None,
                     "uncensored" if cens else "censored", code))
                n_work_upd += 1
            else:
                con.execute(
                    """INSERT INTO works(code,actress_id,release_date,title,producer,fav_count,
                         censorship,in_library,availability,source)
                       VALUES(?,?,?,?,?,?,?,0,'missing','bigboobs')""",
                    (code, actress_id, rel, title, producer, fav or None,
                     "uncensored" if cens else "censored"))
                n_work_new += 1
            for tag in derive_tags(title, cens):
                con.execute("INSERT OR IGNORE INTO tags(code,tag) VALUES(?,?)", (code, tag))
                n_tags += 1
    con.commit()
    print(f"actresses updated: {n_act}")
    print(f"works: {n_work_upd} updated, {n_work_new} new (missing)")
    print(f"tag rows inserted: {n_tags}")
    print("distinct tags:", con.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0])
    print("works w/ release_date:", con.execute("SELECT COUNT(*) FROM works WHERE release_date!=''").fetchone()[0])
    print("actresses w/ age:", con.execute("SELECT COUNT(*) FROM actresses WHERE age IS NOT NULL").fetchone()[0])
    con.close()


if __name__ == "__main__":
    main()
