# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rehearse kudos ledger mode transitions under load.

Drives load at a running deployment and flips the ledger mode (``shadow`` /
``ledger``) through a configured sequence, checking that each flip is fast, does
not induce 5xx responses at the load balancer, and leaves the ledger in the state
the target mode requires: zero unapplied rows after a flip to shadow (the
transition drains the tail), a bounded oldest-pending age after a flip to ledger
(unapplied rows are healthy steady-state there; the gate is that the applier
keeps up).

The deployment under test is a multi-instance stack: several serving app
instances plus one dedicated instance started with ``--quorum`` running the
applier. The mode is a single shared control row, so a flip issued through one
instance takes effect for all of them. The admin CLI is reached either inside a
serving app container (``--exec-container <name>``) or as a local host process
(``--exec-container local``, the shape CI runs).

The measured flip duration is the wall-clock time of the admin CLI call, which
includes process (or ``docker exec``) startup and app-context boot, not only the
underlying ``set_kudos_ledger_mode`` transaction. It is therefore an upper bound
on the control-plane write cost; the transaction itself is faster.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from datetime import UTC, datetime

import requests
from kudos_ops_common import (
    connect,
    count_unapplied_ledger,
    count_unapplied_stat_events,
    default_locustfile,
    discover_app_container,
    run_admin_cli,
    spawn_locust,
    stop_locust,
)


def sum_frontend_5xx(csv_text: str) -> int:
    """Sum ``hrsp_5xx`` across the load balancer's FRONTEND rows.

    The HAProxy-style CSV stats dump prefixes its header line with ``# ``. Only
    the FRONTEND rows carry the externally observed response-class counters, so
    BACKEND and per-server rows are ignored to avoid double counting.
    """
    header, _, body = csv_text.partition("\n")
    header = header.lstrip("# ").strip()
    reader = csv.DictReader(io.StringIO(header + "\n" + body))
    total = 0
    for row in reader:
        if (row.get("svname") or "").strip() == "FRONTEND":
            value = (row.get("hrsp_5xx") or "").strip()
            total += int(value) if value else 0
    return total


