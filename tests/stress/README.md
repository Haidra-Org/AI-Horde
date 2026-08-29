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
- `locustfile_reference_churn.py` mixes control and changing image models while
  an external driver updates the served reference.

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

  `bootstrap_garage.sh` discovers these mappings from the running Compose
  containers, so bootstrap remains correct when Compose is started in
  PowerShell and the bootstrap script runs through WSL. To load the resulting
  service environment into PowerShell, run:

  ```powershell
  & bash tests/bootstrap_garage.sh --powershell |
      Out-String |
      Invoke-Expression
  ```

  POSIX shells can continue to use
  `eval "$(bash tests/bootstrap_garage.sh --shell)"`.

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

The mixed workload contains separate pre-17 and bridge-17+ image worker
populations. Pre-17 workers actively fail the Locust run if they receive an
extended sampler, an explicit scheduler, a solver-control field, or
`flow_shift`; bridge-17+ workers serve those jobs. Both populations always
offer `stable_diffusion`, and the newer population also offers the default Flux
model, while their remaining model sets vary normally.

`SamplerFeatureRequester` walks every extended sampler using no optional
settings and every applicable setting. Where at least two settings apply, it
also selects a non-empty proper subset; the same zero/subset/all pattern covers
the Flux `flow_shift` profile. Applicable solver controls come from the installed
`horde_sdk` constraints rather than a second Locust-owned compatibility table.
Use `--sampler-feature-requestors 1 --legacy-image-workers 1
--extended-image-workers 1` for a deterministic smoke population. Meta users
also fetch `/api/v2/status/sampler_constraints` as ordinary read-only traffic.

`BaselineFeatureRequester` sets one or two per-baseline features (`hires_fix`,
`transparent`, the `qr_code` workflow, `flow_shift`, and `remix`
source-processing) on a model whose baseline it knows, and marks the response
against the rejection `horde.baseline_policy` predicts for it. Accepting a
request the policy table refuses, or refusing one it allows, fails the run. The
baselines come from the `_MODEL_BASELINES` map in `locustsuite/config.py`;
models absent from it are skipped rather than guessed at. Use
`--baseline-feature-requestors 1` for a deterministic population.

The reference-churn workload is normally launched by
`tests/integration/test_reference_refresh_under_locust_traffic.py`. That test
drives seven temporal epochs: control traffic, baseline-only publication, a
model on an existing baseline, an interleaved baseline-plus-model publication,
a model-category outage, policy recovery, and model retirement. Four requesters
and three workers perform real async/pop/submit/check/status round trips at low,
medium, burst, and heavy load. Requester evidence independently checks the
requested model, terminal state, worker attribution, and single-generation
cardinality; Locust's CSV remains the transport/error oracle. The parent test
also holds dedicated assignments across a policy update and model removal to
prove that already-popped work still completes with its original model and
requester-visible identity. Completion-time repricing is observed but is not a
contract asserted by this scenario.

The integration driver writes an atomic JSON epoch configuration and consumes
the workload's JSONL evidence. Requesters cycle through each epoch's models, so
per-model submission coverage is deterministic while compatible pending work is
kept across epoch boundaries. That backlog deliberately keeps old request polls,
worker lookups, and new-epoch traffic overlapping reference publication. Models
retired from the next epoch are cancelled, while the parent test's dedicated held
assignments exercise work across policy updates and model removal. A manual run
therefore needs
`LOCUST_REFERENCE_API_KEY`, `LOCUST_REFERENCE_EPOCH_CONFIG`, and
`LOCUST_REFERENCE_EVIDENCE`; the config must contain `epoch`,
`request_models`, and `worker_models`, with the optional batch, probability,
delay, and pending-limit fields used by the integration test. Then run:

