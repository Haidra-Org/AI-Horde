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

A third kind is a **contract violation**: a chat mix user (``locustsuite/chat_*_client.py``, ``chat_worker_user.py``)
found that a response breaks the chat contract its scenario expects, such as a
stream without ``[DONE]``, a reply that ignores the requested JSON schema, or an
expected 404 that came back as a 400. Its message contains
``CONTRACT_VIOLATION_PREFIX`` whatever HTTP status the response had, and it gates
the run like a crash signal, since a 4xx where the contract promises a reply is a
regression rather than normal API behaviour.

A fourth kind is an **environment condition**: a chat mix user met a condition of
the deployment, not of the contract (chat admission off, maintenance, an
unreachable Horde, a 408 at the front's deadline, an address in timeout, a proxy
error page). Its message contains ``ENVIRONMENT_CONDITION_PREFIX``. It is reported
in its own section and gates the run, since a run that hit one did not exercise
what it meant to, but it is labelled apart so nobody chases it as a regression.

This checker reads the ``--csv`` output produced by ``locust ... --csv <prefix>``
and **fails only on crash signals, contract violations and environment
conditions**. It deliberately asserts nothing about latency or throughput --
that is the job of the baselines in ``tests/stress/BASELINE.md``, gated manually. Benign 4xx failures are reported
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
from dataclasses import dataclass, field
from pathlib import Path

_AGGREGATED_ROW_NAME = "Aggregated"
_STATUS_RE = re.compile(r"Status (\d{3})")

CONTRACT_VIOLATION_PREFIX = "Contract violation"
"""The text a chat contract failure carries. LOG CONTRACT: ``tests/stress/locustsuite/chat_checks.py`` defines the same
``CONTRACT_VIOLATION_PREFIX``; change both together. Locust writes a failure's exception ``repr``, so the text is matched
anywhere in the error, not at its start."""

ENVIRONMENT_CONDITION_PREFIX = "Environment condition"
"""The text a chat environment condition carries. LOG CONTRACT: ``tests/stress/locustsuite/chat_checks.py`` defines the
same ``ENVIRONMENT_CONDITION_PREFIX``; change both together. Matched anywhere in the error, as above."""

FailureEntry = tuple[str, str, str, int]
"""One failures CSV row: method, name, error and occurrences."""


@dataclass
class ClassifiedFailures:
    """Represents a run's recorded failures sorted into the checker's buckets."""

    contract: list[FailureEntry] = field(default_factory=list)
    """Chat contract violations, whatever their status; gating."""
    environment: list[FailureEntry] = field(default_factory=list)
    """Deployment conditions a chat user met; gating, reported apart."""
    crash: list[FailureEntry] = field(default_factory=list)
    """5xx responses and transport errors; gating."""
    client: list[FailureEntry] = field(default_factory=list)
    """4xx responses the suite did not classify; reported, not gating."""


def _read_aggregated(stats_path: Path) -> dict[str, str]:
    """Return the ``Aggregated`` summary row from a Locust ``*_stats.csv``."""
    with stats_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("Name") == _AGGREGATED_ROW_NAME:
            return row
    raise SystemExit(f"No '{_AGGREGATED_ROW_NAME}' row found in {stats_path}; did Locust write CSV output?")


def _classify_failures(failures_path: Path) -> ClassifiedFailures:
    """Sort recorded failures into the contract, environment, crash and client buckets.

    A failure is a *contract* violation when its error contains
    ``CONTRACT_VIOLATION_PREFIX``, whatever status it mentions, and an
    *environment* condition when it contains ``ENVIRONMENT_CONDITION_PREFIX``. Otherwise it is
    a *crash* signal when its error carries a 5xx HTTP status or no HTTP status
    at all (a transport-level exception), and a *client* rejection when it
    carries a 4xx HTTP status.

    Returns:
        The buckets; each entry is ``(method, name, error, occurrences)``.
    """
    classified = ClassifiedFailures()
    if not failures_path.is_file():
        return classified
    with failures_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            error = row.get("Error", "") or ""
            method = row.get("Method", "") or ""
            name = row.get("Name", "") or ""
            occurrences = int(row.get("Occurrences", "0") or "0")
            match = _STATUS_RE.search(error)
            entry = (method, name, error, occurrences)
            if CONTRACT_VIOLATION_PREFIX in error:
                classified.contract.append(entry)
            elif ENVIRONMENT_CONDITION_PREFIX in error:
                classified.environment.append(entry)
            elif match is not None and 400 <= int(match.group(1)) <= 499:
                classified.client.append(entry)
            else:
                classified.crash.append(entry)
    return classified


def _print_bucket(label: str, bucket: list[FailureEntry]) -> int:
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

    classified = _classify_failures(failures_path)
    client_total = _print_bucket("benign client (4xx) failures [non-gating]", classified.client)
    contract_total = _print_bucket("CONTRACT violations (chat responses breaking their expected outcome)", classified.contract)
    environment_total = _print_bucket("ENVIRONMENT conditions (deployment not ready for the run)", classified.environment)
    crash_total = _print_bucket("CRASH-class failures (5xx / transport errors)", classified.crash)

    if client_total:
        print(f"::warning::Locust smoke saw {client_total} benign client (4xx) failure(s); not gating.")

    if crash_total or contract_total or environment_total:
        raise SystemExit(
            f"Smoke run recorded {crash_total} crash-class failure(s) (5xx or transport-level errors), "
            f"{contract_total} contract violation(s) and {environment_total} environment condition(s); see above.",
        )

    print("Smoke run passed: no 5xx, transport-level failures, contract violations or environment conditions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
