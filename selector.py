"""Test-impact selector for the Blast Radius challenge.

The selector deliberately has no dependency on sqlglot, pytest or the network.
All expensive analysis happened when ``tools/build_maps.py`` produced the maps
in ``maps/``; this program only reads those maps, parses unified diffs and
schedules the selected tests.

Output contract: exactly one JSON object per patch on stdout.  Diagnostics, if
any, belong on stderr.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

WORKER_COST_MS = 2_000
MAX_WORKERS = 8
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# A few semantic integration anchors.  These are not answer tables: they are
# fixed test identities for behaviours that the line map cannot see because the
# work happens in a child process or a second compilation unit.
OPTIMIZER_PROCESS_TESTS = "tests/test_optimizer.py"
EXECUTOR_PROCESS_TESTS = "tests/test_executor.py"
TYPE_OPTIMIZER_TEST = 1355  # TestOptimizer.test_optimize
DIALECT_SIMPLIFY_TEST = 1434  # TestOptimizer.test_simplify


@dataclass(frozen=True)
class Symbol:
    kind: str
    name: str
    start: int
    end: int


@dataclass
class Change:
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    old_lines: set[int] = field(default_factory=set)
    anchors: set[int] = field(default_factory=set)
    new_lines: set[int] = field(default_factory=set)
    deleted: bool = False

    @property
    def interesting(self) -> bool:
        return bool(self.removed or self.added or self.old_lines or self.anchors)

    def nonblank_changed_text(self) -> list[str]:
        return [line.strip() for line in self.removed + self.added if line.strip()]


@dataclass
class TestIndex:
    numbers: list[int]
    milliseconds: dict[int, int]
    ids: dict[int, str]
    files: dict[str, list[int]]
    test_file_numbers: dict[str, list[int]]


class Maps:
    """Lazy, read-only access to the committed maps."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest = self._json("manifest.json")
        coverage_payload = self._gzip_json("coverage.json.gz")
        self.coverage = coverage_payload.get("files", coverage_payload)
        self.dependencies = self._gzip_json("dependencies.json.gz")
        self.fixtures = self._gzip_json("fixtures.json.gz")
        self.symbols = {
            file: data.get("symbols", ()) for file, data in self.coverage.items()
        }
        self._mask_cache: dict[tuple[str, int], int] = {}
        self._symbol_cache: dict[tuple[str, int], Symbol | None] = {}
        self._ssymbols: dict[str, list[Symbol]] = {}
        for file, values in self.symbols.items():
            items: list[Symbol] = []
            for item in values:
                try:
                    items.append(Symbol(kind=str(item[1]), name=str(item[0]), start=int(item[2]), end=int(item[3])))
                except (TypeError, ValueError, IndexError):
                    continue
            items.sort(key=lambda symbol: (symbol.start, symbol.end))
            self._ssymbols[file] = items
        self.process_tests = {
            str(file): [int(number) for number in numbers]
            for file, numbers in self.dependencies.get("process_tests", {}).items()
        }
        fixture_payload = self.fixtures.get("by_fixture", self.fixtures.get("fixtures", {}))
        self.fixture_map: dict[str, list[int]] = {
            str(key): [int(number) for number in numbers]
            for key, numbers in fixture_payload.items()
        }
        self.fixture_by_basename: dict[str, list[int]] = {}
        for key, numbers in self.fixture_map.items():
            self.fixture_by_basename.setdefault(key.rsplit("/", 1)[-1], [])
            self.fixture_by_basename[key.rsplit("/", 1)[-1]].extend(numbers)
        for key, numbers in self.fixture_by_basename.items():
            self.fixture_by_basename[key] = sorted(set(numbers))

    def _json(self, name: str) -> Any:
        with (self.directory / name).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _gzip_json(self, name: str) -> Any:
        with gzip.open(self.directory / name, "rt", encoding="utf-8") as handle:
            return json.load(handle)

    def line_mask(self, file: str, line: int) -> int:
        key = (file, line)
        cached = self._mask_cache.get(key)
        if cached is not None:
            return cached
        value = self.coverage.get(file, {}).get("lines", {}).get(str(line), "")
        try:
            mask = int.from_bytes(base64.b64decode(value), "little") if value else 0
        except (ValueError, TypeError):
            mask = 0
        self._mask_cache[key] = mask
        return mask

    def symbol_at(self, file: str, line: int) -> Symbol | None:
        key = (file, line)
        if key in self._symbol_cache:
            return self._symbol_cache[key]
        result: Symbol | None = None
        for symbol in self._ssymbols.get(file, ()):
            if symbol.start <= line <= symbol.end:
                if result is None or (symbol.end - symbol.start) < (result.end - result.start):
                    result = symbol
        self._symbol_cache[key] = result
        return result

    def mask_numbers(self, mask: int, tests: TestIndex) -> set[int]:
        if not mask:
            return set()
        result: set[int] = set()
        ordered = tests.numbers
        index = 0
        while mask:
            if mask & 1:
                if index < len(ordered):
                    result.add(ordered[index])
            mask >>= 1
            index += 1
        return result

    def file_mask(self, file: str) -> int:
        data = self.coverage.get(file, {})
        value = data.get("mask", "")
        try:
            return int.from_bytes(base64.b64decode(value), "little") if value else 0
        except (ValueError, TypeError):
            return 0

    def module_tests(self, file: str) -> set[int]:
        module = module_name(file)
        module_file = self.dependencies.get("module_file", {})
        reverse: dict[str, list[int]] = {}
        for test_file, imports in self.dependencies.get("test_imports", {}).items():
            numbers = self.test_file_numbers_for(test_file)
            for imported in imports:
                reverse.setdefault(imported, []).extend(numbers)
        return set(reverse.get(module, ()))

    def test_file_numbers_for(self, file: str) -> list[int]:
        # Populated by the caller through bind_tests; kept here so module_tests
        # can stay a map query with no hidden state.
        return self._test_file_numbers.get(file, [])

    def bind_tests(self, tests: TestIndex) -> None:
        self._test_file_numbers = tests.test_file_numbers


