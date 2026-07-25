# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared helpers for the kudos ledger operational verification harnesses.

The three harnesses (``chaos_kudos_applier.py``, ``mode_flip_rehearsal.py``, and
``check_kudos_telemetry_accuracy.py``) target a multi-instance deployment of the
serving app plus one instance dedicated to the kudos applier (started with
``--quorum`` and excluded from the serving rotation). Either can be addressed two
ways: a container deployment reached through ``docker exec``, or host processes
reached directly (the shape CI runs). This module holds the pieces more than one
harness needs: container discovery, invoking the ledger admin CLI (in a container
or locally) and parsing its JSON, direct Postgres gate queries, and headless
Locust control.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import psycopg2

# The admin CLI is invoked at this path relative to the repo root, which is also
# where it lives inside the app image.
_ADMIN_CLI_PATH = "tools/kudos_ledger_admin.py"
# The image carries the repo tree at this root without pip-installing the horde
# package, and running the CLI as a script puts the tools directory, not the
# repo root, at the head of sys.path; the root must therefore be supplied on
# PYTHONPATH for the CLI's horde imports to resolve.
_APP_ROOT = "/app"
# Sentinel ``exec_container`` value selecting local host-process invocation of
# the admin CLI (see ``run_admin_cli``) instead of ``docker exec``. Used when the
# deployment under test is host processes rather than containers, as in CI.
LOCAL_EXEC_TARGET = "local"


def repo_root() -> Path:
    """Return the repository root, two directories above this ``tests/stress`` module."""
    return Path(__file__).resolve().parents[2]


