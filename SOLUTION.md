# Blast Radius Selector

This directory contains a patch-independent test-impact engine built from
sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## Architecture

```
sample/commits/<id>.patch
        |
        v
selector.py  -- reads /data/tests.json and /app/maps only
  |-- unified-diff parser (old/anchored/new lines, context)
  |-- map lookup: line masks, symbols, fixtures, imports, process tests
  |-- hybrid policy: coverage, fixtures, structural component tests,
  |                  process-pool anchors and safe fallbacks
  |-- LPT + deterministic local-search scheduler (1..8 workers)
        |
        v
one JSON line per patch: {"commit": id, "workers": [[...]]}
```

The runtime does **not** import sqlglot, pytest or coverage. It never reads
`answers.json`, patch fingerprints, or commit-specific test lists and has no
network access.

## Maps

`tools/build_maps.py` is the build-time tool. It consumes the pinned checkout,
`tests.json`, and coverage data files produced with
`dynamic_context = test_function`. It writes compressed, ordinary repository
files:

| file | contents |
| --- | --- |
| `maps/coverage.json.gz` | source path -> line -> base64 test bitmask, plus AST symbols |
| `maps/dependencies.json.gz` | module imports, test imports, process-pool test list |
| `maps/fixtures.json.gz` | fixture path -> consuming test numbers |
| `maps/tests.json.gz` | canonical test metadata and runtime milliseconds |
| `maps/manifest.json` | provenance and map counts |

Masks use the exact 0..1619 test numbering from `tests.json`; the supplied one
post-label test is intentionally not invented. `tools/validate_maps.py` checks
portable paths, contiguous numbers, masks, fixture rules, process metadata and
(the optional checkout) source existence.

## Selection policy

1. Parse every hunk and collect old, anchored and new lines.
2. Skip comment-only edits and safe identity spellings (`is`/`==` around
   `None`, booleans and enum members), plus a `copy=` flag on a freshly
   constructed expression.
3. For source changes, select tests whose line mask covers a changed line.
4. For structural parser/generator class mappings, keep only the component's
   own test file when that is what covers the mapping; do not select incidental
   imports from unrelated executor/parser tests.
5. Keep dialect-level class-constant changes on the `test_simplify` fixture
   anchor (the integration test that exercises the fixture set).
6. Add process-pool anchors where parent coverage cannot see the child:
   `test_optimizer` for serde/core expression changes, executor TPC tests for
   the main optimizer pipeline, and `test_optimize` for typing changes.
7. Fixture changes map to their consuming tests; changed test files select the
   tests in that file. Unknown source files, new fixtures, empty/malformed
   patches and changed helper/config files fall back to the full suite rather
   than emitting an unsafe empty selection.

The fallbacks are intentionally conservative. The only zero-test outputs are
for changes that are provably comment/identity-only or that have no executable
selected path.

## Scheduling

For each candidate worker count `1..8`, tests are placed longest-first on the
least-loaded worker. A deterministic move/swap local search repairs LPT
placements, and the candidate with the smallest
`max(load) + 2 * worker_count` is chosen. Workers are never empty, tests are
deduplicated, and the output is deterministic.

## Sample results

Command:

```sh
python selector.py --data sample --maps maps > sample_output.jsonl
python score.py sample sample/answers.json sample_output.jsonl --cpu-seconds 0.031
python tools/evaluate.py
```

| candidate | simulated | selected | extra | missed | sample CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| A direct coverage | 1284.672 s | 74 | 18 | 4 | 0.016 s |
| B coverage + dependencies | 99.274 s | 86 | 13 | 0 | <0.001 s |
| C coverage + dependencies + symbols | 99.274 s | 86 | 13 | 0 | <0.001 s |
| D conservative hybrid | 99.274 s | 86 | 13 | 0 | <0.001 s |

The oracle lower bound using only the supplied failing-test labels is 94.351 s;
the final selector is 4.9 s above it. The remaining extra selections are
edge-case local changes whose covering tests do not happen to fail on the
sample. They are retained because excluding them would risk unseen commits.

Final pre-submission validation measured three consecutive runs:

| run | simulated | selector CPU | combined | misses |
| --- | ---: | ---: | ---: | ---: |
| 1 | 99.274 s | 0.078125 s | 99.352125 s | 0 |
| 2 | 99.274 s | 0.062500 s | 99.336500 s | 0 |
| 3 | 99.274 s | 0.062500 s | 99.336500 s | 0 |

The median combined total was 99.3365 s, which rounds to **99.3 s**. Output was
byte-identical across all three runs. Native peak RSS was approximately
41.5 MiB. The CPU figure is native Windows process user+kernel time, not the
container CPU the server will charge; Docker/container validation remains
outstanding.

Exact partition search confirmed that the scheduler's result is optimal for
every non-empty sample commit. The dominant 21.454 s `test_execute_tpcds` test
sets a makespan floor on the two large commits (`2jrq`, `3utz`), so no worker
repartition can improve them. The small remaining selection overhead is the
2 s per-worker charge for 22 non-empty commits (44.0 s total).

These are **local sample measurements**, not an official leaderboard result.
The unseen final patch set may include behaviors not represented by the 30
sample patches.

## Docker

`Dockerfile` is at the repository root. It:

- uses the challenge's pinned `python:3.13-slim` digest;
- clones sqlglot and checks out the exact base commit;
- copies only `selector.py` and `maps/`;
- runs as `nobody` (`65534:65534`);
- sets `PYTHONDONTWRITEBYTECODE=1`, `MAPS_DIR=/app/maps`,
  `DATA_DIR=/data`;
- writes nothing and needs no network at runtime.

The current development host has neither Docker, Podman, GNU `make`, nor a WSL
distribution, so the image was **not built or run here**. The local selector was
exercised directly under the sample data. The Docker path, `nobody` execution,
read-only container filesystem and official container CPU measurement remain
unverified. Do not treat the native measurements above as a Docker result.

## Reproduction and checks

```sh
python -m py_compile selector.py tools/build_maps.py tools/evaluate.py tools/validate_maps.py
python tools/validate_maps.py --root .analysis/sqlglot
python tools/evaluate.py
python selector.py --data sample --maps maps > run1.jsonl
cmp run1.jsonl run2.jsonl                 # after a second run
python score.py sample sample/answers.json sample_output.jsonl --cpu-seconds 0.031
```

`make sample` is the intended clean container check when Docker is available.

Additional edge-case checks cover empty and malformed patches, source deletion,
new source files, CRLF input, missing final newlines and hunk content whose text
begins with `---` or `+++`. Malformed/empty patches, deleted source modules and
new source files fall back to the full suite; comment/identity/`copy=` no-ops
remain empty.
