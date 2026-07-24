# deploy - production runtime wiring

## Purpose

Fresh-host and existing-host operations for the Grid-native core. Production
executes an immutable release selected through `/home/aipg/current`.

## Ownership

- `bootstrap.sh` - fresh-host bootstrap pinned to an operator-supplied full
  commit SHA. Installs only the Grid API, PostgreSQL, Redis, and Nginx.
- `env.template` - `/etc/aipg/grid.env` source of production env names.
- `README.md` - deploy/cutover/runbook notes.
- `nginx/aipg-api.conf` - Grid routes, restricted metrics, public docs/health,
  and static `410 Gone` responses for retired API paths.
- `systemd/aipg-gridapi.service` - uvicorn Grid API unit.
- `systemd/aipg-payout.{service,timer}` - custodial payout one-shot and hourly
  scheduler. The service invokes the wrapper from the selected release.

## Local Contracts

- Env names in `env.template`, systemd, code, and docs must match exactly.
- Public route split is intentional:
  - `/v1/*`, `/`, `/health`, `/docs`, and `/openapi.json` -> Grid API.
  - `/api/v2/*` and `/v2/*` -> static `410 Gone`; no legacy process.
  - `/metrics` should remain restricted by nginx.
- Secrets belong in `/etc/aipg/grid.env` with restrictive permissions, never in
  git, command argv, or logs.
- Deployment scripts may be destructive on fresh VMs. Do not run them locally
  from an agent without explicit user approval.

## Work Guidance

- When adding services, document ports, health checks, restart behavior, and
  firewall/nginx impact.
- `GRID_SALT` stays server-side. The developer console has no local DB/salt path
  and must not receive it.
- If you rename Base/contract env vars, update `docs/`, `grid_api/services/*`,
  and any SDK examples in the same change.

## Verification

- `nginx -t` on target host after nginx changes.
- `systemd-analyze verify` on target host when changing units.
- Local docs-only safety: `git diff --check`.

## Child DOX Index

- None - leaf.
