# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Correlate queue-pressure artifacts into a phase-aligned verdict.

Reads the artifacts a queue-pressure run produces (the Locust CSV history, the
Postgres prober JSONL, the Postgres container log, and the phase-boundary file)
and buckets every series into the baseline, pressure, and relief phases. It then
prints a phase-aligned timeline table and a conclusion block that states, for
each element of the lock-convoy signature, whether the run reproduced it:

- pop/submit latency blowup relative to baseline,
- active-session and RowShareLock spikes tracking the backlog,
- deadlocks confined to the pressure window (prober counter delta and log grep),
- whether status reads degraded less than worker writes,
- and whether latency recovered once inflation stopped in the relief phase.

The verdict is descriptive: it reports which elements reproduced and which did
not, so a partial reproduction is presented as exactly that rather than being
rounded up to a confirmation.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

_POP = "[qp] pop"
_SUBMIT = "[qp] submit"
_ASYNC_SERVED = "[qp] async served"
_ASYNC_BACKLOG = "[qp] async backlog"
_STATUS = "[qp] status served"
_TRACKED_NAMES = [_POP, _SUBMIT, _ASYNC_SERVED, _ASYNC_BACKLOG, _STATUS]

_PHASE_ORDER = ["baseline", "pressure", "relief"]


@dataclass
class _Phases:
    run_start: float
    baseline_start: float
    pressure_start: float
    relief_start: float
    relief_end: float

    def phase_of(self, ts: float) -> str | None:
        if ts < self.baseline_start:
            return None
        if ts < self.pressure_start:
            return "baseline"
        if ts < self.relief_start:
            return "pressure"
        if ts < self.relief_end:
            return "relief"
        return None


@dataclass
class _LatencyStat:
    medians: list[float] = field(default_factory=list)
    p95s: list[float] = field(default_factory=list)
    request_count_last: int = 0
    request_count_first: int | None = None

    def p50(self) -> float:
        return statistics.median(self.medians) if self.medians else 0.0

    def peak_p95(self) -> float:
        return max(self.p95s) if self.p95s else 0.0

    def requests(self) -> int:
        if self.request_count_first is None:
            return 0
        return max(0, self.request_count_last - self.request_count_first)


def _load_phases(path: Path) -> _Phases:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _Phases(
        run_start=data["run_start"],
        baseline_start=data["baseline_start"],
        pressure_start=data["pressure_start"],
        relief_start=data["relief_start"],
        relief_end=data["relief_end"],
    )


def _load_latency(history_csv: Path, phases: _Phases) -> dict[str, dict[str, _LatencyStat]]:
    """Return latency stats keyed by phase then request name from the CSV history."""
    stats: dict[str, dict[str, _LatencyStat]] = {phase: {name: _LatencyStat() for name in _TRACKED_NAMES} for phase in _PHASE_ORDER}
    if not history_csv.is_file():
        return stats
    with history_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Name", "")
            if name not in _TRACKED_NAMES:
                continue
            try:
                ts = float(row.get("Timestamp", "0"))
            except ValueError:
                continue
            phase = phases.phase_of(ts)
            if phase is None:
                continue
            stat = stats[phase][name]
            median = _num(row.get("50%"))
            p95 = _num(row.get("95%"))
            # Snapshot percentile columns are blank when that name saw no traffic
            # in the interval; only fold in populated samples.
            if median is not None and (median > 0 or p95):
                stat.medians.append(median)
            if p95 is not None and p95 > 0:
                stat.p95s.append(p95)
            total = _int(row.get("Total Request Count"))
            if total is not None:
                if stat.request_count_first is None:
                    stat.request_count_first = total
                stat.request_count_last = total
    return stats


