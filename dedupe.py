#!/usr/bin/env python3
"""
dedupe.py — find & merge duplicate actress records in catalog.db.

Same human = one record. Duplicates creep in when a name is typed in a different word
order ("Sora Mikumo" vs "Mikumo Sora") or language, so the owned titles land on one
record and enrichment/wishlist titles on another, and they never combine.

find_duplicate_groups() is read-only (reports candidate groups). merge_group() folds
sources into a target: repoints works, folds aliases/links, fills empty body-info from
the sources, keeps every spelling as an alias, then deletes the emptied source rows.
Never deletes a work; never overwrites a non-empty curated field.
"""
import re
import sqlite3
import sys


def _norm(s):
    """lowercase, strip punctuation, collapse spaces."""
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _token_key(s):
    """word-order-insensitive key: sorted normalized tokens."""
    toks = _norm(s).split()
    return " ".join(sorted(toks)) if toks else ""


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def find_duplicate_groups(con):
    """Return [{'ids':[...], 'reasons':[...], 'rows':[...] }] — actresses that are the
    same person by a high-confidence signal. Conservative: single-token romaji names
    (e.g. many different 'Yui's) only group when a JP/kana name also matches."""
    rows = con.execute(
        "SELECT id,name_en,name_jp,name_kana FROM actresses").fetchall()
    uf = _UF()
    reasons = {}

    def note(a, b, why):
        uf.union(a, b)
        reasons.setdefault(frozenset((a, b)), why)

    # index only by ROCK-SOLID signals. Alias overlap is deliberately NOT auto-merged:
    # alias lists pick up co-stars / scraped noise, so a shared alias is a *hint*, not
    # proof (surfaced separately via alias_matches() for manual review).
    by_jp, by_kana, by_tokkey = {}, {}, {}
    for r in rows:
        aid = r["id"]
        jp = _norm(r["name_jp"])
        ka = _norm(r["name_kana"])
        tk = _token_key(r["name_en"])
        multi = len(_norm(r["name_en"]).split()) >= 2
        if jp:
            by_jp.setdefault(jp, []).append(aid)
        if ka:
            by_kana.setdefault(ka, []).append(aid)
        if tk and multi:
            by_tokkey.setdefault(tk, []).append(aid)

    for bucket, why in ((by_jp, "same Japanese name"),
                        (by_kana, "same kana"),
                        (by_tokkey, "same name, reversed word order")):
        for _, ids in bucket.items():
            for i in range(1, len(ids)):
                note(ids[0], ids[i], why)

    groups = {}
    for r in rows:
        groups.setdefault(uf.find(r["id"]), []).append(r["id"])
    out = []
    rowmap = {r["id"]: r for r in rows}
    for _, ids in groups.items():
        if len(ids) < 2:
            continue
        why = set()
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                w = reasons.get(frozenset((ids[i], ids[j])))
                if w:
                    why.add(w)
        out.append({"ids": sorted(ids), "reasons": sorted(why),
                    "rows": [dict(rowmap[i]) for i in sorted(ids)]})
    return out


def alias_matches(con, exclude_ids=None):
    """Lower-confidence hints: record A's name (en/jp) equals record B's alias. Returned
    for MANUAL review, never auto-merged — alias lists carry co-stars and scraped noise."""
    exclude = exclude_ids or set()
    rows = con.execute("SELECT id,name_en,name_jp FROM actresses").fetchall()
    names = {}
    for r in rows:
        for nm in (_norm(r["name_en"]), _norm(r["name_jp"])):
            if nm and (len(nm.split()) >= 2 or re.search(r"[぀-ヿ一-鿿]", nm)):
                names.setdefault(nm, set()).add(r["id"])
    hints = []
    for aid, alias in con.execute("SELECT actress_id,alias FROM aliases"):
        na = _norm(alias)
        for other in names.get(na, ()):
            if other != aid:
                pair = frozenset((aid, other))
                if pair not in {frozenset(p) for p in exclude}:
                    hints.append((sorted(pair), alias))
    # de-dup
    seen, out = set(), []
    for pair, alias in hints:
        k = tuple(pair)
        if k not in seen:
            seen.add(k)
            out.append({"ids": pair, "via_alias": alias})
    return out


