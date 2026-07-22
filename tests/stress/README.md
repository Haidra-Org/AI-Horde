<!--
SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Stress testing

The `tests/stress/` suite drives a running AI Horde deployment with
production-shaped load and checks the results. It exists to reproduce the kinds
of concurrency and attribution behaviour that only appear under realistic
traffic: a mix of image, text, and interrogation users, a hot-user request
convoy, a deliberately built queue backlog, and an attribution ground-truth
oracle that watches for inconsistent API responses.

The workload is a [Locust](https://locust.io/) suite. Locust is a black-box
load generator: it exercises the HTTP API only and never provisions the
server's own dependencies. The deployment under test and its Postgres, Redis,
and object-storage backends are stood up separately (see Prerequisites).

## Layout

The workload lives in the `locustsuite` package so the user classes, shared
helpers, and Locust event hooks stay in coherent modules:

- `locustsuite/config.py` holds shared runtime constants and the parsed config
  populated at test start.
- `locustsuite/events.py` registers every custom CLI argument (each with a
  matching `HORDE_*` environment variable), the optional API-key bootstrap, and
  the target preflight check.
- `locustsuite/users/` holds the User classes grouped by concern (image, text,
  interrogation, meta browsing, misuse, and the attribution, hot-user-convoy,
  and queue-pressure populations).
- `locustsuite/shapes.py`, `helpers.py`, and `ground_truth.py` hold the staged
  load profiles, shared request helpers, and the attribution oracle.

The `locustfile*.py` files at the top of `tests/stress/` are thin entrypoints
that re-export the User classes Locust should discover:

- `locustfile.py` is the default entrypoint (the full user mix, classic
  `-u`/`-r` operation).
- `locustfile_shaped.py` adds a staged ramp/sustain/cooldown shape selected with
  `--stress-shape-profile`.
- `locustfile_attribution.py` spawns only the adversarial-timing text users that
  the attribution oracle observes.
- `locustfile_hot_user_convoy.py` spawns the hot-user convoy populations.
- `locustfile_queue_pressure.py` spawns the queue-pressure populations.

## Prerequisites

- Install Locust into the project environment (`uv sync --dev` or if not using
  uv, `pip install locust`).
- Bring up a running AI Horde deployment for Locust to target. The suite never
  starts the server or its backends itself; it only sends HTTP requests to the
  `--host` you give it.
- Stand up the server's backends. `tests/docker-compose.yml` provides an
  optional long-lived Postgres, Redis, and Garage (S3) stack for local work:

  ```
  docker compose -f tests/docker-compose.yml up -d
  ```

  Host-published ports are configurable so the stack can coexist with other
  local services. The relevant environment variables and their defaults are:

  | Variable                       | Default |
  | ------------------------------ | ------- |
  | `AI_HORDE_TEST_POSTGRES_PORT`  | `5432`  |
  | `AI_HORDE_TEST_REDIS_PORT`     | `6379`  |
  | `AI_HORDE_TEST_GARAGE_S3_PORT` | `3900`  |
  | `AI_HORDE_TEST_GARAGE_ADMIN_PORT` | `3903` |
  | `AI_HORDE_TEST_GARAGE_RPC_PORT` | `3901` |

- For load runs where the built-in rate limiter would otherwise dominate the
  results, start the server with `HORDE_TEST_RATELIMIT_DISABLED=1`. The limiter
  logs that it is disabled at init so the state is visible in the server output.
  Leave it enabled when the point of the run is to observe rate-limit behaviour.

## Running a scenario

Custom AI Horde options are read from CLI arguments or `HORDE_*` environment
variables. Locust's own options may additionally be set in a `locust.conf`
file. Copy the example and edit it for your environment:

```
cp tests/stress/locust.conf.example tests/stress/locust.conf
```

`locust.conf` is git-ignored and holds Locust's built-in options only; the
custom AI Horde options are documented inline in `locust.conf.example` with
their environment-variable names.

Default mixed workload:

```
cd tests/stress && locust
# or, from the repo root:
locust -f tests/stress/locustfile.py --host http://localhost:7001
```

Staged load profile:

```
locust -f tests/stress/locustfile_shaped.py --stress-shape-profile smoke
```

The scenario entrypoints take fixed per-class user counts so a run reproduces
the same shape every time. The usage block at the top of each
`locustfile_*.py` lists the exact flags. In outline:

```
# Attribution oracle
locust -f tests/stress/locustfile_attribution.py --host http://localhost:7001 \
    --headless --users 40 --spawn-rate 20 --run-time 150s \
    --attribution-pairs 6 --maintenance-workers 2 \
    --attribution-evidence attribution_evidence.jsonl --csv attribution

# Hot-user lock convoy
locust -f tests/stress/locustfile_hot_user_convoy.py --host http://localhost:7001 \
    --headless --users 120 --spawn-rate 60 --run-time 300s \
    --hc-anon-requestors 60 --hc-heavy-requestors 6 --hc-workers 40 \
    --hc-status-pollers 12 --hc-kudos-users 2 \
    --hc-baseline 60 --hc-pressure 180 --hc-relief 60 --hc-n-pressure 6 \
    --csv hc --csv-full-history

# Queue pressure
locust -f tests/stress/locustfile_queue_pressure.py --host http://localhost:7001 \
    --headless --users 60 --spawn-rate 30 --run-time 300s \
    --qp-workers 20 --qp-served-requestors 8 --qp-backlog-requestors 24 \
    --qp-backlog-target 3000 --qp-baseline 60 --qp-pressure 180 --qp-relief 60 \
    --csv qp --csv-full-history
```

### Test users and API keys

Meaningful runs need requestor and worker API keys. There are three ways to
supply them:

- Pass them directly via `--requestor-api-keys` / `--worker-api-keys` (or the
  `HORDE_REQUESTOR_API_KEYS` / `HORDE_WORKER_API_KEYS` environment variables).
- Let the suite auto-register keys at test start via `--bootstrap-requestors` /
  `--bootstrap-workers`. This posts the public `/register` form and works only
  against a local deployment where reCAPTCHA is disabled. Untrusted users are
  capped at three distinct workers each, so keep `--bootstrap-workers` at least
  `ceil(worker_users / 3)`.
- Mint keys on disk ahead of time with `gen_api_keys.py`. It registers users
  through the test bootstrap endpoint `/api/v2/dev/test-user`, which requires
  the server to run with `HORDE_TEST_APIKEYS=1` and local loopback access. The
  keys print one per line and can be pasted straight into the comma-separated
  `HORDE_REQUESTOR_API_KEYS` / `HORDE_WORKER_API_KEYS` values:

  ```
  python tests/stress/gen_api_keys.py -n 20 --host http://localhost:7001
  python tests/stress/gen_api_keys.py -n 5 --role worker --out worker_keys.txt
  ```

  The auto-bootstrap path covers most runs, so `gen_api_keys.py` is only needed
  when other tooling wants the raw keys on disk.

Before spawning users, each entrypoint runs a target preflight
(`GET /api/v2/status/heartbeat`) so a misconfigured `--host` fails early rather
than as a wall of connection errors. Control it with `--skip-preflight`,
`--preflight-fail-hard`, and `--preflight-timeout`.

## Interpreting results

The suite treats operational responses (HTTP 429 rate limits, worker-contention
result codes, deliberate misuse 4xx) as successes so they do not pollute the
Locust failure table. The checker and analyzer scripts turn the raw run
artifacts into a verdict.

- `check_smoke_results.py` gates a smoke run on stability rather than
  performance. It reads the Locust `<prefix>_stats.csv` (and the matching
  `_failures.csv`) and fails only on crash signals: a 5xx, or a transport-level
  exception with no HTTP status. Unclassified benign 4xx are reported as
  warnings. A run that drove no requests at all also fails.

  ```
  python tests/stress/check_smoke_results.py --stats smoke_stats.csv
  ```

- `check_attribution_results.py` gates an attribution run on oracle evidence.
  The attribution scenario writes one JSONL record per consistency violation.
  The checker treats that evidence file as the authority. Its default mode
  passes only when zero violations were observed (the assertion that a fixed
  server never produces the inconsistent responses the scenario probes for).
  With `--expect-violations` it inverts the gate and passes only when at least
  one violation was elicited, so a run against a server known to contain the
  defect proves the scenario still exercises it. Supplying `--stats` additionally
  fails a run that drove zero requests.

  ```
  python tests/stress/check_attribution_results.py --evidence attribution_evidence.jsonl --stats attribution_stats.csv
  python tests/stress/check_attribution_results.py --evidence prefix_evidence.jsonl --expect-violations
  ```

- `analyze_hot_user_convoy.py` and `analyze_queue_pressure.py` correlate a
  run's artifacts into a phase-aligned verdict. Each reads a run directory
  containing the Locust CSV history, the Postgres prober JSONL, and a
  `phases.json` marking the baseline, pressure, and relief boundaries (the hot
  convoy analyzer also reads before/after `pg_stat_statements` snapshots; the
  queue-pressure analyzer also reads the Postgres container log). Each buckets
  every series into the three phases and prints a timeline plus a conclusion
  block. The verdict is descriptive: it states, for each element of the
  lock-convoy signature (tuple-lock queue depth on the hot rows,
  idle-in-transaction blocking chains, latency degradation of writes versus
  reads, deadlocks confined to the pressure window, and recovery in the relief
  phase), whether the run reproduced it. A partial reproduction is reported as
  exactly that per element rather than being rounded up to a confirmation.

  ```
  python tests/stress/analyze_hot_user_convoy.py --run-dir path/to/run
  python tests/stress/analyze_queue_pressure.py --run-dir path/to/run
  ```

## Postgres sampling

`pg_prober.py` produces the prober JSONL the convoy and queue-pressure analyzers
consume. It runs as its own process and samples a handful of cheap catalog views
once per second, appending one timestamped JSON object per sample. The series it
captures are the instrumentation for the lock-convoy hypothesis: `pg_stat_activity`
backend counts by state and wait event, `pg_locks` counts by mode with granted
and waiting kept separate, the `pg_stat_database` deadlocks counter, the current
waiting-prompt backlog depth via an MVCC read that takes no row locks, per-relation
tuple-lock counts for a tracked set of hot relations, and blocking chains derived
from `pg_blocking_pids` with each blocker's state and transaction age. The
sampling connection runs in autocommit with a short `statement_timeout` so a
stalled sample cannot itself pile onto the backend under study.

Its CLI defaults target a stack published on `localhost:15432`; point it at your
own deployment with `--host`, `--port`, `--dbname`, `--user`, and `--password`.

```
python tests/stress/pg_prober.py --port 15432 --duration 300 > pg_prober.jsonl
```

## Continuous integration

Three entrypoints run as parallel jobs on pull requests and on pushes to `main`
(`.github/workflows/prtests.yml` and `maintests.yml`). Each job stands up the
`tests/docker-compose.yml` stack, starts one server, bootstraps its keys through
`/register`, and gates on a checker:

| Job | Entrypoint | Gate |
| --- | ---------- | ---- |
| `stress-smoke-job` | `locustfile.py` | `check_smoke_results.py` |
| `stress-shaped-job` | `locustfile_shaped.py` (`smoke` profile) | `check_smoke_results.py` |
| `stress-attribution-job` | `locustfile_attribution.py` | `check_attribution_results.py` |

The attribution job runs its server with `HORDE_TEST_RATELIMIT_DISABLED=1`: the
oracle probes pop/declare and maintenance interleavings rather than rate-limit
behaviour, and a throttled run could reach zero violations without ever reaching
the interleavings the gate is meant to protect. It passes `--stats` to the
checker for the same reason, so a run that drove no requests fails instead of
reporting a vacuous pass. The two smoke jobs leave the limiter in force, since
the suite counts 429s as successes and the gate is crash-class only.

The hot-user convoy and queue-pressure scenarios are not in CI. Their verdicts
come from `analyze_hot_user_convoy.py` and `analyze_queue_pressure.py`, which are
descriptive rather than pass/fail, and both need a phase-aligned run directory
(prober JSONL, `phases.json`, container logs) that a shared CI runner cannot
produce meaningfully. Run them as described above. If they are added to CI later,
run the analyzer as a non-gating step and keep the run directory as an uploaded
artifact.
