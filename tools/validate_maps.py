#!/usr/bin/env python3
"""Validate the committed maps without sqlglot, pytest or network access."""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import selector  # noqa: E402


def load(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--maps", type=Path, default=ROOT / "maps")
    parser.add_argument("--root", type=Path, help="optional pinned sqlglot checkout")
    options = parser.parse_args()

    errors: list[str] = []
    maps = selector.Maps(options.maps)
    tests = selector.load_tests(options.data, maps)
    maps.bind_tests(tests)

    if len(tests.numbers) != len(set(tests.numbers)):
        errors.append("duplicate test numbers")
    if set(tests.numbers) != set(range(len(tests.numbers))):
        errors.append("test numbers are not contiguous from zero")
    if len(tests.milliseconds) != len(tests.numbers):
        errors.append("missing runtimes")
    if any(value < 0 for value in tests.milliseconds.values()):
        errors.append("negative runtime")

    coverage = load(options.maps / "coverage.json.gz").get("files", {})
    for file, data in coverage.items():
        if not isinstance(file, str) or file.startswith(("/", "\\")) or ".." in Path(file).parts:
            errors.append(f"non-portable source path: {file!r}")
            continue
        if options.root and not (options.root / file).is_file():
            errors.append(f"source path absent from checkout: {file}")
        for line_text, encoded in data.get("lines", {}).items():
            try:
                line = int(line_text)
                mask = int.from_bytes(base64.b64decode(encoded), "little") if encoded else 0
            except (ValueError, TypeError):
                errors.append(f"invalid line/mask {file}:{line_text}")
                continue
            if line <= 0 or mask.bit_length() > len(tests.numbers):
                errors.append(f"out-of-range mask {file}:{line}")
            if mask & ~((1 << len(tests.numbers)) - 1):
                errors.append(f"test bit outside index {file}:{line}")
        for symbol in data.get("symbols", []):
            if len(symbol) < 5:
                errors.append(f"short symbol record in {file}")
                continue
            name, kind, start, end, encoded = symbol
            if kind not in {"class", "function"} or start <= 0 or end < start:
                errors.append(f"invalid symbol {file}:{name}")
            try:
                int.from_bytes(base64.b64decode(encoded), "little")
            except (ValueError, TypeError):
                errors.append(f"invalid symbol mask {file}:{name}")

    fixtures = load(options.maps / "fixtures.json.gz")
    for key, numbers in fixtures.get("by_fixture", {}).items():
        if not isinstance(key, str) or key.startswith(("/", "\\")):
            errors.append(f"non-portable fixture key: {key!r}")
        if any(number not in tests.milliseconds for number in numbers):
            errors.append(f"unknown fixture test number: {key}")

    dependencies = load(options.maps / "dependencies.json.gz")
    for file, numbers in dependencies.get("process_tests", {}).items():
        if file not in tests.test_file_numbers:
            errors.append(f"unknown process-test file: {file}")
        if any(number not in tests.milliseconds for number in numbers):
            errors.append(f"unknown process test number: {file}")

    if options.root:
        for file in coverage:
            if not (options.root / file).is_file():
                errors.append(f"missing checkout file: {file}")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"FAILED: {len(errors)} map errors", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "tests": len(tests.numbers),
                "source_files": len(coverage),
                "fixture_rules": len(fixtures.get("by_fixture", {})),
                "process_test_files": len(dependencies.get("process_tests", {})),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
