# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Standalone Postgres sampler for the queue-pressure simulation.

Runs as its own process and samples a handful of cheap catalog views once per
second, appending one timestamped JSON object per sample to a JSONL file. The
series it captures are the instrumentation used to confirm or refute the
lock-convoy hypothesis:

- ``pg_stat_activity`` backend counts grouped by ``state`` and by
  ``wait_event_type``/``wait_event``, plus the active-session total (the metric
  that spikes as sessions pile up toward the connection limit).
- ``pg_locks`` counts grouped by ``mode`` with granted and ungranted totals kept
  separate, so ``RowShareLock`` pressure and any lock waiting are both visible.
- The ``pg_stat_database`` cumulative ``deadlocks`` counter for the target
  database, whose per-interval delta reveals deadlocks confined to a window.
- The current text and image waiting-prompt backlog depth, via a plain
  ``SELECT count(*)`` over ``waiting_prompts`` filtered to the same
  ``n > 0 AND active AND NOT faulted`` predicate the priority-bump and pop scans
  use. This is an MVCC read that takes no row locks, so the probe never
  perturbs the very contention it measures.
- Lock counts confined to the ``users`` table (tuple-lock granted/waiting and
  the total waiting across modes), which localise contention to the hot-user
  row family.
- Per-relation tuple-lock counts (granted and waiting) bucketed by relation name
  for a tracked set (``users``, ``user_stats``, ``user_records``,
  ``worker_stats``, ``waiting_prompts``) plus an ``other`` bucket. The users-only
  probe above cannot see contention that migrates off the users row onto the
  sibling per-user rows still updated inline; this per-relation view attributes a
  tuple-lock wait to whichever relation it actually lands on.
- Blocking chains derived from ``pg_blocking_pids``: each blocked/blocker edge
  with the blocker's session state and transaction age, the blocked and blocker
  query texts (truncated), and per-chain flags marking the implicated
  ``SELECT ... FOR NO KEY UPDATE`` on the users table (and the ``users.id = 0``
  anon row specifically). Summary counters accompany the (bounded) chain list so
  a run can be judged on chain depth and idle-in-transaction blockers without
  re-parsing SQL offline.

The sampling connection runs in autocommit with a short ``statement_timeout`` so
a stalled sample cannot itself pile onto the backend under study. The users-lock
and blocking-chain probes are folded in as best-effort additions: a failure in
either is attached to the record and left non-fatal so the base series survive.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType

import psycopg2

_APPLICATION_NAME = "qp_prober"

# Backlog predicate mirroring increment_extra_priority / the pop candidate scan
# (waiting_prompts is single-table-inheritance keyed by wp_type).
_BACKLOG_SQL = """
SELECT wp_type, count(*)
FROM waiting_prompts
WHERE n > 0 AND active = true AND faulted = false
GROUP BY wp_type
"""

_ACTIVITY_STATE_SQL = """
SELECT coalesce(state, 'unknown') AS state, count(*)
FROM pg_stat_activity
WHERE datname = %s AND application_name <> %s AND pid <> pg_backend_pid()
GROUP BY state
"""

_ACTIVITY_WAIT_SQL = """
SELECT coalesce(wait_event_type, 'none') AS wet, coalesce(wait_event, 'none') AS we, count(*)
FROM pg_stat_activity
WHERE datname = %s AND application_name <> %s AND pid <> pg_backend_pid() AND state = 'active'
GROUP BY wait_event_type, wait_event
"""

_LOCKS_SQL = """
SELECT mode, granted, count(*)
FROM pg_locks
GROUP BY mode, granted
"""

_DEADLOCKS_SQL = """
SELECT deadlocks
FROM pg_stat_database
WHERE datname = %s
"""

# Lock counts confined to the ``users`` table, the row family the hot-user
# convoy targets. Resolving the oid through a scalar sub-select keeps the query
# safe on a database where the table is absent (the sub-select yields NULL and
# no rows match) rather than raising an ``undefined_table`` that would abort the
# whole sample.
_USERS_LOCKS_SQL = """
SELECT locktype, granted, count(*)
FROM pg_locks
WHERE relation = (SELECT oid FROM pg_class WHERE relname = 'users' LIMIT 1)
GROUP BY locktype, granted
"""

