<!--
SPDX-FileCopyrightText: 2023 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Running ai-horde with Docker

You can run your own private horde with this software, for example to pool the GPUs of a team. It is free software
under the AGPL-3.0-or-later license, so you can use, modify and share it under that license's terms. Hordes do not
share workers with each other, so a private horde needs its own workers.

---

## Prerequisites

- [Install Docker](https://docs.docker.com/get-docker/)

## Build

Run the following command in your project root folder (the folder where your Dockerfile is located):

(The image name can be changed to any name you want.)

```bash
docker build -t aihorde:latest .
```

The default image intentionally omits Pyroscope's native profiling wheels. To
build an image that can run with `PYROSCOPE_ENABLED=true`, include the
`telemetry-profiling` dependency group:

```bash
docker build \
	--build-arg AI_HORDE_DEPENDENCY_GROUPS=telemetry-profiling \
	-t aihorde:telemetry .
```

The published GHCR images follow the same split: `ghcr.io/haidra-org/ai-horde:main`
is the default runtime image, while `ghcr.io/haidra-org/ai-horde:main-telemetry`
includes the profiling group.

## with Docker Compose

[docker-compose.yaml](docker-compose.yaml) is provided to run the AI-Horde with Redis and Postgres.

Copy the `.env_template` file in the root folder to create the `.env_docker` file.

```bash
cp .env_template .env_docker
```

To use the supplied `.env_template` with the supplied `docker-compose.yaml`, you will need to set:

```bash
# .env_docker
REDIS_IP="redis"
REDIS_SERVERS='["redis"]'
USE_SQLITE=0
POSTGRES_URL="postgres"
```

Before starting, stop any stack already using ports 7001, 7002, 5432 or 6379 or the container names in the Compose
file. A project name changes named-volume ownership, but the supplied container names and host ports stay fixed.
Choose the project name before startup and use it consistently for later commands.

Then run the following command in your project root folder:

```bash
# run in background
docker compose up --build -d
```

The Compose volume is mounted at PostgreSQL's version-aware parent directory.
This is required by PostgreSQL 18 and later, and keeps data for different major
versions in separate subdirectories when the development image is updated.
The API waits for the database health check before it starts.

Check the running services and API response:

```bash
docker compose ps
curl -fsS "http://localhost:7001/api/v2/status/models?type=image"
```

Expect running API, PostgreSQL and Redis containers and a JSON model list. A model list does not demonstrate that
a worker can generate; register a worker with this Horde to test a full request. If startup or the query fails,
inspect `docker compose logs aihorde postgres redis`, then check database credentials, Redis reachability and
model-reference bootstrap errors. Configure `.env_docker` before retrying startup.

Use `docker compose stop` to stop this stack while retaining its database volume. If you started with `-p <name>`,
include that same option in status, logs and stop commands.

## Optional tuning environment variables

These are all optional - the image boots with safe defaults. See
[`tests/DEPLOYMENT_CONTRACT.md`](tests/DEPLOYMENT_CONTRACT.md) for the full,
authoritative list (required vs optional) that downstream deployers rely on.

### SQLAlchemy connection pool

Each replica opens up to `SQLALCHEMY_POOL_SIZE + SQLALCHEMY_MAX_OVERFLOW`
Postgres connections. When running multiple replicas, keep the aggregate under
the database's `max_connections`.

```bash
SQLALCHEMY_POOL_SIZE=15       # base pooled connections per replica
SQLALCHEMY_MAX_OVERFLOW=5     # extra burst connections per replica
SQLALCHEMY_POOL_TIMEOUT=30    # seconds to wait for a connection before erroring
SQLALCHEMY_POOL_PRE_PING=0    # 1 to validate connections before use
```

### Telemetry (opt-in)

Telemetry activates when `AI_HORDE_TELEMETRY_ENABLED=1` or any
`OTEL_EXPORTER_OTLP_*` endpoint is set (and `OTEL_SDK_DISABLED` is not `true`).
Common knobs: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`,
`DEPLOYMENT_ENVIRONMENT`, `OTEL_INSTRUMENT_SQLALCHEMY`, `OTEL_INSTRUMENT_REDIS`,
`OTEL_TRACES_SAMPLER_ARG`, and
`PYROSCOPE_ENABLED` (requires the `-telemetry` image variant).