def _work_counts(con, ids):
    counts = {}
    for aid in ids:
        owned = con.execute("SELECT COUNT(*) FROM works WHERE actress_id=? "
                            "AND in_library=1", (aid,)).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM works WHERE actress_id=?",
                            (aid,)).fetchone()[0]
        counts[aid] = (owned, total)
    return counts


def pick_target(con, ids):
    """The record to keep: most owned titles, then most total, then most complete, then
    lowest id. That keeps the record you actually curate as the survivor."""
    counts = _work_counts(con, ids)

    def completeness(aid):
        r = con.execute("SELECT name_en,name_jp,age,height,cup,bust FROM actresses "
                        "WHERE id=?", (aid,)).fetchone()
        return sum(1 for v in r if v)
    return sorted(ids, key=lambda a: (-counts[a][0], -counts[a][1],
                                      -completeness(a), a))[0]


def merge_group(con, ids, target=None):
    """Fold every id in `ids` except the target INTO the target. Returns a summary."""
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        return {"skipped": True}
    target = target or pick_target(con, ids)
    sources = [i for i in ids if i != target]
    moved = 0
    for s in sources:
        # repoint works (code is PK -> no collisions possible)
        moved += con.execute("UPDATE works SET actress_id=? WHERE actress_id=?",
                            (target, s)).rowcount
        # keep every spelling of the source as an alias of the target
        srow = con.execute("SELECT name_en,name_jp,name_kana FROM actresses WHERE id=?",
                          (s,)).fetchone()
        for nm in srow:
            if nm:
                con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) "
                            "VALUES(?,?)", (target, nm))
        con.execute("INSERT OR IGNORE INTO aliases(actress_id,alias) "
                    "SELECT ?, alias FROM aliases WHERE actress_id=?", (target, s))
        con.execute("INSERT OR IGNORE INTO links(actress_id,kind,url) "
                    "SELECT ?, kind, url FROM links WHERE actress_id=?", (target, s))
        # fill empty body-info on the target from the source (never overwrite)
        for col in ("name_jp", "name_kana", "age", "birthdate", "height", "cup",
                    "bust", "waist", "hip", "measurements_raw", "description",
                    "image_url"):
            con.execute(
                f"UPDATE actresses SET {col}=COALESCE(NULLIF({col},''), "
                f"(SELECT {col} FROM actresses WHERE id=?)) WHERE id=? "
                f"AND ({col} IS NULL OR {col}='')", (s, target))
        # tidy up the emptied source, then remove it
        con.execute("DELETE FROM aliases WHERE actress_id=?", (s,))
        con.execute("DELETE FROM links WHERE actress_id=?", (s,))
        con.execute("DELETE FROM actresses WHERE id=?", (s,))
    con.commit()
    return {"target": target, "sources": sources, "works_moved": moved}


if __name__ == "__main__":
    import json
    db = sys.argv[1] if len(sys.argv) > 1 else "db/catalog.db"
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    groups = find_duplicate_groups(con)
    if len(sys.argv) > 2 and sys.argv[2] == "--merge":
        done = [merge_group(con, g["ids"]) for g in groups]
        print(json.dumps({"merged_groups": len(done), "detail": done}, indent=2))
    else:
        print(f"DRY RUN — {len(groups)} duplicate group(s):\n")
        for g in groups:
            cnt = _work_counts(con, g["ids"])
            tgt = pick_target(con, g["ids"])
            for r in g["rows"]:
                o, t = cnt[r["id"]]
                star = " <- keep" if r["id"] == tgt else ""
                print(f'  [{r["id"]:>4}] {r["name_en"] or "?":24} {r["name_jp"] or "":10} '
                      f'owned={o} total={t}{star}')
            print(f'        reason: {", ".join(g["reasons"])}\n')