def sample_5xx(url: str) -> int | None:
    """Return the current FRONTEND 5xx total, or ``None`` if the stats endpoint is unreachable."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        return None
    return sum_frontend_5xx(response.text)


def _flip_one(exec_container: str, mode: str, dsn: str, haproxy_url: str, post_flip_delay: float) -> dict:
    """Flip to ``mode`` and gate the flip; return a structured per-flip result."""
    pre_5xx = sample_5xx(haproxy_url)
    error = None
    start = time.monotonic()
    try:
        run_admin_cli(exec_container, ["mode", mode])
    except SystemExit as exc:
        error = str(exc)
    cli_seconds = round(time.monotonic() - start, 3)

    time.sleep(post_flip_delay)
    post_5xx = sample_5xx(haproxy_url)
    delta_5xx = post_5xx - pre_5xx if pre_5xx is not None and post_5xx is not None else None

    conn = connect(dsn)
    try:
        unapplied_ledger = count_unapplied_ledger(conn)
        unapplied_stat_events = count_unapplied_stat_events(conn)
    finally:
        conn.close()

    oldest_pending_seconds = None
    if mode == "ledger" and error is None:
        oldest_pending_seconds = run_admin_cli(exec_container, ["status"]).get("oldest_pending_seconds")

    return {
        "mode": mode,
        "cli_wall_clock_seconds": cli_seconds,
        "error": error,
        "pre_5xx": pre_5xx,
        "post_5xx": post_5xx,
        "delta_5xx": delta_5xx,
        "unapplied_ledger": unapplied_ledger,
        "unapplied_stat_events": unapplied_stat_events,
        "oldest_pending_seconds": oldest_pending_seconds,
    }


def _evaluate(flip: dict, max_5xx_delta: int, max_pending_age: float) -> tuple[bool, list[str]]:
    """Return whether a flip passed and the reasons for any failure.

    The unapplied-rows invariant depends on the mode flipped to. After a flip to
    shadow, zero unapplied rows must remain: the transition drains the ledger tail
    inside its own transaction and shadow emissions are applied at emit. After a
    flip to ledger, unapplied rows are healthy steady-state under load (the applier
    folds them continuously), so the gate is instead that the oldest pending row is
    younger than the degradation threshold, proving the applier is keeping up.
    """
    reasons: list[str] = []
    if flip["error"] is not None:
        reasons.append(f"flip errored: {flip['error']}")
    elif flip["mode"] == "shadow":
        if flip["unapplied_ledger"] != 0:
            reasons.append(f"{flip['unapplied_ledger']} unapplied ledger rows remain after a shadow flip")
        if flip["unapplied_stat_events"] != 0:
            reasons.append(f"{flip['unapplied_stat_events']} unapplied stat events remain after a shadow flip")
    else:
        pending_age = flip["oldest_pending_seconds"]
        if pending_age is not None and pending_age > max_pending_age:
            reasons.append(f"oldest pending row is {pending_age:.1f}s old, exceeding {max_pending_age}s; the applier is not keeping up")
    if flip["delta_5xx"] is not None and flip["delta_5xx"] > max_5xx_delta:
        reasons.append(f"5xx delta {flip['delta_5xx']} exceeds max {max_5xx_delta}")
    return (not reasons, reasons)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    parser.add_argument("--dsn", required=True, help="Postgres URI for the unapplied-row gates.")
    parser.add_argument(
        "--haproxy-stats-url",
        default="http://127.0.0.1:8404/stats;csv",
        help="Load-balancer CSV stats endpoint (default: http://127.0.0.1:8404/stats;csv).",
    )
    parser.add_argument("--host", default="http://localhost:80", help="Load-balancer base URL for the load generator.")
    parser.add_argument(
        "--modes", default="shadow,ledger,shadow", help="Comma-separated modes to apply in order (default: shadow,ledger,shadow)."
    )
    parser.add_argument("--dwell-seconds", type=float, default=60.0, help="Seconds to dwell in each mode before flipping (default: 60).")
    parser.add_argument(
        "--post-flip-sample-delay", type=float, default=10.0, help="Seconds after a flip before sampling 5xx again (default: 10)."
    )
    parser.add_argument("--max-5xx-delta", type=int, default=0, help="Maximum tolerated 5xx increase across any flip (default: 0).")
    parser.add_argument(
        "--max-pending-age",
        type=float,
        default=30.0,
        help="Maximum age in seconds of the oldest pending row after a flip to ledger mode (default: 30, the degradation threshold).",
    )
    parser.add_argument("--users", type=int, default=40, help="Locust user count when spawning load (default: 40).")
    parser.add_argument("--spawn-rate", type=float, default=10.0, help="Locust spawn rate when spawning load (default: 10).")
    parser.add_argument("--bootstrap-requestors", type=int, default=4, help="Requestor keys to auto-register at load start (default: 4).")
    parser.add_argument("--bootstrap-workers", type=int, default=4, help="Worker keys to auto-register at load start (default: 4).")
    parser.add_argument("--locustfile", default=None, help="Locustfile to drive load with (default: the mixed-workload locustfile.py).")
    parser.add_argument("--no-load", action="store_true", help="Do not spawn Locust; bracket an externally driven load run instead.")
    return parser


def _log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    unknown_modes = sorted(set(modes) - {"shadow", "ledger"})
    if unknown_modes:
        raise SystemExit(f"Unknown mode(s) {unknown_modes}; --modes accepts a comma-separated sequence of shadow and ledger.")
    exec_container = args.exec_container or discover_app_container(args.app_name_substring, args.quorum_name_substring)
    locustfile = args.locustfile or default_locustfile()

    haproxy_reachable = sample_5xx(args.haproxy_stats_url) is not None
    if not haproxy_reachable:
        _log(f"WARNING: HAProxy stats endpoint {args.haproxy_stats_url} unreachable; the 5xx gate is skipped for this run.")

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

    flips: list[dict] = []
    try:
        for mode in modes:
            _log(f"Dwelling {args.dwell_seconds}s before flipping to {mode}")
            time.sleep(args.dwell_seconds)
            _log(f"Flipping to {mode}")
            flip = _flip_one(exec_container, mode, args.dsn, args.haproxy_stats_url, args.post_flip_sample_delay)
            passed, reasons = _evaluate(flip, args.max_5xx_delta, args.max_pending_age)
            flip["passed"] = passed
            flip["failure_reasons"] = reasons
            flips.append(flip)
    finally:
        if load_proc is not None:
            _log("Stopping Locust")
            stop_locust(load_proc)

    all_passed = all(flip["passed"] for flip in flips)
    summary = {
        "exec_container": exec_container,
        "modes": modes,
        "haproxy_stats_url": args.haproxy_stats_url,
        "haproxy_5xx_gate": "enabled" if haproxy_reachable else "skipped_unreachable",
        "flips": flips,
        "passed": all_passed,
        "note": "cli_wall_clock_seconds includes process/docker-exec and app-context startup; it is an upper bound on the mode-write cost.",
    }
    print(json.dumps(summary, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
