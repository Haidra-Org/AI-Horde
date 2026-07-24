<!--
SPDX-FileCopyrightText: 2026 AI Power Grid
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AI Power Grid core

Coordinator and economic ledger for the AI Power Grid decentralized generation
network. The production application is the FastAPI `grid_api` service: it serves
the canonical `/v1` text, image, video, account, worker, validator-preview, and
statistics APIs and dispatches jobs to workers over WebSockets.

The inherited Flask/Horde source remains under `horde/` as frozen migration
history. It is not imported, installed, or served by the Grid runtime. Legacy
`/api/v2` and `/v2` requests receive a static `410 Gone`; clients and workers
must use `/v1`.

## Current architecture

- FastAPI/uvicorn public API in `grid_api/`
- PostgreSQL for accounts, credits, reservations, ledgers, payouts, and
  validator evidence
- Redis Streams/pubsub for job dispatch and streamed results
- `/v1/workers/ws` for text and media workers
- Alembic migrations under `alembic/`
- Base integration for model/recipe/worker registry, job/reward commitments,
  payouts, and future stake/bonds

Hot inference stays off-chain. Base is used for public durable state where it
adds auditability; the request path does not wait for per-job chain writes.

## Public API

The canonical base is `https://api.aipowergrid.io`.

| Capability | Endpoint |
|---|---|
| Text chat | `POST /v1/chat/completions` |
| OpenAI Responses | `POST /v1/responses` |
| Anthropic Messages | `POST /v1/messages` |
| Image generation | `POST /v1/images/generations` |
| Video generation | `POST /v1/videos/generations` |
| Models | `GET /v1/models` |
| Worker transport | `WS /v1/workers/ws` |
| Health | `GET /health` |

Human-facing API and operator documentation lives at
`https://aipowergrid.io/docs`. Router-level behavior is documented by the code
and nested `AGENTS.md` files.

## Local development

Python, PostgreSQL, and Redis are required. Create `.env` with the `POSTGRES_*`,
`REDIS_*`, and `GRID_SALT` values used by `grid_api/config.py`, then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-grid.txt
python -m alembic upgrade head
uvicorn grid_api.main:app --reload --host 127.0.0.1 --port 7010
```

Verify `http://127.0.0.1:7010/health` and
`http://127.0.0.1:7010/v1/models`.

## Safety posture

- Demand charging defaults off with `GRID_CHARGING_ENABLED=0`; metering code
  existing does not mean charging is live.
- Free-credit spending has its own `GRID_FREE_SPENDABLE_LIVE` gate.
- Promotional-credit spending has its own `GRID_PROMO_SPENDABLE_LIVE` gate.
- The live worker reward bridge is the custodial, Transfer-verified AIPG payout
  sender. Multi-asset pass-through is dark. Reward claim facets are deployed on
  Base, but the Merkle publisher/claim operation is not the live payout rail.
- Validator assignments and scorecards are preview evidence only. They do not
  currently affect payouts, routing, strikes, or slashing.

## Repository map

- `grid_api/` - production coordinator and APIs
- `alembic/` - Grid database migrations
- `deploy/` - immutable-release bootstrap, service, Nginx, and runbook assets
- `docs/` - architecture, economics, verification, and integration docs
- `recipes/`, `styles/` - curated media policy/catalog data
- `core-integration-package/` - contract ABIs and integration examples
- `horde/` - frozen historical source; never part of the Grid runtime

Read [AGENTS.md](AGENTS.md) before editing and follow the nested DOX chain.

## Verification

```bash
pytest grid_api/
```

For migration changes, test `python -m alembic upgrade head` against a disposable
database. See [deploy/README.md](deploy/README.md) for the existing-host deploy
sequence.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE) and [LICENSES/](LICENSES/).
