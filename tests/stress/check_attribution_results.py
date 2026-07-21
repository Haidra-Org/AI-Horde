# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gate an attribution scenario run on oracle-violation evidence.

The attribution Locust scenario records every consistency violation to a JSONL
evidence file (one record per violation) and, redundantly, as a Locust
request-event failure under an ``oracle:*`` name. This checker treats the JSONL
evidence as the authority and supports two modes:

- Default (post-fix gate): the run passes only if it observed zero oracle
  violations. This is the assertion that the fixed server never produces the
  inconsistent responses the scenario probes for.
- ``--expect-violations`` (pre-fix elicitation gate): the run passes only if it
  observed at least one oracle violation. This asserts that the scenario actually
  elicited the defect against a server known to contain it; a run that elicited
  nothing is a harness failure, not a success.

When a Locust stats CSV is provided the checker additionally fails a run that
drove zero requests, so a vacuous "no violations" result cannot pass the post-fix
gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

_AGGREGATED_ROW_NAME = "Aggregated"
_SAMPLE_LIMIT = 5


def _read_evidence(evidence_path: Path) -> list[dict[str, object]]:
    """Return the parsed JSONL violation records, skipping blank lines."""
    if not evidence_path.is_file():
        return []
    records: list[dict[str, object]] = []
    with evidence_path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            records.append(json.loads(stripped))
    return records


def _read_request_count(stats_path: Path) -> int:
    """Return the aggregated request count from a Locust ``*_stats.csv``."""
    with stats_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("Name") == _AGGREGATED_ROW_NAME:
                return int(row.get("Request Count", "0") or "0")
    return 0


def _summarize(records: list[dict[str, object]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        oracle = record.get("oracle")
        counts[str(oracle)] += 1
    return counts


def _print_report(records: list[dict[str, object]], counts: Counter[str]) -> None:
    print(f"Attribution oracle evidence: {len(records)} violation record(s).")
    for oracle, count in sorted(counts.items()):
        print(f"  {count:>5}x {oracle}")
    for oracle in sorted(counts):
        samples = [record for record in records if str(record.get("oracle")) == oracle][:_SAMPLE_LIMIT]
        if not samples:
            continue
        print(f"--- sample evidence for {oracle} (up to {_SAMPLE_LIMIT}) ---", file=sys.stderr)
        for sample in samples:
            print(f"  {json.dumps(sample, sort_keys=True)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate an attribution scenario run on oracle-violation evidence.")
    parser.add_argument("--evidence", required=True, type=Path, help="Path to the JSONL oracle-evidence file.")
    parser.add_argument(
        "--stats",
        type=Path,
        default=None,
        help="Optional Locust <prefix>_stats.csv; when given, a zero-request run fails.",
    )
    parser.add_argument(
        "--expect-violations",
        action="store_true",
        help="Pre-fix mode: require at least one violation (exit nonzero if none were elicited).",
    )
    args = parser.parse_args(argv)

    records = _read_evidence(args.evidence)
    counts = _summarize(records)
    _print_report(records, counts)

    if args.stats is not None:
        if not args.stats.is_file():
            raise SystemExit(f"Stats file not found: {args.stats}")
        request_count = _read_request_count(args.stats)
        print(f"Locust drove {request_count} request(s).")
        if request_count <= 0:
            raise SystemExit("Attribution run drove zero requests; the workload never reached the target.")

    total = len(records)
    if args.expect_violations:
        if total == 0:
            raise SystemExit(
                "Elicitation gate FAILED: expected at least one oracle violation but the run recorded none.",
            )
        print(f"Elicitation gate passed: {total} oracle violation(s) were elicited as expected.")
        return 0

    if total > 0:
        raise SystemExit(f"Consistency gate FAILED: the run recorded {total} oracle violation(s); see evidence above.")
    print("Consistency gate passed: no oracle violations were recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
