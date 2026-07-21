# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Correlate hot-user-convoy artifacts into a phase-aligned verdict.

Reads the artifacts a hot-user-convoy run produces (the Locust CSV history, the
Postgres prober JSONL carrying the lock-chain series, the before/after
``pg_stat_statements`` snapshots, and the phase-boundary file) and buckets every
series into the baseline, pressure, and relief phases. It then prints a
phase-aligned timeline and a conclusion block that states, for each element of
the lock-convoy signature, whether the run reproduced it:

- tuple-lock queue depth on the ``users`` rows, and on the ``users.id = 0`` anon
  row specifically (waiting ``FOR NO KEY UPDATE``),
- blocking chains headed by ``idle in transaction`` sessions, and the age of the
  oldest blocking transaction,
- migrated contention on the rows still updated inline (``user_stats`` and
  ``user_records`` tuple-lock queue depth, and idle-in-transaction chains whose
  blocker query targets those rows), reported separately so an A/B shows the old
  users-row signature disappearing while a new one may or may not appear,
- the ``FOR NO KEY UPDATE`` statement's windowed mean and cumulative max latency
  from the ``pg_stat_statements`` delta,
- pop and status latency per phase, and whether reads degraded less than writes,
- and whether latency recovered once new work stopped in the relief phase.

The verdict is descriptive: a partial reproduction is reported as exactly that,
per element, rather than being rounded up to a confirmation.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

_POP = "[hc] pop"
_SUBMIT = "[hc] submit"
_ASYNC_ANON = "[hc] async anon"
_ASYNC_HEAVY = "[hc] async heavy"
_STATUS_POLL = "[hc] status poll"
_KUDOS = "[hc] kudos transfer"
_TRACKED_NAMES = [_POP, _SUBMIT, _ASYNC_ANON, _ASYNC_HEAVY, _STATUS_POLL, _KUDOS]

_PHASE_ORDER = ["baseline", "pressure", "relief"]

_FNKU_MARKER = "for no key update"

# Relations that would carry contention migrating off the users row. A blocker
# query naming one of these, at the head of an idle-in-transaction chain, is the
# post-fix signature the users-only classification cannot see.
_USER_STATS_RE = re.compile(r"\buser_stats\b", re.IGNORECASE)
_USER_RECORDS_RE = re.compile(r"\buser_records\b", re.IGNORECASE)


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


@dataclass
class _ProberPhase:
    active_sessions: list[int] = field(default_factory=list)
    idle_in_transaction: list[int] = field(default_factory=list)
    active_lock_waits: list[int] = field(default_factory=list)
    users_tuple_lock_waiting: list[int] = field(default_factory=list)
    users_fnku_waits: list[int] = field(default_factory=list)
    users_fnku_id_zero_waits: list[int] = field(default_factory=list)
    user_stats_tuple_lock_waiting: list[int] = field(default_factory=list)
    user_records_tuple_lock_waiting: list[int] = field(default_factory=list)
    migrated_idle_chains: list[int] = field(default_factory=list)
    blocking_chain_count: list[int] = field(default_factory=list)
    blocking_chains_idle_blocker: list[int] = field(default_factory=list)
    max_blocker_xact_age_s: list[float] = field(default_factory=list)
    deep_chain_example: dict | None = None
    migrated_chain_example: dict | None = None

    def mx(self, xs: list) -> float:
        return max(xs) if xs else 0

    def mean(self, xs: list) -> float:
        return statistics.mean(xs) if xs else 0.0


