# Tests

Standard library `unittest`, no dependencies. From the project root:

```
python3 -m unittest discover -s tests          # quiet
python3 -m unittest discover -s tests -v       # per-test
python3 -m unittest tests.test_dedupe          # one file
```

Everything runs on temp directories and temp databases. No test reads or writes the real
library, `db/catalog.db`, or `webui/cache/` — `test_serve.py` points `VAULT_DATA` at a scratch
directory before importing `serve.py`. Two tests reach the network only to be skipped if the
configured library is not mounted.

## What is covered, and why

Each case is something that actually broke:

| file | guards against |
|---|---|
| `test_scan.py` | the 18+ page being stored as metadata; censorship read from stray page text instead of `mosaicStatus`/tags; a manual censorship value being overwritten; the ffjav release date coming from another movie's search hit; an ambiguous alias folding two performers into one; a dead `links` row resolving to a deleted actress; co-stars never being recorded; a solo page wiping an existing cast |
| `test_organize.py` | the plan deleting or overwriting anything; subtitles/posters treated as junk; apply running rows the preview never showed; a stale plan being applied after the library changed |
| `test_dedupe.py` | a merge dropping the portrait, orphaning co-star rows, demoting a lead credit, or losing `archive_id` / `fc2ppvdb_uuid` |
| `test_serve.py` | `_Duplicates` / `@eaDir` being walked back in; a short or failed walk replacing a good index; two folders with one code sharing an identity; a public FlareSolverr URL being accepted |
| `test_cleaner.py` | the download cleaner deleting `.scenes.json` sidecars, subtitles and posters, or a subfolder holding a real video; `refolder_fc2.py` being run against the library; an unreadable file making a folder look *smaller* (which selects REPLACE TARGET and deletes the good copy); `_move` swallowing a destination collision |

## Keeping them honest

The suite was validated by running it against the pre-fix copies of each module: **24 of 68 tests
fail** on the old code, and the failures line up with the bugs they describe. A test that cannot
fail is not protecting anything — if you change one of these behaviours deliberately, make the
test fail first, then update it.
