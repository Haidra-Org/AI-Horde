# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gate a Locust *smoke* run on stability, not performance.

The stress suite reports operational responses (HTTP 429 rate limits, worker
contention rcs, deliberate misuse 4xx) as *successes*. A recorded Locust
failure is therefore either:

* a **crash signal** -- a 5xx response, or a transport-level exception
  (connection reset, read timeout, remote disconnect) with no HTTP status, or
* a **benign client rejection** -- a 4xx the suite did not pre-classify (for
  example the demand/kudos threshold 403 returned for large anonymous image
  requests). These are normal API behaviour, not a server fault.

This checker reads the ``--csv`` output produced by ``locust ... --csv <prefix>``
and **fails only on crash signals**. It deliberately asserts nothing about
latency or throughput -- that is the job of the baselines in
``tests/stress/BASELINE.md``, gated manually. Benign 4xx failures are reported
as warnings so regressions in request shaping stay visible without flaking the
smoke gate.

The run also fails if it drove no requests at all (the workload never reached
the target server).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

_AGGREGATED_ROW_NAME = "Aggregated"
_STATUS_RE = re.compile(r"Status (\d{3})")


def _read_aggregated(stats_path: Path) -> dict[str, str]:
    """Return the ``Aggregated`` summary row from a Locust ``*_stats.csv``."""
    with stats_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("Name") == _AGGREGATED_ROW_NAME:
            return row
    raise SystemExit(f"No '{_AGGREGATED_ROW_NAME}' row found in {stats_path}; did Locust write CSV output?")


def _classify_failures(failures_path: Path) -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str, str, int]]]:
    """Split recorded failures into ``(crash, client)`` buckets.

    A failure is a *crash* signal when its error carries a 5xx HTTP status or
    no HTTP status at all (a transport-level exception). A failure is a *client*
    rejection when it carries a 4xx HTTP status.

    Returns:
        A ``(crash, client)`` tuple of lists. Each entry is
        ``(method, name, error, occurrences)``.
    """
    crash: list[tuple[str, str, str, int]] = []
    client: list[tuple[str, str, str, int]] = []
    if not failures_path.is_file():
        return crash, client
    with failures_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            error = row.get("Error", "") or ""
            method = row.get("Method", "") or ""
            name = row.get("Name", "") or ""
            occurrences = int(row.get("Occurrences", "0") or "0")
            match = _STATUS_RE.search(error)
            entry = (method, name, error, occurrences)
            if match is not None and 400 <= int(match.group(1)) <= 499:
                client.append(entry)
            else:
                crash.append(entry)
    return crash, client


def _print_bucket(label: str, bucket: list[tuple[str, str, str, int]]) -> int:
    """Print a failure bucket to stderr and return its total occurrence count."""
    total = sum(occurrences for *_rest, occurrences in bucket)
    if not bucket:
        return 0
    print(f"--- {label} ({total} occurrence(s)) ---", file=sys.stderr)
    for method, name, error, occurrences in bucket:
        print(f"  {occurrences:>4}x {method} {name}: {error}", file=sys.stderr)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate a Locust smoke run on crash-class failures only.")
    parser.add_argument("--stats", required=True, type=Path, help="Path to the Locust <prefix>_stats.csv file.")
    parser.add_argument(
        "--failures",
        type=Path,
        default=None,
        help="Path to the <prefix>_failures.csv file (defaults to <stats-prefix>_failures.csv).",
    )
    args = parser.parse_args(argv)

    if not args.stats.is_file():
        raise SystemExit(f"Stats file not found: {args.stats}")

    failures_path: Path | None = args.failures
    if failures_path is None:
        # locust writes "<prefix>_stats.csv" and "<prefix>_failures.csv" together.
        failures_path = Path(re.sub(r"_stats\.csv$", "_failures.csv", str(args.stats)))

    aggregated = _read_aggregated(args.stats)
    request_count = int(aggregated.get("Request Count", "0") or "0")
    failure_count = int(aggregated.get("Failure Count", "0") or "0")

    print(f"Locust smoke: {request_count} requests, {failure_count} recorded failures.")

    if request_count <= 0:
        raise SystemExit("Smoke run drove zero requests; the workload never reached the target.")

    crash, client = _classify_failures(failures_path)
    client_total = _print_bucket("benign client (4xx) failures [non-gating]", client)
    crash_total = _print_bucket("CRASH-class failures (5xx / transport errors)", crash)

    if client_total:
        print(f"::warning::Locust smoke saw {client_total} benign client (4xx) failure(s); not gating.")

    if crash_total:
        raise SystemExit(
            f"Smoke run recorded {crash_total} crash-class failure(s) (5xx or transport-level errors); see above.",
        )

    print("Smoke run passed: no 5xx or transport-level failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