def _load_prober(prober_jsonl: Path, phases: _Phases) -> dict[str, _ProberPhase]:
    """Return per-phase prober aggregates, including a deepest-chain exemplar."""
    per_phase: dict[str, _ProberPhase] = {phase: _ProberPhase() for phase in _PHASE_ORDER}
    if not prober_jsonl.is_file():
        return per_phase
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
        phase = phases.phase_of(float(record.get("ts", 0)))
        if phase is None:
            continue
        bucket = per_phase[phase]
        bucket.active_sessions.append(int(record.get("active_sessions", 0)))
        bucket.idle_in_transaction.append(int(record.get("idle_in_transaction", 0)))
        bucket.active_lock_waits.append(int(record.get("active_lock_waits", 0)))
        bucket.users_tuple_lock_waiting.append(int(record.get("users_tuple_lock_waiting", 0)))
        bucket.users_fnku_waits.append(int(record.get("users_fnku_waits", 0)))
        bucket.users_fnku_id_zero_waits.append(int(record.get("users_fnku_id_zero_waits", 0)))
        # Per-relation tuple-lock waits (absent on pre-migration artifacts -> 0).
        by_rel = record.get("tuple_lock_waiting_by_relation", {}) or {}
        bucket.user_stats_tuple_lock_waiting.append(int(by_rel.get("user_stats", 0)))
        bucket.user_records_tuple_lock_waiting.append(int(by_rel.get("user_records", 0)))
        bucket.blocking_chain_count.append(int(record.get("blocking_chain_count", 0)))
        bucket.blocking_chains_idle_blocker.append(int(record.get("blocking_chains_idle_blocker", 0)))
        bucket.max_blocker_xact_age_s.append(float(record.get("max_blocker_xact_age_s", 0.0)))
        # Keep the sample's fullest idle-blocker chain as an exemplar for the report,
        # and count (plus keep an exemplar of) those whose blocker targets
        # user_stats/user_records: the migrated-contention chain the users-row
        # classification cannot flag.
        migrated = 0
        for chain in record.get("blocking_chains", []):
            if not chain.get("blocker_idle_in_transaction"):
                continue
            age = float(chain.get("blocker_xact_age_s", 0))
            best = bucket.deep_chain_example
            if best is None or age > float(best.get("blocker_xact_age_s", 0)):
                bucket.deep_chain_example = chain
            blocker_query = chain.get("blocker_query") or ""
            if _USER_STATS_RE.search(blocker_query) or _USER_RECORDS_RE.search(blocker_query):
                migrated += 1
                mbest = bucket.migrated_chain_example
                if mbest is None or age > float(mbest.get("blocker_xact_age_s", 0)):
                    bucket.migrated_chain_example = chain
        bucket.migrated_idle_chains.append(migrated)
    return per_phase


@dataclass
class _StatementDelta:
    calls: int
    windowed_mean_ms: float
    cumulative_max_ms: float


def _load_statement_delta(before_path: Path, after_path: Path) -> _StatementDelta | None:
    """Return the ``FOR NO KEY UPDATE`` statement delta from the two snapshots.

    Windowed mean is the delta of total execution time over the delta of calls, so
    it reflects only the run window. ``max_exec_time`` is a since-reset cumulative
    high-water mark, so the after-snapshot value is reported as-is (labelled).
    """
    if not before_path.is_file() or not after_path.is_file():
        return None
    before = _index_statements(json.loads(before_path.read_text(encoding="utf-8")))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    delta_calls = 0
    delta_total_ms = 0.0
    cumulative_max_ms = 0.0
    for row in after:
        query = (row.get("query") or "").lower()
        if _FNKU_MARKER not in query:
            continue
        queryid = row.get("queryid")
        prior = before.get(queryid, {"calls": 0, "total_exec_time": 0.0})
        delta_calls += int(row.get("calls", 0)) - int(prior.get("calls", 0))
        delta_total_ms += float(row.get("total_exec_time", 0.0)) - float(prior.get("total_exec_time", 0.0))
        cumulative_max_ms = max(cumulative_max_ms, float(row.get("max_exec_time", 0.0)))
    if delta_calls <= 0:
        return _StatementDelta(calls=max(0, delta_calls), windowed_mean_ms=0.0, cumulative_max_ms=cumulative_max_ms)
    return _StatementDelta(calls=delta_calls, windowed_mean_ms=delta_total_ms / delta_calls, cumulative_max_ms=cumulative_max_ms)