def list_container_names() -> list[str]:
    """Return the names of the running containers via ``docker ps``."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def discover_quorum_container(name_substring: str) -> str:
    """Return the single container whose name contains ``name_substring``.

    The applier runs on exactly one quorum-holding container, so more than one
    match is treated as an ambiguous target rather than guessed at.
    """
    matches = sorted(name for name in list_container_names() if name_substring in name)
    if len(matches) != 1:
        raise SystemExit(
            f"Expected exactly one container name containing {name_substring!r}, found {len(matches)}: {matches}. "
            "Pass the container explicitly.",
        )
    return matches[0]


def discover_app_container(app_substring: str, quorum_substring: str) -> str:
    """Return one serving app container: name contains ``app_substring``, not ``quorum_substring``.

    Sibling containers of the same stack (database, cache, load balancer) often
    share the deployment's name prefix, so a name match alone cannot identify an
    app container. Each candidate is probed, in sorted order for determinism, for
    the admin CLI at its fixed in-image path, and the first container that has it
    is returned.
    """
    matches = sorted(name for name in list_container_names() if app_substring in name and quorum_substring not in name)
    for name in matches:
        probe = subprocess.run(
            ["docker", "exec", name, "test", "-e", _ADMIN_CLI_PATH],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return name
    raise SystemExit(
        f"No serving app container with {_ADMIN_CLI_PATH} found among names containing {app_substring!r} "
        f"(excluding {quorum_substring!r}); candidates probed: {matches}. Pass --exec-container explicitly.",
    )


def extract_json_object(text: str) -> dict:
    """Parse the single JSON object the admin CLI prints to stdout.

    The CLI writes exactly one ``json.dumps`` to stdout; app initialisation and
    loguru write to stderr. A whole-string parse is tried first, then the span
    from the first ``{`` to the last ``}``, so incidental leading output does not
    defeat the parse.
    """
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(stripped[start : end + 1])
    raise ValueError(f"No JSON object found in admin CLI output: {text!r}")


def run_admin_cli(exec_container: str, args: list[str], *, timeout: float = 300.0) -> dict:
    """Run ``kudos_ledger_admin.py`` and return its parsed JSON.

    ``exec_container`` selects the target: any container name runs the CLI inside
    that container via ``docker exec``; the sentinel ``LOCAL_EXEC_TARGET``
    (``"local"``) runs it as a local host process instead. Either way the JSON
    parsing and the loud ``SystemExit`` on a nonzero exit are identical, so a
    caller gate fails loudly rather than reading a partial result.

    In local mode the CLI is run with the current interpreter, cwd set to the repo
    root, and the repo root exported on PYTHONPATH. The PYTHONPATH is required for
    the same reason the container path sets it: running the CLI as a script puts
    ``tools/`` at ``sys.path[0]`` and the ``horde`` package is not pip-installed in
    the host-process layout, so its imports resolve only when the repo root is on
    the path.
    """
    if exec_container == LOCAL_EXEC_TARGET:
        root = repo_root()
        env = {**os.environ, "PYTHONPATH": str(root)}
        command = [sys.executable, _ADMIN_CLI_PATH, *args]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=str(root), env=env)
    else:
        command = ["docker", "exec", "-w", _APP_ROOT, "-e", f"PYTHONPATH={_APP_ROOT}", exec_container, "python", _ADMIN_CLI_PATH, *args]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SystemExit(
            f"Admin CLI {' '.join(args)} failed in {exec_container} (exit {result.returncode}):\n{result.stderr}",
        )
    return extract_json_object(result.stdout)


def connect(dsn: str) -> psycopg2.extensions.connection:
    """Open a read-only-style autocommit Postgres connection for gate queries."""
    conn = psycopg2.connect(dsn, connect_timeout=10)
    conn.autocommit = True
    return conn


def _scalar(conn: psycopg2.extensions.connection, sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def count_unapplied_ledger(conn: psycopg2.extensions.connection) -> int:
    """Return the number of currency ledger rows still flagged unapplied."""
    return _scalar(conn, "SELECT count(*) FROM kudos_ledger WHERE applied = false")


def count_unapplied_stat_events(conn: psycopg2.extensions.connection) -> int:
    """Return the number of statistics events still flagged unapplied."""
    return _scalar(conn, "SELECT count(*) FROM kudos_stat_events WHERE applied = false")


def count_applied_ledger(conn: psycopg2.extensions.connection) -> int:
    """Return the number of currency ledger rows flagged applied."""
    return _scalar(conn, "SELECT count(*) FROM kudos_ledger WHERE applied = true")


def count_applied_stat_events(conn: psycopg2.extensions.connection) -> int:
    """Return the number of statistics events flagged applied."""
    return _scalar(conn, "SELECT count(*) FROM kudos_stat_events WHERE applied = true")


def count_duplicate_postings(conn: psycopg2.extensions.connection) -> int:
    """Return the count of excess exact-content duplicate currency postings.

    ``event_id`` is shared by every posting of one business event by design (an
    escrow drain and a balance transfer each emit two postings under one
    ``event_id``), so a raw duplicate-``event_id`` count is nonzero in healthy
    operation and cannot gate crash safety. The crash-safety invariant this gate
    protects is that a fold interrupted and retried never leaves two postings that
    are identical in their full accounting content. Legitimate paired postings
    differ in ``amount`` sign, ``escrow`` target, or ``user_id``, so they are not
    counted; a re-inserted event surfaces as identical tuples appearing twice. The
    returned value is the number of rows in excess of one per identical tuple.
    """
    return _scalar(
        conn,
        """
        SELECT coalesce(sum(cnt - 1), 0)
        FROM (
            SELECT count(*) AS cnt
            FROM kudos_ledger
            GROUP BY event_id, entry_type, user_id, amount, escrow
            HAVING count(*) > 1
        ) AS duplicates
        """,
    )


def default_locustfile() -> str:
    """Return the path to the default mixed-workload locustfile next to this module."""
    return str(Path(__file__).resolve().parent / "locustfile.py")


def spawn_locust(
    *,
    host: str,
    users: int,
    spawn_rate: float,
    locustfile: str,
    run_time: str | None = None,
    bootstrap_requestors: int = 0,
    bootstrap_workers: int = 0,
) -> subprocess.Popen:
    """Start a headless Locust run as a child process and return the handle.

    The child inherits stderr (where Locust logs its progress) so the operator
    sees the load run, while its stdout is discarded to keep the harness's own
    JSON report clean. Bootstrap flags are passed only when positive so an
    externally provisioned key set is left untouched.
    """
    command = [
        "locust",
        "-f",
        locustfile,
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "--host",
        host,
    ]
    if run_time is not None:
        command += ["--run-time", run_time]
    if bootstrap_requestors > 0:
        command += ["--bootstrap-requestors", str(bootstrap_requestors)]
    if bootstrap_workers > 0:
        command += ["--bootstrap-workers", str(bootstrap_workers)]
    return subprocess.Popen(command, stdout=subprocess.DEVNULL)


def stop_locust(proc: subprocess.Popen, *, timeout: float = 30.0) -> None:
    """Stop a Locust child, preferring a graceful shutdown, then wait for it.

    On POSIX, SIGINT is what Locust installs a graceful-shutdown handler for; on
    platforms without it, ``terminate`` is used. A child that does not exit within
    ``timeout`` is killed so the harness never hangs on cleanup.
    """
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            proc.send_signal(signal.SIGINT)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)
