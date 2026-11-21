#!/usr/bin/env python3
"""Build the committed, patch-independent analysis maps.

This is a development/build tool.  It is not imported by the runtime selector.
Inputs are the pinned sqlglot checkout, tests.json and one or more coverage data
files produced by ``coverage run`` with ``dynamic_context = test_function``.
"""

from __future__ import annotations

import argparse
import ast
import base64
import gzip
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from coverage import CoverageData

SOURCE_PREFIXES = ("sqlglot/", "sqlglotc/")
FIXTURE_PREFIX = "tests/fixtures/"


def canonical_test_id(test_id: str) -> str:
    """coverage.py's dotted context spelling for a pytest node id."""
    return test_id.replace("/", ".").replace(".py::", ".").replace("::", ".")


def encode_mask(mask: int) -> str:
    if not mask:
        return ""
    size = max(1, (mask.bit_length() + 7) // 8)
    return base64.b64encode(mask.to_bytes(size, "little")).decode("ascii")


def mask_for_numbers(numbers: Iterable[int]) -> int:
    mask = 0
    for number in numbers:
        mask |= 1 << number
    return mask


def rel_source(path: str, root: Path) -> str | None:
    """Normalise a coverage path to a portable path relative to the checkout."""
    try:
        relative = Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        text = path.replace("\\", "/")
        for prefix in SOURCE_PREFIXES:
            marker = "/" + prefix
            if marker in text:
                return prefix + text.split(marker, 1)[1]
        return None
    text = relative.as_posix()
    return text if text.startswith(SOURCE_PREFIXES) else None


def write_gzip_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            zipped.write(payload)


def load_tests(path: Path) -> list[dict[str, Any]]:
    tests = json.loads(path.read_text(encoding="utf-8"))["tests"]
    expected = list(range(len(tests)))
    numbers = [test["number"] for test in tests]
    if numbers != expected:
        raise SystemExit("tests.json numbers are not contiguous from zero")
    seen: set[str] = set()
    for test in tests:
        test_id = test["id"]
        if test_id in seen:
            raise SystemExit(f"duplicate test id: {test_id}")
        seen.add(test_id)
    return tests


def merge_coverage(files: list[Path]) -> CoverageData:
    merged = CoverageData()
    for path in files:
        if not path.exists():
            continue
        data = CoverageData(basename=str(path))
        data.read()
        merged.update(data)
    return merged


def build_coverage(
    coverage: CoverageData,
    root: Path,
    tests: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    number_by_context = {canonical_test_id(test["id"]): test["number"] for test in tests}
    # Coverage can encode a few unusual parameter ids differently.  Add the
    # common dotted spellings without ever inventing a new test number.
    by_file: dict[str, dict[int, int]] = defaultdict(dict)
    for measured_file in coverage.measured_files():
        relative = rel_source(measured_file, root)
        if not relative:
            continue
        contexts_by_line = coverage.contexts_by_lineno(measured_file)
        for line, contexts in contexts_by_line.items():
            mask = 0
            for context in contexts:
                number = number_by_context.get(context)
                if number is not None:
                    mask |= 1 << number
            if mask:
                by_file[relative][line] = by_file[relative].get(line, 0) | mask

    files: dict[str, dict[str, Any]] = {}
    for relative, lines in sorted(by_file.items()):
        path = root / relative
        symbols = build_symbols(path, relative, lines)
        all_mask = 0
        for mask in lines.values():
            all_mask |= mask
        files[relative] = {
            "lines": {str(line): encode_mask(mask) for line, mask in sorted(lines.items())},
            "mask": encode_mask(all_mask),
            "symbols": symbols,
        }

    # Every source file is useful to the selector even if coverage measured no
    # line for it; keep its AST symbols and an empty line map.
    source_files = set()
    for path in sorted(root.glob("sqlglot/**/*.py")):
        source_files.add(path.relative_to(root).as_posix())
    for path in sorted(root.glob("sqlglotc/**/*.py")):
        source_files.add(path.relative_to(root).as_posix())
    for relative in sorted(source_files):
        if relative not in files:
            path = root / relative
            files[relative] = {
                "lines": {},
                "mask": "",
                "symbols": build_symbols(path, relative, {}),
            }
    return files


def build_symbols(
    path: Path,
    relative: str,
    line_masks: dict[int, int],
) -> list[list[Any]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return []

    symbols: list[list[Any]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = getattr(child, "lineno", 0) or 0
                end = getattr(child, "end_lineno", start) or start
                mask = 0
                for line in range(start, end + 1):
                    mask |= line_masks.get(line, 0)
                kind = "class" if isinstance(child, ast.ClassDef) else "function"
                symbols.append([name, kind, start, end, encode_mask(mask)])
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    symbols.sort(key=lambda item: (item[2], item[3], item[0]))
    return symbols


def module_name(relative: str) -> str:
    text = relative[:-3].replace("/", ".")
    if text.endswith(".__init__"):
        text = text[: -len(".__init__")]
    return text


def resolve_imports(relative: str, module: str, tree: ast.AST, modules: set[str]) -> set[str]:
    dependencies: set[str] = set()

    def add(candidate: str) -> None:
        if candidate in modules:
            dependencies.add(candidate)

    package_parts = module.split(".")
    if relative.endswith("/__init__.py"):
        package = module
    else:
        package_parts = package_parts[:-1]
        package = ".".join(package_parts)

    def from_base(node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = package_parts[:]
        if node.level > 1:
            parts = parts[: max(0, len(parts) - (node.level - 1))]
        base = ".".join(parts)
        if node.module:
            base = f"{base}.{node.module}" if base else node.module
        return base

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = from_base(node)
            add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}" if base else alias.name
                add(candidate)
                # A class imported from a package is usually defined in a
                # snake_case module of that package.
                if base in {"sqlglot.dialects", "sqlglot.parsers", "sqlglot.generators"}:
                    add(f"{base}.{alias.name.lower()}")
                if base == "sqlglot":
                    aliases = {
                        "exp": "sqlglot.expressions",
                        "tokens": "sqlglot.tokens",
                        "parser": "sqlglot.parser",
                        "generator": "sqlglot.generator",
                        "error": "sqlglot.errors",
                        "errors": "sqlglot.errors",
                        "optimizer": "sqlglot.optimizer",
                        "schema": "sqlglot.schema",
                        "planner": "sqlglot.planner",
                    }
                    if alias.name in aliases:
                        add(aliases[alias.name])
    return dependencies


def build_dependencies(root: Path, tests: list[dict[str, Any]]) -> dict[str, Any]:
    source_paths: list[Path] = []
    for pattern in ("sqlglot/**/*.py", "sqlglotc/**/*.py"):
        source_paths.extend(root.glob(pattern))
    module_file: dict[str, str] = {}
    module_trees: dict[str, ast.AST] = {}
    for path in sorted(source_paths):
        relative = path.relative_to(root).as_posix()
        module = module_name(relative)
        module_file[module] = relative
        try:
            module_trees[module] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue

    modules = set(module_file)
    dependencies: dict[str, list[str]] = {}
    for module, tree in module_trees.items():
        dependencies[module] = sorted(resolve_imports(module_file[module], module, tree, modules))

    test_files = sorted({test["file"] for test in tests})
    test_imports: dict[str, list[str]] = {}
    for relative in test_files:
        path = root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            test_imports[relative] = []
            continue
        # Test modules are not part of the source module map, but imports such
        # as `tests.helpers` still carry useful dependency information.  The
        # source-module resolver only retains names present in `modules`.
        fake_module = relative[:-3].replace("/", ".")
        test_imports[relative] = sorted(
            resolve_imports(relative, fake_module, tree, modules)
        )

    return {
        "modules": dependencies,
        "module_file": module_file,
        "test_imports": test_imports,
        "process_tests": build_process_tests(root, tests),
    }


def build_process_tests(root: Path, tests: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Tests whose own body uses a process pool (directly or via a helper)."""
    result: dict[str, list[int]] = {}
    for relative in sorted({test["file"] for test in tests}):
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        methods = test_method_map(tests, relative)
        numbers: list[int] = []
        for class_node in [node for node in tree.body if isinstance(node, ast.ClassDef)]:
            for child in class_node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not child.name.startswith("test_"):
                    continue
                source = ast.get_source_segment(text, child) or ""
                if any(
                    marker in source
                    for marker in ("check_file", "self.pool", "_mp_execute", "ProcessPoolExecutor")
                ):
                    number = methods.get((class_node.name, child.name))
                    if number is not None:
                        numbers.append(number)
        for child in tree.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"):
                source = ast.get_source_segment(text, child) or ""
                if any(
                    marker in source
                    for marker in ("check_file", "self.pool", "_mp_execute", "ProcessPoolExecutor")
                ):
                    number = methods.get(("", child.name))
                    if number is not None:
                        numbers.append(number)
        if numbers:
            result[relative] = sorted(set(numbers))
    return result


def test_method_map(tests: list[dict[str, Any]], relative: str) -> dict[tuple[str, str], int]:
    result: dict[tuple[str, str], int] = {}
    for test in tests:
        if test["file"] != relative:
            continue
        parts = test["id"].split("::")
        if len(parts) >= 3:
            key = (parts[-2], parts[-1])
        elif len(parts) == 2:
            key = ("", parts[-1])
        else:
            continue
        result[key] = test["number"]
    return result


def literal_strings(node: ast.AST) -> list[str]:
    values: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
    return values


def fixture_candidates(value: str, fixture_names: set[str]) -> set[str]:
    value = value.strip().replace("\\", "/")
    if not value or len(value) > 300:
        return set()
    candidates: set[str] = set()
    if value in fixture_names:
        candidates.add(value)
    if value.startswith("tests/fixtures/"):
        stripped = value[len("tests/fixtures/") :]
        if stripped in fixture_names:
            candidates.add(stripped)
    if value.endswith((".sql", ".json", ".csv", ".csv.gz")) and value in fixture_names:
        candidates.add(value)
    base = value.rsplit("/", 1)[-1]
    if base in fixture_names:
        candidates.add(base)
    # Fixture helpers are called with names such as "optimizer/simplify" or
    # "simplify"; the actual files have a .sql suffix.
    for root in ("optimizer/", ""):
        for suffix in ("", ".sql"):
            candidate = f"{root}{value}{suffix}"
            if candidate in fixture_names:
                candidates.add(candidate)
    return candidates


def build_fixtures(root: Path, tests: list[dict[str, Any]]) -> dict[str, Any]:
    fixtures_dir = root / "tests" / "fixtures"
    fixture_names = {
        path.relative_to(fixtures_dir).as_posix()
        for path in fixtures_dir.rglob("*")
        if path.is_file()
    }
    by_fixture: dict[str, set[int]] = defaultdict(set)

    def add_fixture(fixture: str, numbers: Iterable[int]) -> None:
        for number in numbers:
            by_fixture[fixture].add(number)
        # A directory-level change (for example a TPC-H CSV fixture) is
        # relevant to every test that loads a file from that directory.
        parent = fixture
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in {"optimizer/tpc-h", "optimizer/tpc-ds"}:
                for number in numbers:
                    by_fixture[parent].add(number)

    def scope_nodes(node: ast.AST) -> Iterable[ast.AST]:
        """Nodes belonging to this scope, not nested function/class bodies."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield child
            yield from scope_nodes(child)

    def scan_scope(scope: ast.AST, numbers: list[int]) -> None:
        if not numbers:
            return
        values = [
            child.value
            for child in scope_nodes(scope)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        ]
        for value in values:
            for fixture in fixture_candidates(value, fixture_names):
                add_fixture(fixture, numbers)
        for child in scope_nodes(scope):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in {"check_file", "load_sql_fixture_pairs", "load_sql_fixtures"}:
                continue
            if not child.args:
                continue
            for arg in child.args[:1]:
                for value in literal_strings(arg)[:1]:
                    for fixture in fixture_candidates(value, fixture_names):
                        add_fixture(fixture, numbers)

    for relative in sorted({test["file"] for test in tests}):
        path = root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        methods = test_method_map(tests, relative)
        class_tests: dict[str, list[int]] = defaultdict(list)
        for (class_name, method_name), number in methods.items():
            class_tests[class_name].append(number)
        all_file_tests = [test["number"] for test in tests if test["file"] == relative]

        # Module-level fixture loads are conservatively attributed to every
        # test in the file, but nested test bodies are handled separately.
        scan_scope(tree, all_file_tests)

        def visit_class(node: ast.ClassDef) -> None:
            numbers = class_tests.get(node.name, [])
            scan_scope(node, numbers)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("test_"):
                        number = methods.get((node.name, child.name))
                        scan_scope(child, [number] if number is not None else [])
                    else:
                        # setUpClass, helpers, or other class-level methods may
                        # be used by every test in the class.
                        scan_scope(child, numbers)
                elif isinstance(child, ast.ClassDef):
                    visit_class(child)

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                visit_class(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                number = methods.get(("", node.name))
                scan_scope(node, [number] if number is not None else [])

    return {
        "by_fixture": {key: sorted(value) for key, value in sorted(by_fixture.items())},
        "fixture_names": sorted(fixture_names),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="pinned sqlglot checkout")
    parser.add_argument("--tests", type=Path, required=True, help="tests.json")
    parser.add_argument("--out", type=Path, required=True, help="maps directory")
    parser.add_argument("--coverage", type=Path, action="append", default=[])
    parser.add_argument("--base-commit", default="eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    tests = load_tests(args.tests)
    coverage = merge_coverage(args.coverage)
    coverage_files = build_coverage(coverage, root, tests)
    dependencies = build_dependencies(root, tests)
    fixtures = build_fixtures(root, tests)

    test_file_numbers: dict[str, list[int]] = defaultdict(list)
    for test in tests:
        test_file_numbers[test["file"]].append(test["number"])

    args.out.mkdir(parents=True, exist_ok=True)
    write_gzip_json(args.out / "coverage.json.gz", {"files": coverage_files})
    write_gzip_json(args.out / "dependencies.json.gz", dependencies)
    write_gzip_json(args.out / "fixtures.json.gz", fixtures)
    write_gzip_json(
        args.out / "tests.json.gz",
        {
            "tests": [
                {
                    "n": test["number"],
                    "id": test["id"],
                    "file": test["file"],
                    "ms": int(test["seconds"] * 1000 + 0.5),
                }
                for test in tests
            ],
            "test_file_numbers": dict(sorted(test_file_numbers.items())),
        },
    )
    manifest = {
        "schema": 1,
        "base_commit": args.base_commit,
        "tests": len(tests),
        "source_files": len(coverage_files),
        "covered_lines": sum(len(value["lines"]) for value in coverage_files.values()),
        "fixture_rules": len(fixtures["by_fixture"]),
        "coverage_contexts": len(
            {context for context in coverage.measured_contexts() if context}
        ),
    }
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