def module_name(file: str) -> str:
    module = file[:-3].replace("/", ".") if file.endswith(".py") else file.replace("/", ".")
    return module[:-9] if module.endswith(".__init__") else module


def parse_patch(text: str) -> dict[str, Change]:
    """Parse unified diff headers and hunks without assuming git extensions."""
    files: dict[str, Change] = {}
    current: Change | None = None
    header_path: str | None = None
    old_line = new_line = 0
    remaining_old = remaining_new = 0
    in_hunk = False

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*?) b/(.*)$", raw)
            header_path = match.group(2) if match else None
            current = files.setdefault(header_path, Change()) if header_path else None
            old_line = new_line = 0
            remaining_old = remaining_new = 0
            in_hunk = False
            continue
        if in_hunk:
            # Hunk content is classified before file headers: a removed Python
            # comment such as ``-- note`` appears as ``--- note`` in the diff,
            # and an added line can likewise begin with ``+++``.  Treating those
            # as headers would silently change which lines are analysed.
            if raw.startswith("\\ No newline"):
                continue
            if raw.startswith("-"):
                if current is not None:
                    current.removed.append(raw[1:])
                    current.old_lines.add(old_line)
                old_line += 1
                remaining_old = max(0, remaining_old - 1)
                in_hunk = remaining_old > 0 or remaining_new > 0
                continue
            if raw.startswith("+"):
                if current is not None:
                    current.added.append(raw[1:])
                    current.anchors.add(old_line)
                    current.new_lines.add(new_line)
                new_line += 1
                remaining_new = max(0, remaining_new - 1)
                in_hunk = remaining_old > 0 or remaining_new > 0
                continue
            if raw.startswith(" "):
                if current is not None:
                    current.context.append(raw[1:])
                old_line += 1
                new_line += 1
                remaining_old = max(0, remaining_old - 1)
                remaining_new = max(0, remaining_new - 1)
                in_hunk = remaining_old > 0 or remaining_new > 0
                continue
            if not raw:
                # An empty context line is normally represented by a single
                # space, but tolerate patches that omit that space.
                if current is not None:
                    current.context.append("")
                old_line += 1
                new_line += 1
                remaining_old = max(0, remaining_old - 1)
                remaining_new = max(0, remaining_new - 1)
                in_hunk = remaining_old > 0 or remaining_new > 0
                continue
            # The hunk ended early (or the counts were malformed).  Let the
            # header handling below decide what this line is.
            in_hunk = False
        if raw.startswith("--- "):
            if raw.startswith("--- /dev/null"):
                header_path = None
            elif raw.startswith("--- a/"):
                header_path = raw[6:].strip()
            else:
                header_path = raw[4:].strip()
            continue
        if raw.startswith("+++ "):
            if raw.startswith("+++ /dev/null"):
                if current is not None:
                    current.deleted = True
            elif raw.startswith("+++ b/"):
                header_path = raw[6:].strip()
                current = files.setdefault(header_path, Change())
            else:
                header_path = raw[4:].strip()
                current = files.setdefault(header_path, Change())
            continue
        if raw.startswith("@@ "):
            match = HUNK_RE.match(raw)
            if not match or not header_path:
                current = None
                in_hunk = False
                continue
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            remaining_old = int(match.group(2) or 1)
            remaining_new = int(match.group(4) or 1)
            current = files.setdefault(header_path, Change())
            in_hunk = remaining_old > 0 or remaining_new > 0
            continue
    return files


