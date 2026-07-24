# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operational controls for the kudos ledger cutover and reconciliation."""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict

from horde.classes.base.kudos import (
    get_kudos_ledger_mode,
    set_kudos_ledger_mode,
)
from horde.database.kudos_ledger import apply_pending_kudos, kudos_applier_health
from horde.database.kudos_reconciliation import create_balance_snapshot, reconcile_balances
from horde.enums import KudosLedgerMode
from horde.flask import create_app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    mode = commands.add_parser("mode")
    mode.add_argument("value", choices=[str(item) for item in KudosLedgerMode])
    commands.add_parser("snapshot")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("snapshot_id", type=uuid.UUID)
    reconcile.add_argument("--apply", action="store_true", help="Emit compensating postings after reporting drift")
    drain = commands.add_parser("drain")
    drain.add_argument("--max-cycles", type=int, default=10000)
    return parser


def main() -> None:
    """Run the selected non-destructive ledger administration command."""
    options = _parser().parse_args()
    app = create_app()
    with app.app_context():
        if options.command == "status":
            print(
                json.dumps(
                    {
                        "mode": str(get_kudos_ledger_mode()),
                        **kudos_applier_health(),
                    },
                    indent=2,
                ),
            )
            return
        if options.command == "mode":
            set_kudos_ledger_mode(KudosLedgerMode(options.value))
            print(json.dumps({"mode": options.value}))
            return
        if options.command == "snapshot":
            print(json.dumps({"snapshot_id": str(create_balance_snapshot())}))
            return
        if options.command == "reconcile":
            drifts = reconcile_balances(options.snapshot_id, apply_repairs=options.apply)
            print(json.dumps({"drifts": [asdict(item) for item in drifts], "repairs_emitted": options.apply}, default=str, indent=2))
            return
        if options.command == "drain":
            total = 0
            for _ in range(options.max_cycles):
                folded = apply_pending_kudos()
                total += folded
                if folded == 0:
                    break
            print(json.dumps({"folded": total, **kudos_applier_health()}, indent=2))


if __name__ == "__main__":
    main()
