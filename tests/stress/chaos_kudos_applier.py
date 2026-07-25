# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Applier crash-safety chaos harness for the kudos ledger.

Drives load at a running deployment while repeatedly killing the applier that
holds the quorum, then gates on the ledger being fully folded and internally
consistent once load stops.

The deployment under test is a multi-instance stack: several serving app
instances plus one dedicated instance started with ``--quorum`` that runs the
applier and is excluded from the serving rotation. The applier is killed two
ways, selected by flags: a container deployment kills and restarts the
quorum-holding container (``docker kill``/``docker start``); a host-process
deployment SIGKILLs the applier process named by a pid file and runs a supplied
restart command (the shape CI runs, where the applier is a bare ``server.py
--quorum`` process). Killing the applier mid-fold is the point of the test.
Folding is guarded by a Redis quorum key with a short TTL and a Postgres advisory
lock, so within seconds another instance takes over folding; each fold is its own
bounded transaction, so an interrupted fold commits nothing and its rows are
re-read on the next cycle. The gates assert that this failover leaves no stuck,
duplicated, or drifted state.

Gates applied after load stops:

- The admin ``drain`` command reaches quiescence (folds nothing on a final pass).
- ``snapshot`` then ``reconcile`` reports zero balance drift.
- No exact-content duplicate currency postings exist (see
  ``kudos_ops_common.count_duplicate_postings`` for why this is not a raw
  duplicate-``event_id`` count).
- No unapplied currency ledger rows and no unapplied statistics events remain.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from kudos_ops_common import (
    connect,
    count_duplicate_postings,
    count_unapplied_ledger,
    count_unapplied_stat_events,
    default_locustfile,
    discover_app_container,
    discover_quorum_container,
    run_admin_cli,
    spawn_locust,
    stop_locust,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--quorum-container",
        default=None,
        help="Name of the quorum/applier container. Default: auto-discover the single container matching --quorum-name-substring.",
    )
    parser.add_argument(
        "--kill-pid-file",
        default=None,
        help=(
            "Kill a host applier process instead of a container: the file holds the applier PID. "
            "Requires --restart-cmd and is mutually exclusive with the --quorum-container docker target."
        ),
    )
    parser.add_argument(
        "--restart-cmd",
        default=None,
        help=(
            "Shell command that restarts the host applier and refreshes --kill-pid-file with the new PID. Required with --kill-pid-file."
        ),
    )
    parser.add_argument(
        "--exec-container",
        default=None,
        help=(
            "Target that runs the admin CLI: a serving app container name, or 'local' to run it as a host "
            "process. Default: auto-discover one non-quorum app container (container deployments only)."
        ),
    )
    parser.add_argument("--quorum-name-substring", default="quorum", help="Substring identifying the quorum container (default: quorum).")
    parser.add_argument("--app-name-substring", default="horde", help="Substring identifying serving app containers (default: horde).")
    parser.add_argument("--dsn", required=True, help="Postgres URI for the direct gate queries.")
    parser.add_argument("--host", default="http://localhost:80", help="Load-balancer base URL for the load generator.")
    parser.add_argument("--duration", type=int, default=600, help="Seconds to run the kill loop (default: 600).")
    parser.add_argument("--kill-interval-min", type=float, default=20.0, help="Minimum seconds between kills (default: 20).")
    parser.add_argument("--kill-interval-max", type=float, default=60.0, help="Maximum seconds between kills (default: 60).")
    parser.add_argument("--users", type=int, default=40, help="Locust user count when spawning load (default: 40).")
    parser.add_argument("--spawn-rate", type=float, default=10.0, help="Locust spawn rate when spawning load (default: 10).")
    parser.add_argument("--bootstrap-requestors", type=int, default=4, help="Requestor keys to auto-register at load start (default: 4).")
    parser.add_argument("--bootstrap-workers", type=int, default=4, help="Worker keys to auto-register at load start (default: 4).")
    parser.add_argument("--locustfile", default=None, help="Locustfile to drive load with (default: the mixed-workload locustfile.py).")
    parser.add_argument(
        "--no-load",
        action="store_true",
        help="Do not spawn Locust; bracket an externally driven load run instead.",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=15.0,
        help="Seconds to wait after load stops before gating, so in-flight folds finish (default: 15).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned kill schedule and the commands that would run, without touching Docker or the database.",
    )
    return parser