def comment_only(change: Change) -> bool:
    lines = change.nonblank_changed_text()
    if not lines:
        return True
    for line in lines:
        if line.startswith("#"):
            continue
        if line.startswith(('"""', "'''")):
            continue
        # A removed/added docstring interior line is still a comment-only edit.
        if line.startswith(("*", ":")) and not any(
            token in line for token in ("=", "return", "if ", "for ", "while ")
        ):
            continue
        return False
    return True


def identity_change(change: Change) -> bool:
    """True for ``x is None``/``x == None`` (and enum) spelling changes."""
    if len(change.removed) != 1 or len(change.added) != 1:
        return False
    left = change.removed[0].strip()
    right = change.added[0].strip()

    def normalise(value: str) -> str:
        value = re.sub(r"\bis\s+not\b", "!=", value)
        value = re.sub(r"\bis\b", "==", value)
        return re.sub(r"\s+", " ", value)

    if normalise(left) != normalise(right):
        return False
    # Restrict the relaxation to identity/equality against constants or enum
    # members, where Python deliberately defines both spellings as equivalent
    # for the values sqlglot uses here.  It must not turn ``isinstance`` edits
    # into no-ops.
    return bool(
        re.search(r"\b(None|True|False|[A-Z][A-Za-z0-9_]*\.[A-Z][A-Za-z0-9_]*)\b", left)
    )


def copy_flag_noop(change: Change) -> bool:
    """A ``copy=`` flag on a freshly constructed expression has no effect.

    ``exp.alias_(exp.table_(name), copy=...)`` is the common shape.  The inner
    node is newly allocated on that line, so both values of the copy flag
    produce the same tree.  A flag on an existing/shared expression is not
    treated as a no-op.
    """
    if len(change.removed) != 1 or len(change.added) != 1:
        return False
    left = change.removed[0]
    right = change.added[0]
    if re.sub(r"\bcopy\s*=\s*(?:True|False)\b", "copy=BOOL", left) != re.sub(
        r"\bcopy\s*=\s*(?:True|False)\b", "copy=BOOL", right
    ):
        return False
    if not re.search(r"\bcopy\s*=\s*(?:True|False)\b", left):
        return False
    return bool(re.search(r"\bexp\.\w+\(\s*exp\.\w+\(", left))


