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

## with Docker-compose

Create `.env_docker` file to deliver access information of services used together such as Redis and Postgres.

Copy the `.env_template` file in the root folder to create the .env_docker file.

[docker-compose.yaml](docker-compose.yaml) Change the file as needed.


```bash
# run in background
docker-compose up -d
```
