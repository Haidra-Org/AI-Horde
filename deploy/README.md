# Grid core production operations

## Current status

Production serves the FastAPI `grid_api` application from a reviewed commit of
`AIPowerGrid/grid-core`. Database changes are Alembic-managed. Each deployment
gets an immutable checkout under `/home/aipg/releases/`; `/home/aipg/current` is
an atomically replaced symlink to the selected release. The historical
`/home/aipg/system-core` checkout is retained only for forensics and rollback
comparison and must never receive another in-place deployment.

`bootstrap.sh` installs this same Grid-native topology on a fresh host and
requires a reviewed full commit SHA. It does not install the retired Flask
fleet, legacy SQL cron jobs, or polling API.

This file documents updates to the existing managed host. It does not authorize
deploying from an agent or moving money.

## Preflight

1. Confirm the target commit is reviewed and `origin/main` is the intended
   source.
2. Back up PostgreSQL before schema changes.
3. Inspect pending migrations with `python -m alembic current` and
   `python -m alembic heads`.
4. Confirm `/etc/aipg/grid.env` is readable only by the service account and has
   the required current variables.
5. Record the state of `aipg-gridapi`, Redis, PostgreSQL, and any payout timer.
6. Refuse an in-place update when `git status --porcelain` is non-empty. Preserve
   that checkout for investigation and deploy a clean reviewed release instead;
   copying selected files into production creates an untestable runtime.

## Existing-host deploy

Choose and record a reviewed full commit SHA. Build it beside the live release;
do not use a branch checkout as the release identity:

```bash
COMMIT=<reviewed-full-sha>
RELEASE=/home/aipg/releases/grid-core-${COMMIT:0:8}
sudo -H -u aipg git clone https://github.com/AIPowerGrid/grid-core.git "$RELEASE"
sudo -H -u aipg git -C "$RELEASE" checkout --detach "$COMMIT"
test "$(sudo -H -u aipg git -C "$RELEASE" rev-parse HEAD)" = "$COMMIT"
test -z "$(sudo -H -u aipg git -C "$RELEASE" status --porcelain)"
sudo -H -u aipg python3 -m venv "$RELEASE/.venv"
sudo -H -u aipg "$RELEASE/.venv/bin/pip" install -r "$RELEASE/requirements-grid.txt"
sudo -H -u aipg "$RELEASE/.venv/bin/pip" check
```

Back up the Grid-owned PostgreSQL schema and prove every pending migration on a
restored scratch database before applying it to production. Source
`/etc/aipg/grid.env` only in a root/operator shell and never print its values.
After the scratch proof, run Alembic from the candidate release with the service
environment loaded, then verify `alembic current` equals its single head.

Install the versioned unit assets, select the release atomically, and restart:

```bash
sudo install -m 0644 "$RELEASE/deploy/systemd/aipg-gridapi.service" /etc/systemd/system/
sudo install -m 0644 "$RELEASE/deploy/systemd/aipg-payout.service" /etc/systemd/system/
sudo install -m 0644 "$RELEASE/deploy/systemd/aipg-payout.timer" /etc/systemd/system/
sudo ln -s "$RELEASE" /home/aipg/.current.next
sudo mv -Tf /home/aipg/.current.next /home/aipg/current
sudo systemctl daemon-reload
sudo systemd-analyze verify aipg-gridapi.service aipg-payout.service aipg-payout.timer
sudo systemctl restart aipg-gridapi
```

The payout timer is a money-moving control. Preserve its prior enabled/active
state; do not enable or start it merely because unit files were installed. The
payout wrapper derives its Python interpreter from the selected immutable
release, so Core and payout accounting cannot silently execute different trees.

`-H` is intentional: asyncpg may otherwise inspect root's PostgreSQL client
certificate path.

## Verification

```bash
systemctl status aipg-gridapi --no-pager
journalctl -u aipg-gridapi -n 200 --no-pager
curl -fsS http://127.0.0.1:7010/health
curl -fsS http://127.0.0.1:7010/v1/models
curl -fsS http://127.0.0.1:7010/v1/validator/capabilities
curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:7010/v1/validator/assignments
```

The unauthenticated validator-assignment request should return `401`. Also smoke
one authenticated non-money request through the public hostname and inspect
worker reconnects before declaring the deploy healthy.

## Money-path controls

- `GRID_CHARGING_ENABLED=0` keeps demand charging in dry-run. Do not flip it as
  part of an unrelated deploy.
- `GRID_FREE_SPENDABLE_LIVE` is a separate free-credit spending gate.
- `aipg-payout.timer` drives the live custodial worker payout CLI. Stop/restart
  it only under the settlement runbook in
  `grid_api/services/settlement/GO_LIVE.md`.
- Private payout/reporter keys stay in the service environment or secret store;
  never print them during verification.

## Rollback

Code rollback and schema rollback are separate decisions. Do not downgrade a
database by checking out old code and hoping Alembic reverses itself. Atomically
repoint `/home/aipg/current` to a retained known-good release that is compatible
with the migrated schema, restart `aipg-gridapi`, and repeat verification.
Restore a database backup only under an incident plan.

## Asset status

- `systemd/aipg-gridapi.service` is the FastAPI unit template.
- `nginx/aipg-api.conf` serves retirement responses without a legacy process.
- `bootstrap.sh` requires `GRID_CORE_COMMIT` and installs an immutable release.