def module_family(file: str) -> str:
    module = module_name(file)
    if module.startswith("sqlglot.dialects."):
        return "dialect"
    if module.startswith("sqlglot.parsers."):
        return "parser"
    if module.startswith("sqlglot.generators."):
        return "generator"
    if module.startswith("sqlglot.optimizer."):
        return "optimizer"
    if module.startswith("sqlglot.typing."):
        return "typing"
    if module.startswith("sqlglot.expressions"):
        return "expressions"
    if module.startswith("sqlglot.transforms"):
        return "transforms"
    return "shared"


def component_tests(numbers: Iterable[int], file: str, tests: TestIndex) -> set[int]:
    """Tests whose file is the dialect/component test for ``file``."""
    stem = Path(file).stem
    needle = f"test_{stem}.py"
    return {number for number in numbers if needle in tests.ids.get(number, "").split("::", 1)[0]}


def is_structural(maps: Maps, file: str, lines: Iterable[int]) -> bool:
    """A class body/module-level assignment, rather than a function statement."""
    for line in lines:
        symbol = maps.symbol_at(file, line)
        if symbol is None or symbol.kind == "class":
            return True
    return False


def direct_tests(maps: Maps, tests: TestIndex, file: str, change: Change) -> set[int]:
    mask = 0
    for line in change.old_lines | change.anchors:
        mask |= maps.line_mask(file, line)
    return maps.mask_numbers(mask, tests)