def _index_statements(rows: list[dict]) -> dict:
    return {row.get("queryid"): row for row in rows}


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
    print(f"{'request':<20}" + "".join(f"{phase:>24}" for phase in _PHASE_ORDER))
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
        ("active sessions (max)", lambda b: b.mx(b.active_sessions)),
        ("idle-in-txn (max)", lambda b: b.mx(b.idle_in_transaction)),
        ("active lock-waits (max)", lambda b: b.mx(b.active_lock_waits)),
        ("users tuple-lock waiting (max)", lambda b: b.mx(b.users_tuple_lock_waiting)),
        ("users FOR-NO-KEY-UPDATE waits (max)", lambda b: b.mx(b.users_fnku_waits)),
        ("users.id=0 FNKU waits (max)", lambda b: b.mx(b.users_fnku_id_zero_waits)),
        ("user_stats tuple-lock waiting (max)", lambda b: b.mx(b.user_stats_tuple_lock_waiting)),
        ("user_records tuple-lock waiting (max)", lambda b: b.mx(b.user_records_tuple_lock_waiting)),
        ("migrated idle-in-txn chains (max)", lambda b: b.mx(b.migrated_idle_chains)),
        ("blocking chains (max)", lambda b: b.mx(b.blocking_chain_count)),
        ("chains w/ idle-in-txn blocker (max)", lambda b: b.mx(b.blocking_chains_idle_blocker)),
        ("oldest blocking txn age s (max)", lambda b: round(b.mx(b.max_blocker_xact_age_s), 1)),
    ]
    print(f"{'metric':<38}" + "".join(f"{phase:>18}" for phase in _PHASE_ORDER))
    print("-" * 96)
    for label, getter in rows:
        cells = "".join(f"{getter(prober[phase]):>18}" for phase in _PHASE_ORDER)
        print(f"{label:<38}{cells}")
    print("-" * 96)


def _verdict(reproduced: bool, partial: bool) -> str:
    if reproduced:
        return "REPRODUCED"
    if partial:
        return "PARTIAL"
    return "NOT REPRODUCED"


