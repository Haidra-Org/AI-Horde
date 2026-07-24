<!--
SPDX-FileCopyrightText: 2026 AI Power Grid

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Legacy Horde Runtime Retirement

## Status

The Grid-native FastAPI coordinator is the sole supported runtime. New
deployments install `requirements-grid.txt`, run `grid_api.main:app`, and expose
only the `/v1` API and its discovery endpoints. The old Flask process, polling
worker protocol, bridge authentication, and `/api/v2` application routes are
not part of the deploy contract.

The checked-in `horde/` source and `server.py` are frozen historical material.
They are not installed, imported, started, or mounted by current CI, Docker,
Compose, systemd, Nginx, or bootstrap paths. Do not modify them for new work.
Removing that source from the default branch is a separate history-preserving
repository cleanup, not a production migration requirement.

## Runtime Invariants

- `grid_api.main:app` is the only coordinator entry point.
- User, service, worker, and validator credentials resolve through Grid-owned
  tables and scoped Grid keys.
- Workers connect through the Grid WebSocket protocol.
- Client generation uses `/v1`; retired `/v2` and `/api/v2` paths return a
  static `410 Gone` without loading application code.
- Alembic is the schema authority for Grid-owned tables.
- CI runs `scripts/check_retired_runtime.py` to reject legacy imports, table
  adapters, bridge settings, and retired systemd units.

## Historical Data

A read-only production audit on 2026-07-24 found:

| Surface | Historical rows | Active in prior 30 days | Grid equivalent |
| --- | ---: | ---: | ---: |
| users / API keys | 3 | 0 | 108 keys, 74 active |
| workers | 3 | 0 | 42 workers, 38 active |
| prompts | 1 | 0 | Grid queue and ledger active |

There was no API-key hash overlap between the historical and Grid stores. One
worker name overlapped, which is not identity proof and is not migrated.
Historical tables remain read-only for auditability; they are not an
authentication, routing, quota, payout, or accounting fallback. Do not drop
them until retention, backup, and legal requirements are explicitly approved.

## Deployment Order

1. Review and pin a full commit SHA.
2. Back up PostgreSQL and the current service/Nginx configuration.
3. Run `alembic upgrade head` against the target database.
4. Install only `requirements-grid.txt`.
5. Start `aipg-gridapi.service`; do not install a Horde unit.
6. Install the Grid-only Nginx configuration.
7. Verify `/health`, `/v1/models`, worker connections, and a canary generation.
8. Verify exact and nested `/v2` and `/api/v2` requests return `410`.
9. Monitor request errors, queue depth, settlement, and worker reconnects.

Do not combine this runtime cutover with enabling demand charging, payouts,
worker identity enforcement, validator authority, or a contract deployment.
Those controls have independent rollout gates.

## Rollback

Rollback means selecting the prior reviewed Grid release and rerunning its
compatible Alembic/service configuration. It does not mean restarting the
retired Flask fleet. Database migrations must be assessed individually before
downgrading; restore from the pre-deploy backup if a destructive reversal is
required.

## Definition Of Done

- Full Grid test suite passes.
- Docker and Compose launch only `grid_api.main:app`.
- Nginx configuration validates and serves static retirement responses.
- No current worker or user-facing repo calls `/api/v2`.
- Production has no active Horde process or listening Flask port.
- Historical credentials cannot authenticate to Grid routes.