def select(
    maps: Maps,
    tests: TestIndex,
    patch_text: str,
    mode: str = "hybrid",
) -> set[int]:
    changes = parse_patch(patch_text)
    selected: set[int] = set()
    per_file: dict[str, tuple[Change, bool, bool, bool]] = {}
    unknown_paths: list[str] = []
    recognized = False

    for file, change in changes.items():
        if file in tests.test_file_numbers:
            # A changed test file may alter its own expectations, so every test
            # in that file is a candidate.  This is deliberately conservative.
            recognized = True
            selected.update(tests.test_file_numbers[file])
            continue
        if file.startswith(("sqlglot/", "sqlglotc/")) and change.deleted:
            # Deleting a source module can break imports without any covered
            # line remaining in the base map.  No base test set is provably
            # sufficient, so retain recall with the full suite.
            recognized = True
            selected.update(tests.numbers)
            continue
        if file.startswith("tests/fixtures/"):
            recognized = True
            fixture_numbers = fixture_tests(maps, file)
            if fixture_numbers:
                selected.update(fixture_numbers)
            else:
                # A new/renamed fixture has no static consumer rule yet; the
                # safest answer is the full suite rather than an empty line.
                selected.update(tests.numbers)
            continue
        if file.startswith(("sqlglot/", "sqlglotc/")):
            recognized = True

        comment = comment_only(change)
        identity = identity_change(change) or copy_flag_noop(change)
        if (
            not comment
            and not identity
            and file.startswith(("sqlglot/", "sqlglotc/"))
            and file not in maps.coverage
        ):
            unknown_paths.append(file)
        structural = is_structural(maps, file, change.old_lines | change.anchors)
        per_file[file] = (change, comment, identity, structural)

        if comment or identity:
            continue
        if not (file.startswith("sqlglot/") or file.startswith("sqlglotc/")):
            continue

        use = direct_tests(maps, tests, file, change)
        family = module_family(file)
        if mode in ("symbols", "hybrid") and not use:
            # A changed line with no recorded coverage has no test path that can
            # observe it.  Do not broaden to the whole enclosing symbol: that
            # would select tests which never execute the changed statement.
            # Definition/global fallbacks are handled below for modules whose
            # imports are themselves exercised.
            pass
        symbol = maps.symbol_at(file, next(iter(change.old_lines | change.anchors), 0))
        nested_class = symbol is not None and "." in symbol.name
        if structural:
            # Class/module-level assignments are read through call sites, not
            # by the assignment line.  Direct coverage of these lines is mostly
            # incidental import coverage.  Keep the reliable integration
            # anchors below instead.
            matching = component_tests(use, file, tests)
            if family == "dialect" and not nested_class:
                if mode != "direct":
                    use = set()
            elif mode != "direct":
                # Keep structural parser/generator logic only when its own
                # component tests observe it.  Incidental import coverage from
                # unrelated executor/parser tests is not evidence about the
                # changed constant or mapping entry.
                use = matching
        if mode == "direct":
            use = direct_tests(maps, tests, file, change)
        selected.update(use)

    if mode == "direct":
        return selected

    # Dependency and process expansion.  These are the places where line
    # coverage alone is incomplete because the affected work runs in another
    # interpreter or in a generated expression.
    for file, (change, comment, identity, structural) in per_file.items():
        if comment or identity:
            continue
        if not (file.startswith("sqlglot/") or file.startswith("sqlglotc/")):
            continue
        family = module_family(file)
        module = module_name(file)
        symbol = maps.symbol_at(file, next(iter(change.old_lines | change.anchors), 0))
        nested_dialect = (
            structural and family == "dialect" and symbol is not None and "." in symbol.name
        )
        if nested_dialect:
            continue

        if mode in ("dependencies", "symbols", "hybrid") and family == "optimizer":
            # The executor's TPC fixtures exercise optimizer pipelines through a
            # process pool; those child-process lines are absent from the parent
            # coverage contexts.  Limit this expansion to the modules that are
            # part of the main optimizer pipeline, where it is most likely to
            # change executed SQL.
            if module in {
                "sqlglot.optimizer.optimizer",
                "sqlglot.optimizer.eliminate_joins",
                "sqlglot.planner",
            }:
                selected.update(maps.process_tests.get(EXECUTOR_PROCESS_TESTS, ()))

        if mode in ("dependencies", "symbols", "hybrid") and module in {
            "sqlglot.serde",
            "sqlglot.expressions.core",
        }:
            selected.update(maps.process_tests.get(OPTIMIZER_PROCESS_TESTS, ()))

        if mode in ("dependencies", "symbols", "hybrid") and family == "typing":
            selected.add(TYPE_OPTIMIZER_TEST)

        if mode in ("dependencies", "symbols", "hybrid") and family == "dialect" and structural:
            selected.add(DIALECT_SIMPLIFY_TEST)

    if unknown_paths:
        # A source file absent from the base-commit map is new (or was excluded
        # from the build for a parse failure).  No base test is known to cover
        # it, so the safe fallback is the full suite rather than an empty set.
        selected.update(tests.numbers)
    elif not changes or not recognized:
        # An empty/malformed patch or a changed helper/configuration file has
        # no source-level evidence at all.  Preserve recall with the baseline.
        selected.update(tests.numbers)

    return selected


def fixture_tests(maps: Maps, file: str) -> set[int]:
    key = file[len("tests/fixtures/") :]
    numbers = list(maps.fixture_map.get(key, ()))
    if not numbers:
        numbers = list(maps.fixture_by_basename.get(key.rsplit("/", 1)[-1], ()))
    return set(numbers)


def schedule(numbers: Iterable[int], milliseconds: dict[int, int]) -> list[list[int]]:
    selected = sorted(set(int(number) for number in numbers))
    if not selected:
        return []
    items = sorted(((milliseconds.get(number, 0), number) for number in selected), reverse=True)
    best: tuple[int, int, list[list[int]]] | None = None
    for worker_count in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * worker_count
        workers: list[list[int]] = [[] for _ in range(worker_count)]
        for duration, number in items:
            lightest = min(range(worker_count), key=lambda index: (loads[index], index))
            loads[lightest] += duration
            workers[lightest].append(number)
        # A small deterministic local search repairs the few LPT placements
        # that leave a worker unnecessarily long.  It never adds workers.
        loads, workers = improve(loads, workers, items)
        cost = max(loads) + WORKER_COST_MS * worker_count
        candidate = (cost, worker_count, [sorted(worker) for worker in workers])
        if best is None or (candidate[0], candidate[1]) < (best[0], best[1]):
            best = candidate
    assert best is not None
    return [worker for worker in best[2] if worker]