def _num(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


@dataclass
class _ProberPhase:
    active_sessions: list[int] = field(default_factory=list)
    idle_in_transaction: list[int] = field(default_factory=list)
    rowsharelock_granted: list[int] = field(default_factory=list)
    rowsharelock_waiting: list[int] = field(default_factory=list)
    locks_waiting_total: list[int] = field(default_factory=list)
    active_lock_waits: list[int] = field(default_factory=list)
    backlog_text: list[int] = field(default_factory=list)
    deadlocks_last: int = 0
    deadlocks_first: int | None = None

    def _mx(self, xs: list[int]) -> int:
        return max(xs) if xs else 0

    def _mean(self, xs: list[int]) -> float:
        return statistics.mean(xs) if xs else 0.0

    def deadlock_delta(self) -> int:
        if self.deadlocks_first is None:
            return 0
        return max(0, self.deadlocks_last - self.deadlocks_first)


def _load_prober(prober_jsonl: Path, phases: _Phases) -> tuple[dict[str, _ProberPhase], int]:
    """Return per-phase prober aggregates and the whole-run deadlock delta."""
    per_phase: dict[str, _ProberPhase] = {phase: _ProberPhase() for phase in _PHASE_ORDER}
    first_deadlocks: int | None = None
    last_deadlocks = 0
    if not prober_jsonl.is_file():
        return per_phase, 0
    for line in prober_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "sample_error" in record:
            continue
        deadlocks = int(record.get("deadlocks", 0))
        if first_deadlocks is None:
            first_deadlocks = deadlocks
        last_deadlocks = deadlocks
        ts = float(record.get("ts", 0))
        phase = phases.phase_of(ts)
        if phase is None:
            continue
        bucket = per_phase[phase]
        bucket.active_sessions.append(int(record.get("active_sessions", 0)))
        bucket.idle_in_transaction.append(int(record.get("idle_in_transaction", 0)))
        bucket.rowsharelock_granted.append(int(record.get("rowsharelock_granted", 0)))
        bucket.rowsharelock_waiting.append(int(record.get("rowsharelock_waiting", 0)))
        bucket.locks_waiting_total.append(int(record.get("locks_waiting_total", 0)))
        bucket.active_lock_waits.append(int(record.get("active_lock_waits", 0)))
        bucket.backlog_text.append(int(record.get("backlog_text", 0)))
        if bucket.deadlocks_first is None:
            bucket.deadlocks_first = deadlocks
        bucket.deadlocks_last = deadlocks
    whole_run_delta = 0 if first_deadlocks is None else max(0, last_deadlocks - first_deadlocks)
    return per_phase, whole_run_delta


def _grep_pg_log(postgres_log: Path) -> dict[str, list[str]]:
    """Grep the Postgres container log for deadlock and lock-wait evidence."""
    findings: dict[str, list[str]] = {"deadlock": [], "lock_wait": [], "too_many_connections": []}
    if not postgres_log.is_file():
        return findings
    patterns = {
        "deadlock": re.compile(r"deadlock detected", re.IGNORECASE),
        "lock_wait": re.compile(r"still waiting for|process \d+ (still )?waiting", re.IGNORECASE),
        "too_many_connections": re.compile(r"too many clients|remaining connection slots|sorry, too many", re.IGNORECASE),
    }
    for line in postgres_log.read_text(encoding="utf-8", errors="replace").splitlines():
        for key, pattern in patterns.items():
            if pattern.search(line):
                if len(findings[key]) < 8:
                    findings[key].append(line.strip())
    return findings


def _fmt(value: float) -> str:
    return f"{value:,.0f}"


def _ratio(pressure: float, baseline: float) -> str:
    if baseline <= 0:
        return "n/a" if pressure <= 0 else "inf"
    return f"{pressure / baseline:.1f}x"


def _print_latency_table(latency: dict[str, dict[str, _LatencyStat]]) -> None:
    print("\n" + "=" * 96)
    print("PER-PHASE LATENCY (ms): p50 = median of interval medians, p95 = peak interval p95")
    print("=" * 96)
    header = f"{'request':<20}" + "".join(f"{phase:>24}" for phase in _PHASE_ORDER)
    print(header)
    print(f"{'':<20}" + "".join(f"{'p50 / p95 / reqs':>24}" for _ in _PHASE_ORDER))
    print("-" * 96)
    for name in _TRACKED_NAMES:
        cells = []
        for phase in _PHASE_ORDER:
            stat = latency[phase][name]
            cells.append(f"{_fmt(stat.p50())} / {_fmt(stat.peak_p95())} / {stat.requests()}")
        print(f"{name:<20}" + "".join(f"{cell:>24}" for cell in cells))
    print("-" * 96)


def _print_prober_table(prober: dict[str, _ProberPhase]) -> None:
    print("\n" + "=" * 96)
    print("PER-PHASE POSTGRES (max within phase unless noted)")
    print("=" * 96)
    rows = [
        ("active sessions (max)", lambda b: b._mx(b.active_sessions)),
        ("active sessions (mean)", lambda b: round(b._mean(b.active_sessions), 1)),
        ("idle-in-txn (max)", lambda b: b._mx(b.idle_in_transaction)),
        ("RowShareLock granted (max)", lambda b: b._mx(b.rowsharelock_granted)),
        ("RowShareLock waiting (max)", lambda b: b._mx(b.rowsharelock_waiting)),
        ("locks waiting total (max)", lambda b: b._mx(b.locks_waiting_total)),
        ("active lock-waits (max)", lambda b: b._mx(b.active_lock_waits)),
        ("text backlog (max)", lambda b: b._mx(b.backlog_text)),
        ("deadlock delta (phase)", lambda b: b.deadlock_delta()),
    ]
    header = f"{'metric':<30}" + "".join(f"{phase:>18}" for phase in _PHASE_ORDER)
    print(header)
    print("-" * 96)
    for label, getter in rows:
        cells = "".join(f"{getter(prober[phase]):>18}" for phase in _PHASE_ORDER)
        print(f"{label:<30}{cells}")
    print("-" * 96)


def _print_conclusion(
    latency: dict[str, dict[str, _LatencyStat]],
    prober: dict[str, _ProberPhase],
    whole_run_deadlock_delta: int,
    pg_findings: dict[str, list[str]],
) -> None:
    def peak(phase: str, name: str) -> float:
        return latency[phase][name].peak_p95()

    base_pop = peak("baseline", _POP)
    press_pop = peak("pressure", _POP)
    relief_pop = peak("relief", _POP)
    base_submit = peak("baseline", _SUBMIT)
    press_submit = peak("pressure", _SUBMIT)
    base_status = peak("baseline", _STATUS)
    press_status = peak("pressure", _STATUS)

    pop_ratio = (press_pop / base_pop) if base_pop > 0 else 0.0
    submit_ratio = (press_submit / base_submit) if base_submit > 0 else 0.0
    status_ratio = (press_status / base_status) if base_status > 0 else 0.0

    base_sessions = prober["baseline"]._mx(prober["baseline"].active_sessions)
    press_sessions = prober["pressure"]._mx(prober["pressure"].active_sessions)
    base_rsl = prober["baseline"]._mx(prober["baseline"].rowsharelock_granted)
    press_rsl = prober["pressure"]._mx(prober["pressure"].rowsharelock_granted)
    press_backlog = prober["pressure"]._mx(prober["pressure"].backlog_text)
    press_lock_waits = prober["pressure"]._mx(prober["pressure"].active_lock_waits)

    # Signature element decisions. "reproduced" uses a strong threshold, "partial"
    # a weaker one, so a partial reproduction is never rounded up to a yes.
    def verdict(reproduced: bool, partial: bool) -> str:
        if reproduced:
            return "REPRODUCED"
        if partial:
            return "PARTIAL"
        return "not seen"

    latency_blowup = verdict(pop_ratio >= 3.0 or submit_ratio >= 3.0, pop_ratio >= 1.5 or submit_ratio >= 1.5)
    sessions_spike = verdict(
        press_sessions >= max(base_sessions * 1.5, base_sessions + 8),
        press_sessions >= base_sessions + 3,
    )
    rsl_spike = verdict(
        press_rsl >= max(base_rsl * 1.5, base_rsl + 8),
        press_rsl >= base_rsl + 3,
    )
    deadlocks_seen = whole_run_deadlock_delta > 0 or bool(pg_findings["deadlock"])
    deadlock_verdict = "REPRODUCED" if deadlocks_seen else "not seen"
    lock_waits_seen = press_lock_waits > 0 or bool(pg_findings["lock_wait"])
    status_spared = verdict(
        (pop_ratio > 0 and status_ratio > 0 and pop_ratio >= status_ratio * 1.5),
        (pop_ratio > 0 and status_ratio >= 0 and pop_ratio > status_ratio),
    )
    recovery = verdict(
        (base_pop > 0 and relief_pop <= base_pop * 1.5),
        (press_pop > 0 and relief_pop <= press_pop * 0.6),
    )

    print("\n" + "=" * 96)
    print("SIGNATURE VERDICT")
    print("=" * 96)
    print(f"pop p95 baseline->pressure : {_fmt(base_pop)} -> {_fmt(press_pop)} ms ({_ratio(press_pop, base_pop)})")
    print(f"submit p95 baseline->press : {_fmt(base_submit)} -> {_fmt(press_submit)} ms ({_ratio(press_submit, base_submit)})")
    print(f"status p95 baseline->press : {_fmt(base_status)} -> {_fmt(press_status)} ms ({_ratio(press_status, base_status)})")
    print(f"pop p95 relief             : {_fmt(relief_pop)} ms ({_ratio(relief_pop, base_pop)} vs baseline)")
    print(f"active sessions base->press: {base_sessions} -> {press_sessions}")
    print(f"RowShareLock base->press   : {base_rsl} -> {press_rsl}")
    print(f"text backlog peak (press)  : {press_backlog}")
    print(f"lock-waits peak (press)    : {press_lock_waits} (pg-log lock-wait lines: {len(pg_findings['lock_wait'])})")
    print(f"deadlock delta (whole run) : {whole_run_deadlock_delta} (pg-log deadlock lines: {len(pg_findings['deadlock'])})")
    print(f"too-many-connections lines : {len(pg_findings['too_many_connections'])}")
    print("-" * 96)
    print(f"{'pop/submit latency blowup':<40}: {latency_blowup}")
    print(f"{'active-session spike':<40}: {sessions_spike}")
    print(f"{'RowShareLock spike':<40}: {rsl_spike}")
    print(f"{'lock waiting present':<40}: {'yes' if lock_waits_seen else 'no'}")
    print(f"{'deadlocks in window':<40}: {deadlock_verdict}")
    print(f"{'status less affected than pop':<40}: {status_spared}")
    print(f"{'recovery on relief':<40}: {recovery}")
    print("-" * 96)

    for label, lines in (
        ("deadlock detected", pg_findings["deadlock"]),
        ("lock waits", pg_findings["lock_wait"]),
        ("connection exhaustion", pg_findings["too_many_connections"]),
    ):
        if lines:
            print(f"\npg-log evidence [{label}] (up to 8 lines):")
            for entry in lines:
                print(f"  {entry}")

    strong = [latency_blowup, sessions_spike, rsl_spike].count("REPRODUCED") + (1 if deadlocks_seen else 0)
    partial = [latency_blowup, sessions_spike, rsl_spike].count("PARTIAL")
    print("\n" + "=" * 96)
    if strong >= 3:
        overall = "HYPOTHESIS SUPPORTED: multiple signature elements reproduced together."
    elif strong >= 1 or partial >= 2:
        overall = "HYPOTHESIS PARTIALLY SUPPORTED: some elements reproduced; see per-element verdicts."
    else:
        overall = "HYPOTHESIS NOT REPRODUCED at these parameters."
    print(overall)
    print("=" * 96)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze queue-pressure run artifacts into a verdict.")
    parser.add_argument("--run-dir", required=True, help="Directory containing the run artifacts.")
    parser.add_argument("--phases", default=None, help="Path to phases.json (defaults to <run-dir>/phases.json).")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    phases_path = Path(args.phases) if args.phases else run_dir / "phases.json"
    history_csv = run_dir / "qp_stats_history.csv"
    prober_jsonl = run_dir / "pg_prober.jsonl"
    postgres_log = run_dir / "postgres.log"

    if not phases_path.is_file():
        print(f"ERROR: phases file not found: {phases_path}")
        return 2

    phases = _load_phases(phases_path)
    latency = _load_latency(history_csv, phases)
    prober, whole_run_deadlock_delta = _load_prober(prober_jsonl, phases)
    pg_findings = _grep_pg_log(postgres_log)

    print(f"run dir     : {run_dir}")
    print(f"history csv : {history_csv} ({'present' if history_csv.is_file() else 'MISSING'})")
    print(f"prober jsonl: {prober_jsonl} ({'present' if prober_jsonl.is_file() else 'MISSING'})")
    print(f"postgres log: {postgres_log} ({'present' if postgres_log.is_file() else 'MISSING'})")

    _print_latency_table(latency)
    _print_prober_table(prober)
    _print_conclusion(latency, prober, whole_run_deadlock_delta, pg_findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