def _print_conclusion(
    latency: dict[str, dict[str, _LatencyStat]],
    prober: dict[str, _ProberPhase],
    statement_delta: _StatementDelta | None,
) -> None:
    def peak(phase: str, name: str) -> float:
        return latency[phase][name].peak_p95()

    base_pop = peak("baseline", _POP)
    press_pop = peak("pressure", _POP)
    relief_pop = peak("relief", _POP)
    base_submit = peak("baseline", _SUBMIT)
    press_submit = peak("pressure", _SUBMIT)
    base_status = peak("baseline", _STATUS_POLL)
    press_status = peak("pressure", _STATUS_POLL)

    pop_ratio = (press_pop / base_pop) if base_pop > 0 else 0.0
    submit_ratio = (press_submit / base_submit) if base_submit > 0 else 0.0
    status_ratio = (press_status / base_status) if base_status > 0 else 0.0

    press = prober["pressure"]
    press_users_tuple = press.mx(press.users_tuple_lock_waiting)
    press_users0 = press.mx(press.users_fnku_id_zero_waits)
    press_fnku = press.mx(press.users_fnku_waits)
    press_idle_chains = press.mx(press.blocking_chains_idle_blocker)
    press_chain_depth = press.mx(press.blocking_chain_count)
    press_oldest_txn = press.mx(press.max_blocker_xact_age_s)
    press_user_stats_tuple = press.mx(press.user_stats_tuple_lock_waiting)
    press_user_records_tuple = press.mx(press.user_records_tuple_lock_waiting)
    press_migrated_chains = press.mx(press.migrated_idle_chains)

    # Signature elements. Thresholds separate a clear reproduction from a weaker
    # partial so a partial is never rounded up to a confirmation.
    users_tuple_queue = _verdict(press_users_tuple >= 5, press_users_tuple >= 1)
    users0_queue = _verdict(press_users0 >= 3, press_users0 >= 1)
    idle_chain = _verdict(press_idle_chains >= 3, press_idle_chains >= 1)
    # Migrated-contention elements reuse the users-row thresholds so a shift onto
    # the sibling rows reads on the same scale as the signature it replaces.
    user_stats_queue = _verdict(press_user_stats_tuple >= 5, press_user_stats_tuple >= 1)
    user_records_queue = _verdict(press_user_records_tuple >= 5, press_user_records_tuple >= 1)
    migrated_chain = _verdict(press_migrated_chains >= 3, press_migrated_chains >= 1)
    fnku_slow = "NOT REPRODUCED"
    if statement_delta is not None:
        fnku_slow = _verdict(
            statement_delta.windowed_mean_ms >= 100 or statement_delta.cumulative_max_ms >= 5000,
            statement_delta.windowed_mean_ms >= 20,
        )
    latency_blowup = _verdict(pop_ratio >= 3.0 or submit_ratio >= 3.0, pop_ratio >= 1.5 or submit_ratio >= 1.5)
    status_spared = _verdict(
        pop_ratio > 0 and status_ratio > 0 and pop_ratio >= status_ratio * 1.5,
        pop_ratio > 0 and status_ratio >= 0 and pop_ratio > status_ratio,
    )
    recovery = _verdict(base_pop > 0 and relief_pop <= base_pop * 1.5, press_pop > 0 and relief_pop <= press_pop * 0.6)

    print("\n" + "=" * 96)
    print("SIGNATURE VERDICT")
    print("=" * 96)
    print(f"pop p95 baseline->pressure : {_fmt(base_pop)} -> {_fmt(press_pop)} ms ({_ratio(press_pop, base_pop)})")
    print(f"submit p95 baseline->press : {_fmt(base_submit)} -> {_fmt(press_submit)} ms ({_ratio(press_submit, base_submit)})")
    print(f"status p95 baseline->press : {_fmt(base_status)} -> {_fmt(press_status)} ms ({_ratio(press_status, base_status)})")
    print(f"pop p95 relief             : {_fmt(relief_pop)} ms ({_ratio(relief_pop, base_pop)} vs baseline)")
    print(f"users tuple-lock queue (press, max)   : {press_users_tuple}")
    print(f"users.id=0 FNKU waits (press, max)     : {press_users0}")
    print(f"users FNKU waits any-row (press, max)  : {press_fnku}")
    print(f"blocking chain depth (press, max)      : {press_chain_depth}")
    print(f"idle-in-txn-headed chains (press, max) : {press_idle_chains}")
    print(f"oldest blocking txn age (press, max s) : {press_oldest_txn:.1f}")
    if statement_delta is not None:
        print(
            f"FOR NO KEY UPDATE (delta)              : {statement_delta.calls} calls, "
            f"windowed mean {statement_delta.windowed_mean_ms:.1f} ms, cumulative max {statement_delta.cumulative_max_ms:,.0f} ms",
        )
    else:
        print("FOR NO KEY UPDATE (delta)              : pg_stat_statements snapshots MISSING")
    print("-" * 96)
    print(f"{'users-row tuple-lock queue':<40}: {users_tuple_queue}")
    print(f"{'users.id=0 FNKU queue':<40}: {users0_queue}")
    print(f"{'idle-in-txn-headed blocking chains':<40}: {idle_chain}")
    print(f"{'FOR NO KEY UPDATE latency inflated':<40}: {fnku_slow}")
    print(f"{'pop/submit latency blowup':<40}: {latency_blowup}")
    print(f"{'status less affected than pop':<40}: {status_spared}")
    print(f"{'recovery on relief':<40}: {recovery}")
    print("-" * 96)

    # Migrated-contention watch: reported separately from the users-row verdict so
    # an A/B shows the old signature disappearing while a new one may appear on the
    # rows still updated inline. These do not feed the overall convoy verdict.
    print("MIGRATED CONTENTION (rows still updated inline: user_stats / user_records)")
    print(f"user_stats tuple-lock queue (press, max)   : {press_user_stats_tuple}")
    print(f"user_records tuple-lock queue (press, max) : {press_user_records_tuple}")
    print(f"migrated idle-in-txn chains (press, max)   : {press_migrated_chains}")
    print(f"{'user_stats tuple-lock queue':<40}: {user_stats_queue}")
    print(f"{'user_records tuple-lock queue':<40}: {user_records_queue}")
    print(f"{'idle-in-txn chains on stats/records':<40}: {migrated_chain}")
    print("-" * 96)

    exemplar = press.deep_chain_example
    if exemplar is not None:
        print("\ndeepest idle-in-transaction blocking chain observed in the pressure phase:")
        print(
            f"  blocker pid {exemplar.get('blocker_pid')} state={exemplar.get('blocker_state')!r} "
            f"xact_age={exemplar.get('blocker_xact_age_s')}s",
        )
        print(f"    blocker query: {(exemplar.get('blocker_query') or '')[:160]}")
        print(f"  blocked pid {exemplar.get('blocked_pid')} wait={exemplar.get('blocked_wait')!r}")
        print(f"    blocked query: {(exemplar.get('blocked_query') or '')[:160]}")

    migrated_exemplar = press.migrated_chain_example
    if migrated_exemplar is not None:
        print("\ndeepest idle-in-transaction chain whose blocker targets user_stats/user_records:")
        print(
            f"  blocker pid {migrated_exemplar.get('blocker_pid')} state={migrated_exemplar.get('blocker_state')!r} "
            f"xact_age={migrated_exemplar.get('blocker_xact_age_s')}s",
        )
        print(f"    blocker query: {(migrated_exemplar.get('blocker_query') or '')[:160]}")
        print(f"  blocked pid {migrated_exemplar.get('blocked_pid')} wait={migrated_exemplar.get('blocked_wait')!r}")
        print(f"    blocked query: {(migrated_exemplar.get('blocked_query') or '')[:160]}")

    core = [users_tuple_queue, users0_queue, idle_chain, fnku_slow]
    strong = core.count("REPRODUCED")
    partial = core.count("PARTIAL")
    print("\n" + "=" * 96)
    if strong >= 3:
        overall = "HOT-USER CONVOY REPRODUCED: the users-row lock-queue signature formed under pressure."
    elif strong >= 1 or partial >= 2:
        overall = "HOT-USER CONVOY PARTIALLY REPRODUCED: some signature elements formed; see per-element verdicts."
    else:
        overall = "HOT-USER CONVOY NOT REPRODUCED at these parameters."
    print(overall)
    print("=" * 96)