# Per-relation tuple-lock counts. A ``tuple`` lock names its table through the
# ``relation`` oid, so joining ``pg_locks`` to ``pg_class`` attributes each
# row-level wait to the relation it targets. Grouping by name (rather than
# resolving a single oid) lets a wait that has migrated off the users row onto a
# sibling per-user row be seen where it lands instead of vanishing.
_RELATION_TUPLE_LOCKS_SQL = """
SELECT c.relname AS relname, l.granted AS granted, count(*) AS n
FROM pg_locks AS l
JOIN pg_class AS c ON c.oid = l.relation
WHERE l.locktype = 'tuple'
GROUP BY c.relname, l.granted
"""

# Relations whose per-user or per-worker rows are updated inline per request and
# are the candidate homes for contention migrating off the users row. Anything
# outside this set is folded into an ``other`` bucket.
_TRACKED_LOCK_RELATIONS = ("users", "user_stats", "user_records", "worker_stats", "waiting_prompts")

# Blocking chains derived from ``pg_blocking_pids``: one row per (blocked pid,
# blocker pid) edge, carrying the blocker's session state and transaction age so
# a chain headed by an ``idle in transaction`` session (the observed convoy
# culprit) can be told apart from one headed by an actively-working backend. The
# query texts are truncated server-side to bound the JSONL line size.
_BLOCKING_CHAINS_SQL = """
SELECT
    blocked.pid AS blocked_pid,
    coalesce(blocked.state, '') AS blocked_state,
    coalesce(blocked.wait_event_type, '') || '/' || coalesce(blocked.wait_event, '') AS blocked_wait,
    left(coalesce(blocked.query, ''), %s) AS blocked_query,
    blocker.pid AS blocker_pid,
    coalesce(blocker.state, '') AS blocker_state,
    extract(epoch FROM (now() - blocker.xact_start)) AS blocker_xact_age_s,
    left(coalesce(blocker.query, ''), %s) AS blocker_query
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(blocker_pid)
JOIN pg_stat_activity AS blocker ON blocker.pid = bp.blocker_pid
WHERE blocked.datname = %s
"""

_QUERY_TRUNCATE_CHARS = 300
_MAX_CHAINS_PER_SAMPLE = 25

# A waiting ``SELECT ... FOR NO KEY UPDATE`` over the users table is the exact
# statement the production forensics implicated. ``users.id = 0`` is the anon
# requester row; the literal survives in the query text as ``in (0`` or ``= 0``.
_FNKU_RE = re.compile(r"for no key update", re.IGNORECASE)
_USERS_RE = re.compile(r"\busers\b", re.IGNORECASE)
_USERS_ZERO_RE = re.compile(r"(in \(0[\s,)])|(=\s*0\b)", re.IGNORECASE)


class _StopFlag:
    """Cooperative stop flag toggled by SIGINT/SIGTERM so the file is flushed cleanly."""

    def __init__(self) -> None:
        self.stop = False

    def request_stop(self, _signum: int, _frame: FrameType | None) -> None:
        self.stop = True


