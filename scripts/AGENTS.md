# grid-core scripts

## Purpose

Operational and developer entrypoints that sit outside the Python packages.
This directory includes the live hourly payout wrapper, Grid-native checks,
account provisioning tools, and an incomplete testnet model-registry helper.

## Ownership

- `payout_hourly.sh` - production systemd timer entrypoint for custodial AIPG
  payouts and failed-payout reconciliation.
- `deploy_model_registry.py` - incomplete Base Sepolia ModelRegistry scaffold;
  not the production Grid Diamond deployment path.
- `run_tests.sh` - Grid-native retirement, lint, and offline test wrapper.
- `check_retired_runtime.py` - CI guard against reintroducing the retired
  runtime through code, packaging, deploy, or operator scripts.
- `create_service_account.py` - one-time provisioning for bounded frontend or
  backend service principals; prints the new key exactly once.
- `rotate_service_key.py` - atomically revokes a service's old keys and prints
  one replacement key exactly once.

## Local Contracts

- `payout_hourly.sh` moves real funds because it always passes `--send`. Do not
  run, edit, or repoint it casually. Preserve UTC period boundaries, the
  caller-injected environment, payout idempotency, receipt verification, and
  the retry step.
- Systemd owns `/etc/aipg/grid.env`; do not source that file from the shell
  wrapper or print its values.
- The payout wrapper resolves Python from its own immutable release directory;
  never point it back at the historical mutable production checkout.
- `deploy_model_registry.py` is a scaffold with no compiled deployment path.
  Never use it for Base mainnet or describe it as the canonical registry tool.

## Work Guidance

- Money-path changes belong primarily in
  `grid_api/services/settlement/payouts.py`; keep this wrapper thin.
- Add explicit dry-run defaults to any new chain, database, or cleanup tool.
- Put reusable logic in the owning service package and test it there.
- Invoke Python operator tools through the selected release's `.venv/bin/python`;
  their env-based shebang assumes an already activated virtual environment.

## Verification

- Run `bash -n scripts/payout_hourly.sh` for wrapper edits.
- Run focused settlement tests before changing payout invocation or periods.
- Run `scripts/run_tests.sh` from an environment with
  `requirements.dev.txt` installed.
- Run `git diff --check` and inspect commands for leaked secrets.

## Child DOX Index

No child guides are currently required; this file owns `scripts/`.