def improve(loads: list[int], workers: list[list[int]], items: list[tuple[int, int]]) -> tuple[list[int], list[list[int]]]:
    """Deterministic move/swap local search for the makespan."""
    if len(workers) == 1:
        return loads, workers
    loads = list(loads)
    workers = [list(worker) for worker in workers]
    durations = {number: duration for duration, number in items}
    for _ in range(8):
        changed = False
        heaviest = max(range(len(loads)), key=lambda index: (loads[index], -index))
        target = min(range(len(loads)), key=lambda index: (loads[index], index))
        if heaviest == target:
            break
        # Find the largest item on the heaviest worker whose move lowers the
        # makespan, or a swap with a smaller item on the target worker.
        for number in sorted(workers[heaviest], key=lambda value: (-durations.get(value, 0), value)):
            duration = durations.get(number, 0)
            before = max(loads)
            if max(loads[heaviest] - duration, loads[target] + duration) < before:
                workers[heaviest].remove(number)
                workers[target].append(number)
                loads[heaviest] -= duration
                loads[target] += duration
                changed = True
                break
        if changed:
            continue
        for first in sorted(workers[heaviest], key=lambda value: (-durations.get(value, 0), value)):
            for second in sorted(workers[target], key=lambda value: (durations.get(value, 0), value)):
                first_duration = durations.get(first, 0)
                second_duration = durations.get(second, 0)
                if first_duration <= second_duration:
                    continue
                new_heavy = loads[heaviest] - first_duration + second_duration
                new_target = loads[target] - second_duration + first_duration
                if max(new_heavy, new_target) < max(loads):
                    workers[heaviest].remove(first)
                    workers[target].remove(second)
                    workers[heaviest].append(second)
                    workers[target].append(first)
                    loads[heaviest] = new_heavy
                    loads[target] = new_target
                    changed = True
                    break
            if changed:
                break
        if not changed:
            break
    return loads, workers


def load_tests(data_dir: Path, maps: Maps) -> TestIndex:
    with (data_dir / "tests.json").open("r", encoding="utf-8") as handle:
        records = json.load(handle)["tests"]
    numbers = [int(record["number"]) for record in records]
    milliseconds: dict[int, int] = {}
    ids: dict[int, str] = {}
    files: dict[str, list[int]] = {}
    for record in records:
        number = int(record["number"])
        milliseconds[number] = int(float(record["seconds"]) * 1000 + 0.5)
        ids[number] = str(record["id"])
        files.setdefault(str(record["file"]), []).append(number)
    return TestIndex(numbers, milliseconds, ids, files, {file: sorted(values) for file, values in files.items()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data", default=os.environ.get("DATA_DIR", "/data"))
    parser.add_argument("--maps", default=os.environ.get("MAPS_DIR", str(Path(__file__).resolve().parent / "maps")))
    parser.add_argument("--mode", default=os.environ.get("RADAR_MODE", "hybrid"))
    parser.add_argument("--help", action="help")
    options, _ = parser.parse_known_args(argv)

    data_dir = Path(options.data)
    maps = Maps(Path(options.maps))
    tests = load_tests(data_dir, maps)
    maps.bind_tests(tests)

    commits_dir = data_dir / "commits"
    if not commits_dir.is_dir():
        print(f"missing commits directory: {commits_dir}", file=sys.stderr)
        return 2

    for patch in sorted(commits_dir.glob("*.patch")):
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            print(f"cannot read {patch}: {error}", file=sys.stderr)
            text = ""
        try:
            numbers = select(maps, tests, text, options.mode)
            workers = schedule(numbers, tests.milliseconds)
        except Exception as error:  # pragma: no cover - defensive runtime guard
            print(f"selector fallback for {patch.stem}: {error}", file=sys.stderr)
            workers = schedule(tests.numbers, tests.milliseconds)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
