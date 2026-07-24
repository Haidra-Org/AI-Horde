<!--
SPDX-FileCopyrightText: 2026 AI Power Grid

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# AIPG Core Architecture Migration Guide

> **Historical design series.** The production coordinator has already moved to
> FastAPI `/v1`, Redis Streams, and worker WebSockets. The proposed paths and
> phases below are retained as migration rationale and are not a current deploy
> or API contract.

## Overview

This guide covers migrating from the current Flask/Waitress + HTTP polling architecture to FastAPI + Redis Streams + SSE for a production-grade worker coordination system.

### Current Architecture (Problems)

```
Worker → HTTP Poll (every 1s) → Flask/Waitress → PostgreSQL query → "No jobs" → Repeat
```

- **3600 wasted requests/hour per worker** just asking "got work?"
- Synchronous Flask can't scale concurrent connections
- No LLM token streaming capability
- Database hammered with polling queries

### Target Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              AIPG CORE                                  │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────┐    ┌─────────────────────────────────────────────┐    │
│  │   Users     │    │              FastAPI (async)                │    │
│  │   & Apps    │───▶│  POST /api/v2/generate/async  (submit job)  │    │
│  │             │◀───│  GET  /api/v2/generate/{id}/stream (SSE)    │    │
│  └─────────────┘    └───────────────────┬─────────────────────────┘    │
│                                         │                               │
│                          ┌──────────────┴──────────────┐               │
│                          ▼                              ▼               │
│              ┌───────────────────┐          ┌───────────────────┐      │
│              │      Redis        │          │    PostgreSQL     │      │
│              │                   │          │                   │      │
│              │  Streams: jobs    │          │  - Users          │      │
│              │  Pub/Sub: tokens  │          │  - Workers        │      │
│              │  Cache: sessions  │          │  - Completed Jobs │      │
│              └────────┬──────────┘          └───────────────────┘      │
│                       │                                                 │
│                       │ XREAD BLOCK (no polling!)                      │
│                       ▼                                                 │
│              ┌─────────────────────────────────────────────────────┐   │
│              │                    WORKERS                           │   │
│              │  - XREAD BLOCK on Redis Streams (waits for jobs)    │   │
│              │  - PUBLISH tokens to Redis Pub/Sub (streaming)      │   │
│              │  - POST results back to API                          │   │
│              └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Migration Phases

| Phase | Duration | Description |
|-------|----------|-------------|
| 1 | 1-2 weeks | FastAPI scaffold + run parallel to Flask |
| 2 | 1 week | Redis Streams for job queue |
| 3 | 1 week | SSE + Pub/Sub for LLM streaming |
| 4 | 1 week | Worker migration + cleanup |

---

## Phase 1: FastAPI Migration

See: [01-fastapi-migration.md](./01-fastapi-migration.md)

## Phase 2: Redis Streams Job Queue

See: [02-redis-streams.md](./02-redis-streams.md)

## Phase 3: LLM Streaming (SSE + Pub/Sub)

See: [03-llm-streaming.md](./03-llm-streaming.md)

## Phase 4: Worker Migration

See: [04-worker-migration.md](./04-worker-migration.md)

## Completed Runtime Retirement

See: [05-horde-retirement.md](./05-horde-retirement.md)

---

## Quick Reference

### Key Dependencies

```txt
# requirements.txt additions
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
redis[hiredis]>=5.0.0
asyncpg>=0.29.0
sqlalchemy[asyncio]>=2.0.0
sse-starlette>=1.8.0
```

### Redis Streams Commands

```bash
# Add job to stream
XADD jobs:image * job_id "abc123" payload '{"prompt":"..."}'

# Worker reads (blocks up to 30s)
XREAD BLOCK 30000 STREAMS jobs:image $

# Acknowledge job processed
XACK jobs:image workers job_id
```

### Redis Pub/Sub Commands

```bash
# Worker publishes token
PUBLISH stream:abc123 "Hello"

# API subscribes
SUBSCRIBE stream:abc123
```
