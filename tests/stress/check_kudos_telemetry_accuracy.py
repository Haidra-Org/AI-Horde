# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gate the applier ``folded`` counter against database truth.

The applier emits an OTLP counter (``horde.kudos.applier.folded``) with a
``row_type`` attribute of ``currency`` or ``stat`` each time it folds a batch. In
a Prometheus-compatible backend fed via OTLP the metric appears under a
translated name (commonly ``horde_kudos_applier_folded`` with a
``horde_kudos_row_type`` label). This harness records the database's applied-row
counts at ``begin``, and at ``end`` gates that the rows the counter reports folded
over the window equal the database's applied-row delta exactly, for each row type.

Because applied ledger and statistics rows are never purged (the retention job is
a compatibility no-op), the applied-row counts are a monotone truth baseline over
the run window, so their delta is a faithful expectation for the counter.

The counter is cumulative per applier process, and folding ownership moves
between processes (quorum handoff), so the raw metric is a set of per-process
series that appear, reset, and go stale over time. A point-in-time sample of the
summed series is therefore meaningless across a window. The window total is
instead computed from a range query: per series, positive increments are summed
with standard counter-reset handling (a sample below its predecessor contributes
its full value as the increment since the reset), and a series first seen after
the window start contributes its first value in full (a fresh process starts its
counter at zero). Increments a process never exported before dying are absent
from the backend and surface as a shortfall; that is a genuine telemetry gap,
not a harness artifact.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from kudos_ops_common import (
    connect,
    count_applied_stat_events,
)

# OTLP-to-Prometheus name translation is backend-dependent, so the exact metric
# name is discovered rather than assumed. This regex matches the plausible
# spellings (with or without the ``horde_`` prefix and the counter ``_total``
# suffix) against the series ``__name__``.
_METRIC_NAME_REGEX = r".*kudos.*applier.*folded.*"
_ROW_TYPES = ("currency", "stat")
# Range samples are fetched from this long before the window start so every
# series alive at the boundary contributes a pre-window baseline sample. It
# exceeds the export cadence by a wide margin; a series with a longer silent gap
# is treated as a fresh process (a live SDK re-exports cumulative sums every
# interval, so a longer silence means the process is gone).
_BASELINE_LOOKBACK_SECONDS = 300
_RANGE_STEP_SECONDS = 10


def _headers(org_id: str | None) -> dict[str, str]:
    """Return request headers, adding the tenant header only when an org id is given."""
    return {"X-Scope-OrgID": org_id} if org_id else {}


def _get(prom_url: str, path: str, params: dict, org_id: str | None) -> dict:
    response = requests.get(f"{prom_url.rstrip('/')}{path}", params=params, headers=_headers(org_id), timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise SystemExit(f"Prometheus API error for {path}: {payload}")
    return payload["data"]


def discover_metric(prom_url: str, org_id: str | None) -> tuple[str, str]:
    """Return the folded-counter metric name and its row-type label key.

    Discovery uses the ``/api/v1/series`` endpoint with a name regex so the harness
    does not depend on one spelling of the OTLP-translated name. When more than one
    candidate name exists (for example a bare gauge alongside the ``_total``
    counter) the ``_total`` spelling is preferred. The row-type label key is the
    single returned label whose name contains ``row_type``.
    """
    series = _get(prom_url, "/api/v1/series", {"match[]": f'{{__name__=~"{_METRIC_NAME_REGEX}"}}'}, org_id)
    if not series:
        raise SystemExit(f"No series matched {_METRIC_NAME_REGEX!r}; is the applier exporting to this backend?")
    names = {entry["__name__"] for entry in series}
    total_names = sorted(name for name in names if name.endswith("_total"))
    metric_name = total_names[0] if total_names else sorted(names)[0]
    label_keys = {key for entry in series if entry["__name__"] == metric_name for key in entry if "row_type" in key}
    if len(label_keys) != 1:
        raise SystemExit(f"Expected exactly one row_type label on {metric_name}, found {sorted(label_keys)}.")
    return metric_name, label_keys.pop()


def sum_windowed_increments(series_data: list[dict], window_start: float, row_type_label: str) -> tuple[dict[str, float], int]:
    """Return per-row-type folded totals over the window, and the reset count.

    ``series_data`` is the Prometheus range-query result list: one entry per
    series with its label set and ``[timestamp, value]`` samples. Samples at or
    before ``window_start`` only establish each series' baseline. Within the
    window, each sample contributes ``value - previous`` when the counter grew,
    ``value`` when it fell below its predecessor (a reset: the value is the
    accumulation since the restart), and a series with no baseline contributes
    its first in-window value in full (a fresh process starts at zero).
    """
    totals = {row_type: 0.0 for row_type in _ROW_TYPES}
    resets = 0
    for series in series_data:
        row_type = series["metric"].get(row_type_label)
        if row_type not in totals:
            continue
        previous: float | None = None
        for timestamp_text, value_text in series["values"]:
            value = float(value_text)
            if float(timestamp_text) <= window_start:
                previous = value
                continue
            if previous is None:
                totals[row_type] += value
            elif value >= previous:
                totals[row_type] += value - previous
            else:
                totals[row_type] += value
                resets += 1
            previous = value
    return totals, resets


def windowed_folded_totals(
    args: argparse.Namespace,
    metric_name: str,
    row_type_label: str,
    window_start: float,
) -> tuple[dict[str, float], int, int]:
    """Fetch the raw range and return (per-row-type totals, series count, resets)."""
    data = _get(
        args.prom_url,
        "/api/v1/query_range",
        {
            "query": metric_name,
            "start": window_start - _BASELINE_LOOKBACK_SECONDS,
            "end": time.time(),
            "step": _RANGE_STEP_SECONDS,
        },
        args.org_id,
    )
    series_data = data.get("result", [])
    totals, resets = sum_windowed_increments(series_data, window_start, row_type_label)
    return totals, len(series_data), resets


def db_applied_counts(dsn: str) -> tuple[dict[str, int], str | None]:
    """Return applied-row counts keyed by the counter's row types, plus the ledger mode.

    The mode matters for window validity: in shadow mode ledger rows are inserted
    already applied without passing through the applier, so the applied-row delta
    outgrows the folded counter and the comparison is meaningless. The gate is only
    valid over a window spent entirely in ledger mode.
    """
    conn = connect(dsn)
    try:
        # Floor-adjustment postings are excluded from the currency baseline:
        # the applier emits them already applied while folding an overdraft
        # (kudos_ledger.py marks the correction applied at creation), so they
        # never enter the folded count and are not part of the counter's claim.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM kudos_ledger WHERE applied = true AND entry_type != 'FLOOR_ADJUSTMENT'")
            currency_applied = int(cur.fetchone()[0])
        counts = {
            "currency": currency_applied,
            "stat": count_applied_stat_events(conn),
        }
        with conn.cursor() as cur:
            cur.execute("SELECT mode FROM kudos_ledger_control WHERE id = 1")
            row = cur.fetchone()
        return counts, row[0] if row else None
    finally:
        conn.close()


def _cmd_begin(args: argparse.Namespace) -> int:
    metric_name, row_type_label = discover_metric(args.prom_url, args.org_id)
    db_counts, mode = db_applied_counts(args.dsn)
    if mode != "ledger":
        raise SystemExit(
            f"Ledger mode is {mode!r}, not 'ledger'. In shadow mode rows are applied at emit without the applier, "
            "so the folded-counter comparison is meaningless. Flip to ledger mode before recording a baseline.",
        )
    state = {
        "begin_iso": datetime.now(UTC).isoformat(),
        "begin_epoch": time.time(),
        "prom_url": args.prom_url,
        "metric_name": metric_name,
        "row_type_label": row_type_label,
        "db": db_counts,
    }
    Path(args.state).write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps({"recorded": state}, indent=2))
    return 0


