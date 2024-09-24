<!--
SPDX-FileCopyrightText: 2023 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Running ai-horde with Docker

---

## Prerequisites

- [Install Docker](https://docs.docker.com/get-docker/)

## Build

Run the following command in your project root folder (the folder where your Dockerfile is located):

(The image name can be changed to any name you want.)

```bash
docker build -t aihorde:latest .
```

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

Then run the following command in your project root folder:

```bash
# run in background
docker compose up --build -d
```