```
locust -f tests/stress/locustfile_reference_churn.py \
    --host http://localhost:7001 --headless --users 4 --spawn-rate 4
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

## Kudos ledger operational verification

Three harnesses verify the kudos applier and its ledger cutover against a running
deployment rather than in unit isolation. They assume a multi-instance stack:
several serving app containers behind a load balancer, plus one dedicated
container started with `--quorum` that runs the applier and is excluded from the
serving rotation. The applier folds unapplied ledger rows on the quorum node
every few seconds; a Redis quorum key with a short TTL plus a Postgres advisory
lock mean that if the quorum container dies another instance takes over folding
within seconds, and each fold is its own bounded transaction. The harnesses drive
the shared `locustfile.py` workload (or bracket an externally driven run with
`--no-load`) and gate on Postgres truth and the admin CLI in
`tools/kudos_ledger_admin.py`, run via `docker exec`. Container names are
auto-discovered by substring (`--quorum-name-substring`, `--app-name-substring`)
or given explicitly; `--dsn` is the Postgres URI used for the direct gate
queries. The example ports below are placeholders for a local stack.

- `chaos_kudos_applier.py` drives load while repeatedly `docker kill`ing and
  restarting the quorum/applier container on a random interval, then, once load
  stops, gates that the ledger folded cleanly despite the interruptions. The
  gates are: the admin `drain` reaches quiescence (a final pass folds nothing),
  `snapshot` then `reconcile` reports zero balance drift, no exact-content
  duplicate currency postings exist, and no unapplied ledger rows or statistics
  events remain. It prints a JSON summary and exits nonzero on any gate failure.
  `--dry-run` prints the planned kill schedule and the commands it would run
  without touching Docker or the database.

  ```
  python tests/stress/chaos_kudos_applier.py --dsn postgresql://horde@localhost:5432/horde \
      --host http://localhost:80 --duration 600 --kill-interval-min 20 --kill-interval-max 60
  ```

  The duplicate check is deliberately not a raw duplicate-`event_id` count:
  postings of one business event share an `event_id` by design (an escrow drain
  and a balance transfer each emit two postings under one `event_id`), so that
  count is nonzero in healthy operation. The gate instead counts postings that are
  identical in their full accounting content, which is what a re-inserted event
  from a botched retry would produce.

- `check_kudos_telemetry_accuracy.py` gates the applier's `folded` OTLP counter
  against database truth. Run `begin` to record the counter and the applied-row
  counts, drive load, stop it, then run `end`. It discovers the metric's
  OTLP-translated name through the Prometheus `/api/v1/series` endpoint rather
  than assuming one spelling, sums the counter across instances per `row_type`,
  waits for the counter to settle across two samples `--settle-seconds` apart
  (covering the OTLP export interval), and gates that the counter delta equals the
  applied-row delta exactly for both `currency` and `stat`. Applied ledger and
  statistics rows are never purged, so those counts are a monotone truth baseline
  over the window. Floor-adjustment postings are excluded from the currency
  baseline: the applier emits them already applied while folding an overdraft,
  so they are outside the folded counter's claim (they have their own metric). `--org-id` sets the optional `X-Scope-OrgID` tenant header and
  is omitted when not given.

  ```
  python tests/stress/check_kudos_telemetry_accuracy.py begin \
      --prom-url http://127.0.0.1:9009/prometheus --dsn postgresql://horde@localhost:5432/horde
  # drive load, then stop it
  python tests/stress/check_kudos_telemetry_accuracy.py end \
      --prom-url http://127.0.0.1:9009/prometheus --dsn postgresql://horde@localhost:5432/horde
  ```

  The counter is cumulative per applier process, and folding ownership moves
  between processes (quorum handoff), so the raw metric is per-process series
  that appear, reset, and go stale. The window total is therefore computed from
  a range query with standard counter-reset handling (per series, positive
  increments are summed; a fresh series contributes its first value in full)
  rather than from point-in-time samples. Folds a process never exported before
  dying are absent from the backend and surface as a shortfall; that is a
  genuine telemetry gap to interpret, not a harness artifact.

  The whole `begin`/`end` window must be spent in ledger mode: shadow-mode rows
  are inserted already applied without passing through the applier, so a shadow
  interlude inflates the applied-row delta past the folded counter. Both
  subcommands refuse to run when the control row is not in ledger mode; a
  mid-window flip is on the operator to avoid.

- `mode_flip_rehearsal.py` drives load and flips the ledger mode through a
  sequence (`--modes`, default `shadow,ledger,shadow`), dwelling in each mode
  first. For each flip it samples the load balancer's FRONTEND `hrsp_5xx` counter
  before and after (from the HAProxy-style CSV stats endpoint), times the flip,
  and checks the ledger state the target mode requires: after a flip to `shadow`
  no unapplied ledger rows or statistics events may remain (the transition drains
  the tail inside its own transaction), while after a flip to `ledger` unapplied
  rows are healthy steady-state under load, so the gate is instead that the
  oldest pending row is younger than `--max-pending-age` (default 30s, the
  degradation threshold), proving the applier keeps up. It exits nonzero if any
  flip errored, a mode-specific gate failed, or the 5xx delta across a flip
  exceeds `--max-5xx-delta` (default 0). If the stats endpoint is unreachable the
  5xx gate is skipped with a loud warning in the report rather than silently.

  ```
  python tests/stress/mode_flip_rehearsal.py --dsn postgresql://horde@localhost:5432/horde \
      --host http://localhost:80 --haproxy-stats-url "http://127.0.0.1:8404/stats;csv" \
      --modes shadow,ledger,shadow --dwell-seconds 60
  ```

  The reported flip duration (`cli_wall_clock_seconds`) is the wall-clock time of
  the admin CLI call, which includes process (or `docker exec`) and app-context
  startup, not only the underlying `set_kudos_ledger_mode` transaction. Treat it
  as an upper bound on the control-plane write cost; the transaction itself is
  faster.

### CI regression job

The `stress-kudos-ledger-job` in `.github/workflows/prtests.yml` and
`maintests.yml` runs a time-compressed version of two of these harnesses as a
regression guard; the full-length runs above remain the operational sign-off. It
starts two host servers against the shared test-stack datastore (a serving
instance and a dedicated `--quorum` applier), then runs `mode_flip_rehearsal.py`
(`--modes shadow,ledger,shadow`, short dwells) and, after setting ledger mode,
`chaos_kudos_applier.py` (`--duration 120`, tight kill intervals). Both address
the admin CLI as a local host process rather than a container: pass
`--exec-container local`, which runs `tools/kudos_ledger_admin.py` with the
current interpreter, the repo root as cwd, and the repo root on `PYTHONPATH`
(needed because running the CLI as a script otherwise puts `tools/` at
`sys.path[0]` and the `horde` package is not pip-installed). The chaos harness
kills a host process instead of a container with `--kill-pid-file PATH` plus
`--restart-cmd '<shell>'`: it SIGKILLs the PID in the file, then runs the shell
command, which is responsible for relaunching the applier and rewriting the pid
file so the next kill targets the replacement.

`check_kudos_telemetry_accuracy.py` is deliberately excluded from CI: it gates an
OTLP counter against the database through a Prometheus/Mimir query API, and the CI
runner has no such backend (telemetry is enabled but exports nowhere). Run it
against a deployment that has one, as described above.

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

The Docker publish workflow additionally runs the mixed `locustfile.py` suite
against both built image variants. This checks the default dependency image and
the telemetry-profiling image under identical load, including native Pyroscope
span correlation in the telemetry variant.

A fourth job, `stress-kudos-ledger-job`, does not fit this checker pattern: it
starts a second `--quorum` server and gates inside the harnesses themselves. See
the CI regression job note under Kudos ledger operational verification above.

The attribution and staged sampler-sweep jobs run their server with
`HORDE_TEST_RATELIMIT_DISABLED=1`: the
oracle probes pop/declare and maintenance interleavings rather than rate-limit
behaviour, and a throttled run could reach zero violations without ever reaching
the interleavings the gate is meant to protect. It passes `--stats` to the
checker for the same reason, so a run that drove no requests fails instead of
reporting a vacuous pass. The classic smoke job leaves the limiter in force,
since the suite counts 429s as successes and the gate is crash-class only.

The hot-user convoy and queue-pressure scenarios are not in CI. Their verdicts
come from `analyze_hot_user_convoy.py` and `analyze_queue_pressure.py`, which are
descriptive rather than pass/fail, and both need a phase-aligned run directory
(prober JSONL, `phases.json`, container logs) that a shared CI runner cannot
produce meaningfully. Run them as described above. If they are added to CI later,
run the analyzer as a non-gating step and keep the run directory as an uploaded
artifact.
