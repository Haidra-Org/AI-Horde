# grid-core (deployed as system-core) - DOX root

## Purpose

AI Power Grid core runtime: FastAPI coordinator, worker dispatch, prepaid demand
billing, worker den ledger, Base-chain settlement hooks, deployment assets, and
integration SDKs.

This repository uses the DOX AGENTS.md hierarchy. Before editing, read this file,
then walk from the repository root to every target path and read each nested
AGENTS.md on that route. The nearest AGENTS.md is the local contract; parent docs
remain binding for broader rules. After meaningful changes, update the closest
owning AGENTS.md and any affected parent Child DOX Index.

## Ownership

- `grid_api/` - live v2 FastAPI Grid coordinator for `/v1`, worker WebSockets,
  credits, ledger, den, settlement, recipes, and chain sync.
- `horde/` - frozen historical Flask source retained for license and migration
  archaeology. Grid code must not import it; production does not install or run
  it. `/api/v2` retirement responses are static Nginx responses.
- `alembic/` - grid-owned database migrations. Must match `grid_api/v2/schema.py`.
- `deploy/`, `docker/`, root Docker/systemd scripts - production and local
  runtime wiring. Production selects immutable releases through
  `/home/aipg/current`; the historical mutable checkout is not deployable.
- `docs/` - architecture, economics, blockchain, and migration specs.
- `core-integration-package/` - Base contract ABIs, sample contracts, and JS SDKs
  for ModelRegistry, RecipeVault, and JobAnchor.
- `recipes/`, `styles/` - curated JSON data loaded by `grid_api.services.recipes`
  and `grid_api.services.styles`.
- `sdk/modelvault-worker-sdk/` - TypeScript SDK package for worker/model vault
  integration.
- `sql_statements/` - frozen historical SQL; not run by Grid bootstrap.
- `tests/` - inherited integration tests; `grid_api/**/tests/` owns current unit
  and router tests.
- Root-owned files include `server.py`, `server_grid_api.py`, `requirements*.txt`,
  `pyproject.toml`, `Dockerfile`, `docker-compose.yaml`, root READMEs, assets,
  and one-off utility scripts.

## Local Contracts

- Keep hot inference off-chain. Use Base for consensus-critical registry,
  staking/bonding, signed receipt roots, settlement, and audit anchors; never add
  per-request chain calls in a user request path.
- Production runs only the FastAPI `/v1` coordinator (`grid_api`); workers connect
  at `/v1/workers/ws`. Do not restore legacy key lookup, table writes, Flask
  processes, or polling routes.
- Money paths must be fail-closed in live mode, idempotent by durable refs, and
  covered by tests. `GRID_CHARGING_ENABLED=0` is dry-run; do not assume money is
  live just because billing helpers exist.
- Treat `grid_ledger` and `grid_credit_ledger` as append-only economic records.
  Do not mutate historical ledger rows except via an explicit audited migration.
- Worker/model claims are untrusted until verified by registry sync, signed
  receipts, validators, or settlement review. Do not make payout/slashing logic
  depend only on worker self-report.
- Generated caches, venvs, pyc files, zip/tar artifacts, and screenshots are not
  source contracts. Do not extend docs from generated output unless the source is
  unavailable and that fact is documented.
- Preserve SPDX/license headers where they already exist.

## Work Guidance

- Prefer `rg` / `rg --files` for exploration.
- Use the existing stack: Python/FastAPI/SQLAlchemy/Redis for core, Foundry
  or JS SDKs only inside their owning contract/SDK areas.
- Keep env names synchronized across code, `deploy/env.template`, systemd,
  docs, and examples.
- For any endpoint change, update the owning router/API doc and add a contract
  test where practical.
- For any schema change, update `grid_api/v2/schema.py`, Alembic migrations,
  affected services, and tests together.
- For any Base-chain change, update the relevant contract/ABI/SDK docs and keep
  hot-path code on cached/offline reads.
- If you add or remove a durable folder boundary, create/remove/update its
  AGENTS.md and refresh this Child DOX Index.

## Verification

- Full Python sanity: `pytest` from `system-core`.
- Dependency gate: `pip-audit -r requirements-grid.txt`; investigate every skipped
  non-PyPI dependency separately.
- Grid-focused: `pytest grid_api/`.
- Service units: `pytest grid_api/services/`.
- Router billing/settlement coverage: `pytest grid_api/routers/`.
- Legacy smoke tests live under `tests/` and may skip without external services.
- For docs-only changes, run at least `git diff --check` and inspect the DOX
  chain for stale indexes.

## Child DOX Index

- [alembic/AGENTS.md](alembic/AGENTS.md) - grid-owned DB migrations.
- [core-integration-package/AGENTS.md](core-integration-package/AGENTS.md) - Base
  contract ABIs, sample contracts, and JS SDKs.
- [deploy/AGENTS.md](deploy/AGENTS.md) - production systemd/nginx/bootstrap env.
- [docs/AGENTS.md](docs/AGENTS.md) - architecture and migration documentation.
- [grid_api/AGENTS.md](grid_api/AGENTS.md) - live FastAPI Grid coordinator.
- [horde/AGENTS.md](horde/AGENTS.md) - legacy Flask/Horde API and compatibility
  runtime.
- [recipes/AGENTS.md](recipes/AGENTS.md) - curated ComfyUI recipe JSON.
- [scripts/AGENTS.md](scripts/AGENTS.md) - payout timer entrypoint, monitoring,
  and one-off operator/developer scripts.
- [sdk/modelvault-worker-sdk/AGENTS.md](sdk/modelvault-worker-sdk/AGENTS.md) -
  TypeScript ModelVault worker SDK package.
- [sql_statements/AGENTS.md](sql_statements/AGENTS.md) - legacy SQL migrations,
  cron, and stored procedures.
- [styles/AGENTS.md](styles/AGENTS.md) - curated style preset JSON.
- [tests/AGENTS.md](tests/AGENTS.md) - legacy integration smoke tests.
