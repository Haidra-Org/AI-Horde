<!--
SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
SPDX-FileCopyrightText: 2024 Tazlin <tazlin.on.github@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->

[![Discord](https://img.shields.io/badge/Discord-Haidra-5865F2?logo=discord)](https://discord.gg/3DxrhksKzn)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=fff)](https://python.org)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue)](LICENSE)
[![CI](https://github.com/Haidra-Org/AI-Horde/actions/workflows/maintests.yml/badge.svg)](https://github.com/Haidra-Org/AI-Horde/actions/workflows/maintests.yml)
[![Docker](https://img.shields.io/badge/Docker-ghcr.io-blue?logo=docker)](https://github.com/Haidra-Org/AI-Horde/pkgs/container/ai-horde)
[![Version](https://img.shields.io/github/v/release/Haidra-Org/AI-Horde?sort=semver&logo=github)](https://github.com/Haidra-Org/AI-Horde/releases)
[![Code style](https://img.shields.io/badge/code%20style-ruff-000000)](https://github.com/astral-sh/ruff)

# AI-Horde — Community-Powered Distributed AI Inference

The [AI Horde](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#ai-horde) is a free, community-run, crowdsourced distributed inference cluster for generative AI. Like Folding@home for AI, volunteers share GPU compute via [workers](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#worker) to serve image generation, text generation, and image alchemy to anyone — for free.

The system uses [kudos](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/kudos.md) — a non-monetary priority point — to keep things fair. Workers earn kudos by contributing compute; users spend kudos for faster service. Kudos can never be bought or sold. Read about [why we built this](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/why.md).

---

## Who are you?

**New to the Horde?** → [Use the public instance](https://aihorde.net) with an OAuth2 account (Google, Discord, GitHub), a pseudonymous account, or [anonymously](https://github.com/Haidra-Org/haidra-assets/blob/main/docs/definitions.md#anonymous). See the [public performance dashboard](https://grafana.aihorde.net/d/jSb16YLVk/performance?orgId=1).

**Running a worker?** → [Image workers](https://github.com/Haidra-Org/horde-worker-reGen) | [Text workers](https://github.com/Haidra-Org/AI-Horde-Worker) | [KoboldAI](https://github.com/henk717/KoboldAI) | [KoboldCPP](https://github.com/lostruins/koboldcpp) | [Aphrodite Engine](https://github.com/PygmalionAI/aphrodite-engine) | [TabbyAPI](https://github.com/theroyallab/tabbyAPI)

**Building an integration?** → [REST API docs](#api-documentation) | [Integration guide](README_integration.md) | [Return codes](README_return_codes.md) | SDKs: [Python](https://github.com/Haidra-Org/horde-sdk), [JavaScript](https://github.com/ZeldaFan0225/ai_horde)

**Deploying your own instance?** → [Docker Compose](#deployment) | [Configuration reference](.env_template) | [Env contract](tests/DEPLOYMENT_CONTRACT.md)

**Contributing code?** → [Quick start](#local-development) | [CONTRIBUTING.md](CONTRIBUTING.md) | [Good first issues](https://github.com/Haidra-Org/AI-Horde/labels/good%20first%20issue)

---

## Quick Start

### Prerequisites

Python 3.12+, PostgreSQL 15+ (or SQLite for development), Redis 7+.

### Local Development

```bash
git clone https://github.com/Haidra-Org/AI-Horde.git
cd AI-Horde
cp .env_template .env
pip install uv && uv sync
python server.py -vvvvi --horde stable
```

Open [http://localhost:7001](http://localhost:7001) — the Swagger UI is at `/api`.

### Key CLI Arguments

| Arg | Default | Description |
|---|---|---|
| `-v` / `-vv` / `-vvv` | 0 | Increase logging verbosity |
| `-q` / `--quiet` | 0 | Decrease logging verbosity |
| `-i` / `--insecure` | — | Serve HTTP (dev only; breaks OAuth) |
| `-p` / `--port` | 7001 | Listen port |
| `--listen` | 0.0.0.0 | Bind address |
| `--horde` | stable | Horde mode (stable, etc.) |
| `--quorum` | — | Force this node as primary quorum |
| `--test` | — | Run sandbox self-test and exit |
| `--check_prompts` | — | Clean up stale prompts and exit |
| `--force_subscription` | — | Force kudos update for a user |
| `--waitress_threads` | 45 | WSGI worker threads |
| `--waitress_connection_limit` | 1024 | Max concurrent connections |

---

## Architecture

```
┌──────────────┐     ┌───────────────────────────┐     ┌──────────────┐
│   Clients    │     │     AI-Horde Server        │     │   Workers    │
│  (Users &    │────▶│    (This Repository)       │────▶│  (GPU Nodes) │
│  Integrations) │     │                           │     │              │
└──────────────┘     │  Flask REST API (Swagger)  │     └──────────────┘
                     │  + Background Threads       │
                     │  + PostgreSQL (SQLAlchemy)  │
                     │  + Redis (cache / quorum)   │
                     │  + S3/R2 Object Storage     │
                     └───────────────────────────┘
```

1. **Submit** — Client sends a request to `/api/v2/generate/async` (or `/text/async`, `/interrogate/async`).
2. **Queue** — Horde validates, deducts kudos, inserts a `WaitingPrompt` into PostgreSQL with priority ordering.
3. **Pop** — Workers poll `/api/v2/generate/pop`. Horde returns the highest-priority matching job.
4. **Submit** — Worker runs inference locally and posts results back via `/api/v2/generate/submit`.
5. **Retrieve** — Client polls `/api/v2/generate/status/{id}` until `done=true`.

### Core subsystems

| Layer | Technology |
|---|---|
| Web | Flask 2.2 + flask-restx (auto-generated Swagger) |
| WSGI | Waitress (with Prometheus-compatible metrics) |
| Database | PostgreSQL 15+ (primary), SQLite (dev/test), SQLAlchemy 2.0 |
| Cache | Redis 7 (queue state, rate limits, quorum coordination) |
| Storage | S3-compatible (Cloudflare R2, AWS S3, MinIO) |
| Auth | flask-dance (Google, Discord, GitHub OAuth2) |
| Telemetry | OpenTelemetry / Logfire (opt-in), Pyroscope (opt-in) |

---

## Features

- **Image Generation** — SD 1.5/2.x, SDXL, Flux, SD3, Qwen, Stable Cascade, LCM, img2img, inpainting/outpainting, ControlNet, LoRAs, TI embeddings, hires fix, tiling
- **Text Generation** — LLMs via KoboldAI, Aphrodite, TabbyAPI, vLLM; any HuggingFace-compatible model; context up to 1M tokens
- **Image Alchemy** — Interrogation (CLIP, BLIP), upscaling (RealESRGAN, NMKD), face restoration (GFPGAN), background removal, control map extraction
- **Kudos Economy** — NN-based pricing model, fair priority queue, non-transferable
- **Shared Keys** — Delegate priority with expiring or kudos-limited tokens
- **Styles** — Named, shareable generation presets for image & text
- **Teams** — Organize workers into self-managed groups
- **Webhooks** — Receive results asynchronously via POST callbacks
- **OAuth2** — Google, Discord, GitHub login + pseudonymous + anonymous access
- **Content Safety** — Regex-based CSAM prevention, NSFW censorlists, AI anti-CSAM
- **Rate Limiting** — Redis-backed distributed rate limiting (Flask-Limiter)
- **Horizontal Scaling** — Multiple middleware nodes via Redis quorum, shared-nothing architecture

---

## API Documentation

Full REST API documentation is auto-generated at `/api` (Swagger UI). All endpoints are prefixed with `/api/v2/`.

Key endpoint groups: image generation (`/generate/*`), text generation (`/generate/text/*`), alchemy (`/interrogate/*`), users, workers, teams, shared keys, styles, and status. See the [integration guide](README_integration.md) for workflow examples and [return codes](README_return_codes.md) for error handling.

---

## Deployment

### Docker

```bash
docker build -t aihorde:latest .
docker run -d --name aihorde -p 7001:7001 aihorde:latest
```

A `-telemetry` variant with profiling support is available (see [README_docker.md](README_docker.md)).

### Docker Compose

```bash
cp .env_template .env_docker
# Set REDIS_IP="redis", REDIS_SERVERS='["redis"]', USE_SQLITE=0, POSTGRES_URL="postgres"
docker compose up --build -d
```

Full environment variable reference: [.env_template](.env_template) and [DEPLOYMENT_CONTRACT.md](tests/DEPLOYMENT_CONTRACT.md).

---

## Testing

```bash
uv sync --group dev
pytest                    # All tests
pytest tests/unit         # In-process unit tests (kudos, DB, etc.)
pytest tests/integration  # Real PostgreSQL + Redis (testcontainers)
```

Unit tests in `tests/unit/` (16 files), integration tests in `tests/integration/` (14 files), load testing in `tests/stress/` (Locust). See [CI workflows](.github/workflows/) for the full pipeline (linting via ruff, type checking via mypy, REUSE compliance).

---

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

**Quick checklist:**
1. Open a [feature request](https://github.com/orgs/Haidra-Org/projects/14) first
2. Install dev tools: `uv sync --group dev && pre-commit install`
3. Lint: `ruff . --fix && ruff format .`
4. REUSE: `reuse lint` (every file must have an SPDX header)
5. Type check: `mypy` (strict on `horde.telemetry` and `horde.metrics`)
6. All contributors must follow the [Anarchist Code of Conduct](https://wiki.dbzer0.com/the-anarchist-code-of-conduct/)

Looking for a place to start? Check [good first issues](https://github.com/Haidra-Org/AI-Horde/labels/good%20first%20issue).

---

## Community

- **Discord** — [Join the Haidra Discord](https://discord.gg/3DxrhksKzn) — primary community hub
- **Patreon** — [Support development](https://www.patreon.com/db0)
- **GitHub Sponsors** — [Sponsor db0](https://github.com/db0)
- **Mastodon** — [@stablehorde@sigmoid.social](https://sigmoid.social/@stablehorde)
- **Bug Reports** — [GitHub Issues](https://github.com/Haidra-Org/AI-Horde/issues)

---

## Sponsors

[![NLnet logo](assets/logo_nlnet.svg)](https://nlnet.nl/project/AI-Horde/)

The AI Horde is supported by the [NLnet Foundation](https://nlnet.nl/project/AI-Horde/) through the [NGI0 Entrust Fund](https://nlnet.nl/entrust/), and by our community via [Patreon](https://www.patreon.com/db0) and [GitHub Sponsors](https://github.com/db0).

---

## License

Copyright © 2022 Konstantinos Thoukydidis, 2023–2026 Tazlin and contributors.

Licensed under the **GNU Affero General Public License v3.0 or later** — see [LICENSE](LICENSE) for the full text. All files carry [REUSE](https://reuse.software/) 3.0 compliant SPDX headers.
