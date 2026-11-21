#!/usr/bin/env python3
"""Evaluate the selector on the supplied sample labels.

The labels are used here only to measure candidate quality.  The runtime
selector never reads answers.json and never imports this module.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import selector  # noqa: E402


CANDIDATES = {
    "A-direct": "direct",
    "B-dependencies": "dependencies",
    "C-symbols": "symbols",
    "D-hybrid": "hybrid",
}


def score_selection(
    numbers: set[int],
    failing: set[int],
    tests: selector.TestIndex,
) -> int:
    workers = selector.schedule(numbers, tests.milliseconds)
    if not workers:
        simulated = 0
    else:
        loads = [
            sum(tests.milliseconds[number] for number in worker) for worker in workers
        ]
        simulated = max(loads) + selector.WORKER_COST_MS * len(workers)
    if failing - numbers:
        simulated += 300_000
    return simulated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "sample")
    parser.add_argument("--maps", type=Path, default=ROOT / "maps")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()

    maps = selector.Maps(options.maps)
    tests = selector.load_tests(options.data, maps)
    maps.bind_tests(tests)
    answers = json.loads((options.data / "answers.json").read_text(encoding="utf-8"))["commits"]
    patches = sorted((options.data / "commits").glob("*.patch"))

    results = []
    for label, mode in CANDIDATES.items():
        started = time.process_time()
        simulated = 0
        selected_total = 0
        extra_total = 0
        missed = 0
        per_commit = []
        for patch in patches:
            numbers = selector.select(
                maps, tests, patch.read_text(encoding="utf-8", errors="replace"), mode
            )
            failing = set(int(number) for number in answers[patch.stem]["failing"])
            commit_score = score_selection(numbers, failing, tests)
            simulated += commit_score
            selected_total += len(numbers)
            extra_total += len(numbers - failing)
            missed += 1 if failing - numbers else 0
            per_commit.append(
                {
                    "commit": patch.stem,
                    "selected": len(numbers),
                    "extra": len(numbers - failing),
                    "missed": sorted(failing - numbers),
                    "simulated_ms": commit_score,
                }
            )
        elapsed = time.process_time() - started
        results.append(
            {
                "candidate": label,
                "mode": mode,
                "simulated_ms": simulated,
                "selected_tests": selected_total,
                "extra_tests": extra_total,
                "missed_commits": missed,
                "cpu_seconds_sample": round(elapsed, 6),
                "per_commit": per_commit,
            }
        )

    if options.json:
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print("candidate            simulated_ms  selected  extra  missed  cpu_s")
    for result in results:
        print(
            f"{result['candidate']:<20}"
            f"{result['simulated_ms']:>13}"
            f"{result['selected_tests']:>10}"
            f"{result['extra_tests']:>7}"
            f"{result['missed_commits']:>8}"
            f"{result['cpu_seconds_sample']:>8.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