def _plan_kill_offsets(duration: float, interval_min: float, interval_max: float, rng: random.Random) -> list[float]:
    """Return the elapsed-second offsets at which kills are scheduled."""
    offsets: list[float] = []
    elapsed = 0.0
    while True:
        elapsed += rng.uniform(interval_min, interval_max)
        if elapsed >= duration:
            break
        offsets.append(round(elapsed, 2))
    return offsets


def _log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", file=sys.stderr, flush=True)


def _kill_and_restart(container: str) -> None:
    subprocess.run(["docker", "kill", "--signal=KILL", container], check=True, capture_output=True, text=True)
    subprocess.run(["docker", "start", container], check=True, capture_output=True, text=True)


def _pid_alive(pid: int) -> bool:
    """Return whether ``pid`` is a live process, portably across POSIX and Windows."""
    if sys.platform == "win32":
        probe = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in probe.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sigkill(pid: int) -> None:
    """Forcibly terminate ``pid`` with the platform's uncatchable kill."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], check=True, capture_output=True, text=True)
    else:
        os.kill(pid, signal.SIGKILL)


def _read_pid_file(pid_file: str) -> int | None:
    """Return the PID recorded in ``pid_file``, or ``None`` if absent or malformed."""
    try:
        return int(Path(pid_file).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _kill_and_restart_process(
    pid_file: str,
    restart_cmd: str,
    *,
    death_timeout: float = 15.0,
    respawn_timeout: float = 30.0,
) -> None:
    """SIGKILL the applier PID in ``pid_file``, run ``restart_cmd``, wait for a fresh live PID.

    The restart command is responsible for launching the replacement applier and
    rewriting ``pid_file`` with its PID; this waits for the old process to die, runs
    it, then polls the pid file until it names a different live process so the next
    kill targets the replacement rather than a stale PID.
    """
    old_pid = _read_pid_file(pid_file)
    if old_pid is None:
        raise SystemExit(f"No readable PID in {pid_file!r}; cannot kill the applier process.")
    if _pid_alive(old_pid):
        _sigkill(old_pid)
    deadline = time.monotonic() + death_timeout
    while _pid_alive(old_pid) and time.monotonic() < deadline:
        time.sleep(0.2)

    subprocess.run(restart_cmd, shell=True, check=True)

    deadline = time.monotonic() + respawn_timeout
    while time.monotonic() < deadline:
        new_pid = _read_pid_file(pid_file)
        if new_pid is not None and new_pid != old_pid and _pid_alive(new_pid):
            return
        time.sleep(0.3)
    raise SystemExit(f"Applier did not respawn a live PID in {pid_file!r} within {respawn_timeout}s.")


def _validate_target(args: argparse.Namespace) -> None:
    """Reject flag combinations that mix the container and host-process kill targets."""
    if args.kill_pid_file is not None:
        if args.restart_cmd is None:
            raise SystemExit("--kill-pid-file requires --restart-cmd.")
        if args.quorum_container is not None:
            raise SystemExit("--kill-pid-file and --quorum-container are mutually exclusive kill targets.")
    elif args.restart_cmd is not None:
        raise SystemExit("--restart-cmd is only valid with --kill-pid-file (the host-process kill target).")


def _run_dry_run(args: argparse.Namespace, process_target: bool, target_label: str, exec_container: str, offsets: list[float]) -> int:
    if process_target:
        kill_commands = {
            "kill": f"SIGKILL the PID recorded in {args.kill_pid_file}",
            "restart": args.restart_cmd,
        }
        cli_prefix = f"{sys.executable} tools/kudos_ledger_admin.py"
    else:
        kill_commands = {
            "kill": f"docker kill --signal=KILL {target_label}",
            "restart": f"docker start {target_label}",
        }
        cli_prefix = f"docker exec {exec_container} python tools/kudos_ledger_admin.py"
    plan = {
        "dry_run": True,
        "kill_target": target_label,
        "exec_container": exec_container,
        "duration_seconds": args.duration,
        "planned_kills": len(offsets),
        "kill_offsets_seconds": offsets,
        "load": "external (--no-load)" if args.no_load else "locust",
        "commands": {
            **kill_commands,
            "drain": f"{cli_prefix} drain",
            "snapshot": f"{cli_prefix} snapshot",
            "reconcile": f"{cli_prefix} reconcile <snapshot_id>",
        },
        "gate_queries": [
            "SELECT count(*) FROM kudos_ledger WHERE applied = false",
            "SELECT count(*) FROM kudos_stat_events WHERE applied = false",
            "exact-content duplicate currency postings",
        ],
    }
    print(json.dumps(plan, indent=2))
    return 0


def _gate(exec_container: str, dsn: str) -> dict:
    """Run the post-load gates and return a structured result with a pass flag."""
    drain = run_admin_cli(exec_container, ["drain"])
    # A second drain confirms quiescence: the first may fold a residual backlog,
    # so quiescence is proven only when a pass folds nothing.
    drain_final = run_admin_cli(exec_container, ["drain"])
    snapshot_id = run_admin_cli(exec_container, ["snapshot"])["snapshot_id"]
    reconcile = run_admin_cli(exec_container, ["reconcile", snapshot_id])
    drifts = reconcile.get("drifts", [])

    conn = connect(dsn)
    try:
        duplicate_postings = count_duplicate_postings(conn)
        unapplied_ledger = count_unapplied_ledger(conn)
        unapplied_stat_events = count_unapplied_stat_events(conn)
    finally:
        conn.close()

    checks = {
        "drain_reached_quiescence": drain_final["folded"] == 0,
        "reconcile_zero_drift": len(drifts) == 0,
        "no_duplicate_postings": duplicate_postings == 0,
        "no_unapplied_ledger": unapplied_ledger == 0,
        "no_unapplied_stat_events": unapplied_stat_events == 0,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "detail": {
            "first_drain_folded": drain["folded"],
            "final_drain_folded": drain_final["folded"],
            "snapshot_id": snapshot_id,
            "drift_count": len(drifts),
            "drifts": drifts,
            "duplicate_postings": duplicate_postings,
            "unapplied_ledger": unapplied_ledger,
            "unapplied_stat_events": unapplied_stat_events,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _validate_target(args)
    process_target = args.kill_pid_file is not None
    rng = random.Random()
    offsets = _plan_kill_offsets(args.duration, args.kill_interval_min, args.kill_interval_max, rng)

    if process_target:
        target_label = f"process pid-file {args.kill_pid_file}"
    else:
        target_label = args.quorum_container or discover_quorum_container(args.quorum_name_substring)

    if args.dry_run:
        exec_container = args.exec_container or ("local" if process_target else "<auto-discovered-app-container>")
        return _run_dry_run(args, process_target, target_label, exec_container, offsets)

    if process_target:
        if args.exec_container is None:
            raise SystemExit("--kill-pid-file requires --exec-container (e.g. 'local'); there is no container to auto-discover.")
        exec_container = args.exec_container
    else:
        exec_container = args.exec_container or discover_app_container(args.app_name_substring, args.quorum_name_substring)
    locustfile = args.locustfile or default_locustfile()

    load_proc = None
    if not args.no_load:
        _log(f"Spawning Locust against {args.host} with {args.users} users at {args.spawn_rate}/s")
        load_proc = spawn_locust(
            host=args.host,
            users=args.users,
            spawn_rate=args.spawn_rate,
            locustfile=locustfile,
            bootstrap_requestors=args.bootstrap_requestors,
            bootstrap_workers=args.bootstrap_workers,
        )

    kills: list[dict] = []
    start = time.monotonic()
    try:
        for offset in offsets:
            sleep_for = offset - (time.monotonic() - start)
            if sleep_for > 0:
                time.sleep(sleep_for)
            _log(f"Killing applier {target_label} (kill {len(kills) + 1}/{len(offsets)})")
            event = {"offset_seconds": round(time.monotonic() - start, 2), "iso": datetime.now(UTC).isoformat()}
            try:
                if process_target:
                    _kill_and_restart_process(args.kill_pid_file, args.restart_cmd)
                else:
                    _kill_and_restart(target_label)
                event["ok"] = True
            except subprocess.CalledProcessError as exc:
                event["ok"] = False
                event["error"] = exc.stderr
                _log(f"Kill/restart failed: {exc.stderr}")
            kills.append(event)
        remaining = args.duration - (time.monotonic() - start)
        if remaining > 0:
            time.sleep(remaining)
    finally:
        if load_proc is not None:
            _log("Stopping Locust")
            stop_locust(load_proc)

    _log(f"Waiting {args.settle_seconds}s for in-flight folds to finish before gating")
    time.sleep(args.settle_seconds)

    _log("Running post-load gates")
    gate = _gate(exec_container, args.dsn)

    summary = {
        "kill_target": target_label,
        "exec_container": exec_container,
        "duration_seconds": args.duration,
        "kills_planned": len(offsets),
        "kills_performed": sum(1 for kill in kills if kill.get("ok")),
        "kills": kills,
        "gate": gate,
    }
    print(json.dumps(summary, indent=2))
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