def _print_population_health(stats_csv: Path) -> bool:
    """Report per-population failure rates; return False when the run is invalid.

    A population failing wholesale (for example every anonymous request 401ing
    because the seeded key hash predates the current salt) silently removes its
    load from the measured signature while every downstream verdict still
    renders. Surface it as a loud invalid-run banner so a broken run is never
    mistaken for a clean one.
    """
    if not stats_csv.is_file():
        print(f"WARNING: {stats_csv} missing; cannot check population health")
        return True
    broken: list[tuple[str, int, int]] = []
    with stats_csv.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Name", "")
            if name == "Aggregated":
                continue
            requests = int(row.get("Request Count", 0) or 0)
            failures = int(row.get("Failure Count", 0) or 0)
            if requests >= 50 and failures / requests > 0.5:
                broken.append((name, failures, requests))
    if not broken:
        return True
    print("=" * 96)
    print("INVALID RUN: population(s) failing wholesale; their load is ABSENT from the signature")
    for name, failures, requests in broken:
        print(f"  {name}: {failures}/{requests} failed")
    print("  Fix the failing population and rerun; verdicts below describe a DIFFERENT scenario.")
    print("=" * 96)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze hot-user-convoy run artifacts into a verdict.")
    parser.add_argument("--run-dir", required=True, help="Directory containing the run artifacts.")
    parser.add_argument("--phases", default=None, help="Path to phases.json (defaults to <run-dir>/phases.json).")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    phases_path = Path(args.phases) if args.phases else run_dir / "phases.json"
    history_csv = run_dir / "hc_stats_history.csv"
    prober_jsonl = run_dir / "pg_prober.jsonl"
    before_path = run_dir / "pg_stat_statements_before.json"
    after_path = run_dir / "pg_stat_statements_after.json"

    if not phases_path.is_file():
        print(f"ERROR: phases file not found: {phases_path}")
        return 2

    phases = _load_phases(phases_path)
    latency = _load_latency(history_csv, phases)
    prober = _load_prober(prober_jsonl, phases)
    statement_delta = _load_statement_delta(before_path, after_path)

    print(f"run dir     : {run_dir}")
    print(f"history csv : {history_csv} ({'present' if history_csv.is_file() else 'MISSING'})")
    print(f"prober jsonl: {prober_jsonl} ({'present' if prober_jsonl.is_file() else 'MISSING'})")
    print(f"pgss snaps  : {'present' if before_path.is_file() and after_path.is_file() else 'MISSING'}")

    healthy = _print_population_health(run_dir / "hc_stats.csv")
    _print_latency_table(latency)
    _print_prober_table(prober)
    _print_conclusion(latency, prober, statement_delta)
    return 0 if healthy else 3


if __name__ == "__main__":
    raise SystemExit(main())