def _connect(args: argparse.Namespace) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(
        host=args.host,
        port=args.port,
        dbname=args.dbname,
        user=args.user,
        password=args.password,
        application_name=_APPLICATION_NAME,
        connect_timeout=5,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '3000ms'")
    return conn


def _sample_users_locks(cur: psycopg2.extensions.cursor, record: dict[str, object]) -> None:
    """Fold per-``users``-table lock counts into ``record`` (best effort).

    A failure here (e.g. a missing table on an unexpected schema) is attached as
    ``users_lock_error`` and left non-fatal so the base activity/lock series the
    sample already carries are never lost to a lock-chain probe error.
    """
    try:
        cur.execute(_USERS_LOCKS_SQL)
    except psycopg2.Error as exc:
        record["users_lock_error"] = str(exc)
        return
    tuple_waiting = 0
    tuple_granted = 0
    waiting_total = 0
    for locktype, granted, count in cur.fetchall():
        count = int(count)
        if not granted:
            waiting_total += count
        if locktype == "tuple":
            if granted:
                tuple_granted += count
            else:
                tuple_waiting += count
    record["users_tuple_lock_waiting"] = tuple_waiting
    record["users_tuple_lock_granted"] = tuple_granted
    record["users_lock_waiting_total"] = waiting_total


def _sample_relation_locks(cur: psycopg2.extensions.cursor, record: dict[str, object]) -> None:
    """Fold per-relation tuple-lock counts into ``record`` (best effort).

    Generalises the users-only probe so a tuple-lock wait is attributed to the
    relation it targets (``user_stats``/``user_records`` in particular, the rows
    an append-only kudos ledger leaves updated inline). Relations outside the
    tracked set fall into an ``other`` bucket. A failure is attached as
    ``relation_lock_error`` and left non-fatal, matching the users probe, so the
    base series and the users-specific fields are never lost to this addition.
    """
    try:
        cur.execute(_RELATION_TUPLE_LOCKS_SQL)
    except psycopg2.Error as exc:
        record["relation_lock_error"] = str(exc)
        return
    waiting = {name: 0 for name in _TRACKED_LOCK_RELATIONS}
    granted = {name: 0 for name in _TRACKED_LOCK_RELATIONS}
    waiting["other"] = 0
    granted["other"] = 0
    for relname, is_granted, count in cur.fetchall():
        bucket = relname if relname in waiting else "other"
        if is_granted:
            granted[bucket] += int(count)
        else:
            waiting[bucket] += int(count)
    record["tuple_lock_waiting_by_relation"] = waiting
    record["tuple_lock_granted_by_relation"] = granted


def _sample_blocking_chains(cur: psycopg2.extensions.cursor, dbname: str, record: dict[str, object]) -> None:
    """Fold ``pg_blocking_pids`` chains and their summary counts into ``record``.

    Each captured chain is classified so the analyzer can isolate the convoy
    signature without re-parsing SQL: ``for_no_key_update`` marks the implicated
    statement, ``on_users`` that it touches the users table, and ``users_id_zero``
    that it references the anon requester row specifically. The chain list is
    truncated to a bounded count while the summary counters reflect every edge.
    """
    try:
        cur.execute(_BLOCKING_CHAINS_SQL, (_QUERY_TRUNCATE_CHARS, _QUERY_TRUNCATE_CHARS, dbname))
        rows = cur.fetchall()
    except psycopg2.Error as exc:
        record["chain_error"] = str(exc)
        return
    chains: list[dict[str, object]] = []
    idle_blocker_count = 0
    fnku_waits = 0
    fnku_users0_waits = 0
    max_blocker_age = 0.0
    for blocked_pid, blocked_state, blocked_wait, blocked_query, blocker_pid, blocker_state, blocker_age, blocker_query in rows:
        age = float(blocker_age) if blocker_age is not None else 0.0
        max_blocker_age = max(max_blocker_age, age)
        for_no_key_update = bool(_FNKU_RE.search(blocked_query or ""))
        on_users = bool(_USERS_RE.search(blocked_query or ""))
        users_id_zero = for_no_key_update and on_users and bool(_USERS_ZERO_RE.search(blocked_query or ""))
        blocker_idle = (blocker_state or "").startswith("idle in transaction")
        if blocker_idle:
            idle_blocker_count += 1
        if for_no_key_update and on_users:
            fnku_waits += 1
            if users_id_zero:
                fnku_users0_waits += 1
        if len(chains) < _MAX_CHAINS_PER_SAMPLE:
            chains.append(
                {
                    "blocked_pid": int(blocked_pid),
                    "blocked_state": blocked_state,
                    "blocked_wait": blocked_wait,
                    "blocked_query": blocked_query,
                    "blocker_pid": int(blocker_pid),
                    "blocker_state": blocker_state,
                    "blocker_xact_age_s": round(age, 3),
                    "blocker_query": blocker_query,
                    "for_no_key_update": for_no_key_update,
                    "on_users": on_users,
                    "users_id_zero": users_id_zero,
                    "blocker_idle_in_transaction": blocker_idle,
                },
            )
    record["blocking_chains"] = chains
    record["blocking_chain_count"] = len(rows)
    record["blocking_chains_idle_blocker"] = idle_blocker_count
    record["max_blocker_xact_age_s"] = round(max_blocker_age, 3)
    record["users_fnku_waits"] = fnku_waits
    record["users_fnku_id_zero_waits"] = fnku_users0_waits


def _sample(conn: psycopg2.extensions.connection, dbname: str) -> dict[str, object]:
    """Take one sample of the catalog views, returning a JSON-serialisable record."""
    record: dict[str, object] = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
    }
    with conn.cursor() as cur:
        cur.execute(_ACTIVITY_STATE_SQL, (dbname, _APPLICATION_NAME))
        by_state = {row[0]: int(row[1]) for row in cur.fetchall()}
        record["activity_by_state"] = by_state
        record["active_sessions"] = by_state.get("active", 0)
        record["idle_in_transaction"] = by_state.get("idle in transaction", 0)
        record["total_sessions"] = sum(by_state.values())

        cur.execute(_ACTIVITY_WAIT_SQL, (dbname, _APPLICATION_NAME))
        by_wait = {f"{row[0]}/{row[1]}": int(row[2]) for row in cur.fetchall()}
        record["activity_by_wait"] = by_wait
        # Lock waits are the wait_event_type most directly implicated by the hypothesis.
        record["active_lock_waits"] = sum(v for k, v in by_wait.items() if k.startswith("Lock/"))

        cur.execute(_LOCKS_SQL)
        granted: dict[str, int] = {}
        waiting: dict[str, int] = {}
        for mode, is_granted, count in cur.fetchall():
            target = granted if is_granted else waiting
            target[mode] = target.get(mode, 0) + int(count)
        record["locks_granted"] = granted
        record["locks_waiting"] = waiting
        record["rowsharelock_granted"] = granted.get("RowShareLock", 0)
        record["rowsharelock_waiting"] = waiting.get("RowShareLock", 0)
        record["locks_waiting_total"] = sum(waiting.values())

        cur.execute(_DEADLOCKS_SQL, (dbname,))
        deadlock_row = cur.fetchone()
        record["deadlocks"] = int(deadlock_row[0]) if deadlock_row else 0

        cur.execute(_BACKLOG_SQL)
        backlog = {row[0]: int(row[1]) for row in cur.fetchall()}
        record["backlog_text"] = backlog.get("text", 0)
        record["backlog_image"] = backlog.get("image", 0)

        _sample_users_locks(cur, record)
        _sample_relation_locks(cur, record)
        _sample_blocking_chains(cur, dbname, record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample the queue-pressure Postgres into JSONL once per second.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=15432)
    parser.add_argument("--dbname", default="postgres")
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="postgres")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between samples.")
    parser.add_argument("--duration", type=float, default=0.0, help="Stop after N seconds (0 runs until signalled).")
    parser.add_argument("--out", required=True, help="Output JSONL path.")
    args = parser.parse_args(argv)

    stop_flag = _StopFlag()
    signal.signal(signal.SIGINT, stop_flag.request_stop)
    signal.signal(signal.SIGTERM, stop_flag.request_stop)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(args)
    deadline = time.time() + args.duration if args.duration > 0 else None
    samples = 0
    print(f"[prober] sampling {args.host}:{args.port}/{args.dbname} every {args.interval}s -> {out_path}", flush=True)
    with out_path.open("a", encoding="utf-8") as handle:
        while not stop_flag.stop:
            loop_start = time.time()
            try:
                record = _sample(conn, args.dbname)
            except psycopg2.Error as exc:
                record = {"ts": time.time(), "iso": datetime.now(timezone.utc).isoformat(), "sample_error": str(exc)}
                # A dropped/timed-out connection (e.g. across a Postgres restart) is
                # recovered rather than fatal, since the run tunes and restarts PG.
                try:
                    conn.close()
                except psycopg2.Error:
                    pass
                try:
                    conn = _connect(args)
                except psycopg2.Error as reconnect_exc:
                    record["reconnect_error"] = str(reconnect_exc)
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            samples += 1
            if deadline is not None and time.time() >= deadline:
                break
            sleep_for = args.interval - (time.time() - loop_start)
            if sleep_for > 0:
                time.sleep(sleep_for)
    print(f"[prober] wrote {samples} samples to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