def _poll_stable_totals(
    args: argparse.Namespace,
    metric_name: str,
    row_type_label: str,
    window_start: float,
) -> tuple[dict[str, float], int, int]:
    """Poll the windowed totals until two computations ``--settle-seconds`` apart agree.

    The settle interval covers the OTLP export cadence so the gate reads a total
    that has finished propagating rather than one mid-export. If the totals never
    settle within the attempt bound the last computation is returned and the
    caller still gates on it.
    """
    previous = windowed_folded_totals(args, metric_name, row_type_label, window_start)
    for _ in range(args.max_settle_attempts):
        time.sleep(args.settle_seconds)
        current = windowed_folded_totals(args, metric_name, row_type_label, window_start)
        if current[0] == previous[0]:
            return current
        previous = current
    return previous


def _cmd_end(args: argparse.Namespace) -> int:
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    metric_name = state["metric_name"]
    row_type_label = state["row_type_label"]

    folded_totals, series_count, resets = _poll_stable_totals(args, metric_name, row_type_label, state["begin_epoch"])
    end_db, end_mode = db_applied_counts(args.dsn)
    if end_mode != "ledger":
        raise SystemExit(
            f"Ledger mode is {end_mode!r} at end, not 'ledger'; the window is invalid because shadow-mode rows are "
            "applied at emit without the applier. Keep the whole begin/end window in ledger mode.",
        )

    per_row_type: dict[str, dict] = {}
    passed = True
    for row_type in _ROW_TYPES:
        folded = folded_totals[row_type]
        db_delta = end_db[row_type] - state["db"][row_type]
        matched = folded == db_delta
        per_row_type[row_type] = {
            "counter_folded_in_window": folded,
            "db_delta": db_delta,
            "matched": matched,
        }
        if not matched:
            passed = False
            if folded < db_delta:
                # Increments a process never exported before dying are absent from
                # the backend; under crash chaos that shortfall is expected and is
                # itself the measurement, not a harness artifact.
                per_row_type[row_type]["note"] = (
                    "A shortfall means some folds were never exported (for example an applier process died between "
                    "folding and its next export). Treat as an accuracy failure only for a window without process churn."
                )

    report = {
        "metric_name": metric_name,
        "begin_iso": state["begin_iso"],
        "end_iso": datetime.now(UTC).isoformat(),
        "series_seen": series_count,
        "counter_resets_handled": resets,
        "per_row_type": per_row_type,
        "passed": passed,
    }
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", default="./kudos_telemetry_state.json", help="State file path (default: ./kudos_telemetry_state.json).")
    parser.add_argument("--prom-url", default="http://127.0.0.1:9009/prometheus", help="Prometheus-compatible API base URL.")
    parser.add_argument("--org-id", default=None, help="Optional X-Scope-OrgID tenant header value; omitted when not given.")
    parser.add_argument("--dsn", required=True, help="Postgres URI for the applied-row counts.")
    parser.add_argument("--settle-seconds", type=float, default=30.0, help="Seconds between counter stability samples (default: 30).")
    parser.add_argument(
        "--max-settle-attempts", type=int, default=10, help="Maximum settle samples before gating on the last value (default: 10)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("begin", help="Record the database baseline and window start.")
    commands.add_parser("end", help="Gate the windowed folded total against the database delta.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "begin":
        return _cmd_begin(args)
    return _cmd_end(args)


if __name__ == "__main__":
    sys.exit(main())
