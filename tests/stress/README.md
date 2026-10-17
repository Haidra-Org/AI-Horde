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
- `locustsuite/chat_*.py` hold the chat contract mix: request permutations,
  response checks, simulated worker behaviour, settings and one module per
  population; `locustsuite/users/chat_mix.py` exports its users. See
  [Chat contract mix](#chat-contract-mix).

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
- `locustfile_moderation.py` floods the prompt filters with operator-seeded
  nonsense tokens, reviews the evidence listing as a moderator (following
  `text_url` links and checking digests), and probes the moderation surface the
  way an attacker would. See [Moderation evidence scenario](#moderation-evidence-scenario).
- `locustfile_chat.py` drives the OpenAI-compatible chat front with streamed,
  non-streamed and impatient clients while simulated chat workers serve the
  Horde instances behind it. `run_chat_scale_demo.sh` wraps it with the
  processes it needs. See [Chat front scale demo](#chat-front-scale-demo).

## Prerequisites

- Install Locust into the project environment (`uv sync --dev` or if not using
  uv, `pip install locust`).
- Bring up a running AI Horde deployment for Locust to target. The suite never
  starts the server or its backends itself; it only sends HTTP requests to the
  `--host` you give it. `run_chat_scale_demo.sh` is the exception: it starts its
  own Horde instances and front (see [Chat front scale demo](#chat-front-scale-demo)).
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

- `check_moderation_results.py` gates a moderation evidence run on what the
  backend recorded. See [Checking the run](#checking-the-run).

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

## Moderation evidence scenario

`locustfile_moderation.py` exercises the evidence path: capture of rejected
prompts, the background move of their text to object storage, the moderator
listing with its presigned `text_url` links, and the timeout an address earns
after repeated model rejections. It never sends real abusive text. Before a run,
a moderator seeds four filters whose regexes are nonsense words (`PUT
/api/v2/filters`, each with `"replacement": ""`): one of type 10, which the
NSFW-model check strips so a prompt holding only that word is a model rejection;
and, for the two filter words, types 10 and 20 for one word and type 10 for the
other. Together the two words are one match in each group the general filter
counts (suspicion 2). Requests default to `replacement_filter: true`, which
sanitizes such a prompt with the type-10 replacements and rejects it only when
nothing is left, so both words need a type-10 filter for the flooder's filter
requests to be rejected. Pass the words
with `--moderation-model-token` and `--moderation-filter-tokens`, and a model the
deployment's reference marks NSFW with `--moderation-nsfw-model`.

`PUT /api/v2/filters` returns the existing filter when one with the same regex
and type exists, so check that none does first, keep the ids it returns, and
delete only those after the run.

Populations, each with a fixed-count flag:

| User | Flag | Behavior |
| --- | --- | --- |
| `RejectionFlooder` | `--moderation-flooders` | Sends model rejections (six of seven requests) and filter rejections with the requestor keys; counts 400 `CorruptPrompt`, 403 `TimeoutIP` and 429 as expected, keeps sending after a timeout to show it holds; an accepted prompt fails the run because the tokens are not tripping the filter. `--moderation-pad-chars` appends a random negative prompt to make each rejected prompt large. |
| `ModeratorReviewer` | `--moderation-reviewers` | Lists pages with `--moderation-moderator-api-key`, pages by cursor, fetches a stored event's `text_url`, verifies the object's SHA-256 against `text_sha256` and its three stage keys, and writes a note with `--moderation-note-chance`. |
| `ModerationProber` | `--moderation-probers` | Listing without a key and with a non-moderator key (401 or 403), a page over the limit (400), a presigned link with its object key changed (403), and a rejected prompt with a 40,000-character negative side (clipped, 400). |

The report's per-name rows (`[rejected]`, `[timed-out]`, `[rate-limited]`,
`[expired]`) are the behavioral record: with the default threshold, a flooder key
sharing one address should show its first few requests rejected and the rest
timed out within the first minute, and the reviewer should see `text_state`
move from `pending` to `stored` within seconds of capture.

The users also write the run's ground truth to a JSONL file: a `run_start`
record with the newest event id before the run, one record per flood request
(path, key hash, status, return code, timeout seconds, prompt length and
digest, send and receive times), per reviewer object fetch and per probe, and
a `run_stop` record. The file is truncated at test start.

| Flag | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--moderation-evidence-path` | `HORDE_MODERATION_EVIDENCE_PATH` | `moderation_evidence.jsonl` | Where the ground truth is written. |
| `--moderation-address-label` | `HORDE_MODERATION_ADDRESS_LABEL` | `local` | Name of this source address. Timeouts are per address, so give each load-generating host its own label and its own evidence file. |
| `--moderation-threshold-hint` | `HORDE_MODERATION_THRESHOLD_HINT` | `5` | The deployment's `HORDE_MODEL_REJECTION_TIMEOUT_THRESHOLD`, recorded for the checker's report. |

The model-rejection count is per address per hour. Space runs from one address
an hour apart, or the second run starts above the threshold and the onset
check fails.

Any filter rejection times its address out at once, and the flooders send one
filter request in seven, so a single-address run usually times out on a filter
rejection before the model threshold can trip. To test the model threshold, run
model-only with `--moderation-filter-tokens ""` (the filter task then sends
nothing). An address timeout refuses every generation request from that address,
so load that must keep generating, such as a concurrent `closedloop` run, has to
come from another address.

### Checking the run

Locust's exit code only reflects per-response expectations. The pass criterion
is `check_moderation_results.py`, run after the scenario ends:

```
python tests/stress/check_moderation_results.py \
    --evidence moderation_evidence.jsonl --host https://dev.example \
    --moderator-api-key M --stats moderation_stats.csv \
    --threshold 5 --flooders 8
```

It gathers the run's events (ids above the `run_start` id) from the moderator
listing, prints one PASS or FAIL line per check with its numbers, and exits 1
on any failure. `--evidence` takes one file per host. On a shared deployment,
`--allow-other-traffic` turns the count equalities into lower bounds.

| Group | Enabled by | What it proves |
| --- | --- | --- |
| A, API | Always | Every rejected request is an event of its reason (A1). No event older than `--grace-seconds` is still `pending` (A2). Each `text_state` has the fields the listing promises (A3). Stored objects are uncacheable, hold exactly the three stages, and match `text_sha256` and `text_chars`; clipping is bounded and flagged (A4). Per address, the model-rejection timeout engages after `--threshold` rejections, not before, every rejection past it times the address out, nothing gets through a timeout, and the first filter rejection times out (A5). Moderators are never timed out (A6). Probes are refused as expected and no response is a 5xx (A7). Reviewer fetches matched their listing (A8). With `--stats`, Locust saw no 5xx and the flooders reached the target (A9). |
| B, database | `--pg-dsn` (needs `psycopg2`) | The row invariants of each `text_state`, redaction and anonymization hold for the run's rows; no row is pending past the grace period; `ck_prompt_moderation_text_state` exists. |
| C, bucket | `--evidence-endpoint`, `--evidence-bucket`, `EVIDENCE_AWS_ACCESS_KEY_ID`, `EVIDENCE_AWS_SECRET_ACCESS_KEY` (needs `boto3`) | The run's `evidence/` objects are exactly its stored events: none missing, no orphans. |
| D, metrics | `--mimir-url` (the Prometheus API base), `--mimir-tenant`, `--mimir-selector` | Upload backlog within one batch, no upload or write failures, one countermeasure notice per address that passed the threshold, and no retention saturation over the run window unless `--expect-saturation`. |

`--flooders` is the in-flight tolerance on the threshold: requests sent
before the response that set a timeout arrived may still be rejected. A timeout
that follows a filter rejection says nothing about the model threshold, so the
onset check then tests only its upper bound.

## Chat contract mix

The chat contract mix adds chat users to the default `locustfile.py` (and
`locustfile_shaped.py`) mix, beside the image, text and interrogation users. It
is opt-in: `events.on_test_start` pins each population to its count like every
other class and leaves out one whose count is zero, since the clients need a
running OpenAI-compatible front and the workers need worker names their key
owns. With every count at zero, the default, the mix is unchanged.

| Population | Count flag | What it does |
| --- | --- | --- |
| `ChatMixFrontClient` | `--chat-mix-front-clients` | Posts the permutations in `chat_scenarios.py` to the front, streamed or not, and checks every response against the outcome the contract promises: body and stream shape, `finish_reason`, tool calls, JSON against the schema sent, the request's nonce echoed back, usage, `x_horde`, a non-null `system_fingerprint`, error envelope and `type`, `param`, `x-should-retry`, `Retry-After` and CORS. Also reads the model cards and sends Lite's CORS preflight. |
| `ChatMixNativeClient` | `--chat-mix-native-clients` | Drives the native routes: polls a chat request to the end, reuses an operation key unchanged and changed, asks for a dry run, reads the declaration gap from the status, and sends text requests to a chat worker's model and to a chat-only worker's model. |
| `ChatMixWorker` | `--chat-mix-workers` | Pops chat jobs as a persona, checks each payload against its own declaration (routing and dispatch narrowing), and answers as the client's reply marker asks, echoing the request's nonce. With `--chat-mix-worker-progress`, a conforming text reply to a job whose payload carries `stream_progress` is first posted to `/api/v2/generate/text/progress` as deltas, one per `--chat-mix-worker-progress-interval` seconds (default 1) of the generation time, under the same rules as `ChatWorkerSimulator`'s progress mode; faults, tool calls and replies that break a contract are not posted. |

The modules are `locustsuite/chat_scenarios.py` (permutations and expected
outcomes), `chat_checks.py` (response checks), `chat_worker_script.py`
(personas, replies, payload checks), `chat_mix_settings.py` (arguments,
settings, coverage gate), and one module per population
(`chat_front_client.py`, `chat_native_client.py`, `chat_worker_user.py`);
`users/chat_mix.py` exports the classes.

### Workers and their names

The run's chat workers take worker slots in order, and slot `n` takes persona
`n` modulo `--chat-mix-worker-personas` (default `conformant,limited,flaky`)
and, when names are configured, the `n`-th name, so a name always pops as the
same persona. Each persona serves its own model (`--chat-mix-model`,
`--chat-mix-limited-model`, `--chat-mix-flaky-model`), so a scenario reaches the
declaration it needs and no other worker answers it:

- `conformant` declares every feature and guarantee and takes chat jobs only.
- `limited` declares `seed` and `tools` with only `prompt_overflow_refused` and
  `tools_max_temperature` 0.5, and takes text jobs too; forced tool choices,
  `json_object` and hot tool temperatures on its model end in 404
  `no_worker_supports_features`.
- `flaky` declares every feature but `vision` and `tools_with_response_format`
  and breaks an unscripted reply's contract at `--chat-mix-flaky-rate`; a
  `strict` reply that reaches the client still has to honour the request.

On a deployment with anti-abuse limits, reuse worker names a key already owns:
`--chat-mix-worker-names` holds one name per worker slot of the whole run, and
`--chat-mix-worker-api-key` is the key that owns every one of them. The run
refuses to start with names but no key, fewer names than
`--chat-mix-workers`, or duplicate names among the active worker slots. Without names the workers generate names under the worker key pool.
No worker takes a new identity to get past a refusal: a configured name whose
pop is refused (wrong owner, too many workers, maintenance) backs off and stops
after three refusals in a row, and a generated name stops at the first. A
stopped worker fails the run through the coverage gate.

A scenario that needs a worker behaviour writes `[reply:<script>]` into its last
user message (`ReplyScript` in `chat_worker_script.py`), so the client knows the
outcome it caused, such as a 502 `ChatRequestFaulted` after three refused
replies. The aborting scenarios (an undeclared function, duplicate call ids, a
worker fault) fault three jobs each, and every fault counts as the worker's
aborted job; the Horde puts a worker into maintenance past 20 aborted jobs an
hour, 10 while the deployment is in raid mode, as dev is. They are capped by
`--chat-mix-aborting-per-hour`, across the whole run: 2 by default (6 aborted
jobs), at most 6. In a distributed run only worker process 0 sends them, so the
run's total stays the configured one; the other processes log that they skip
them. As a breaker,
a worker whose replies meant to conform are refused three times stops without
faulting the last job, so a Horde regression cannot push a worker name into
maintenance.

### Groups, tags and classification

`--chat-mix-group-weights` weights the front's scenario groups (`ordinary`,
`tools`, `structured`, `conformance`, `gap`, `misuse`, `lite`, `vision`); a
group left out weighs 0. Give a group weight 0 when the run has no worker of the
persona it needs. The `vision` group runs only with `--chat-mix-vision`, which
makes the conformant persona declare `vision` with the `base64` transport; the
worker then checks the image reached it as a JPEG data URL (prefix and JPEG
magic bytes) and answers `vision-ok`, which the scenario requires; the Horde has
to store the image at submit. Every chat task carries the `chat` tag, plus
`chat-front`, `chat-native` or `chat-worker`, so `--tags chat` runs the chat mix
alone and `--exclude-tags chat` leaves it out.

Outcomes are classified in the users, and `check_smoke_results.py` gates on
them:

- A response that breaks its scenario's expected outcome fails with a message
  starting `Contract violation`, whatever its status, as does an unexpected
  refusal of a worker's pop or submit, a native text or status call, or the
  model list. The checker gates on it.
- A deployment condition fails with a message starting `Environment
  condition`: 503 `ChatAdmissionDisabled`, `MaintenanceMode`, 502
  `HordeUnavailable`, 408 `ChatRequestTimeout`, 403 `TimeoutIP`, and any 5xx
  without an error envelope (a proxy's error page). The checker reports these in
  their own section and gates on them: the run did not exercise what it meant to.
- An intended outcome, such as a 404 declaration gap or a 502 after refused
  replies, is a success of the scenario's row. An intended 502 is also
  cross-checked on the native status (`faulted`, `fault_rc`) unless
  `--chat-mix-skip-fault-verify` is set.
- Operating conditions are successes recorded under `<row> [benign <kind>]`:
  429, 503 `FrontAtCapacity`, 403 `KudosUpfront`, 503 `queue_unavailable` to a
  `fail_fast` request (see [Fail-fast share](#fail-fast-share)), and an
  oversized upload the front cut off before the client read its 413 (`upload
  reset before 413`: the front answers from the `Content-Length` without reading
  the body and closes the connection).

At test stop a coverage gate fails the run (`[coverage] chat mix`) when a
configured persona never came online or its worker stopped itself, when no
front scenario outside the misuse group completed, or, in a run longer than 5
minutes, when a weighted group recorded no attempt.

### Anonymous share

`--chat-mix-anonymous-share` (0 to 1, default 0.1) sends that share of the
`ordinary` and `lite` scenarios without `Authorization`, as anonymous users,
under rows named `<scenario> anonymous`. The front treats a missing header as
the anonymous key and nothing else differs on the wire
(`docs/decisions/0035-keep-anonymous-access-on-the-chat-front.md`), so the
expected outcome stays the scenario's. The Horde's side differs: the anonymous
user holds no kudos and ranks last, so its queue wait is recorded apart
(`[queue_wait_seconds] <persona> anonymous`); a request above 256 tokens, the
floor of the Horde's upfront-kudos threshold (`require_upfront_kudos`), may get
403 `KudosUpfront` (benign), while one at or below it never may, so that refusal
is a contract violation there; and all anonymous traffic shares one concurrency
limit, whose 429 is benign like any rate limit. `anonymous_plain` stays as an
always-anonymous scenario.

### Fail-fast share

`--chat-mix-fail-fast-share` (0 to 1, default 0) sends that share of the
`ordinary`, `tools`, `structured`, `conformance`, `gap` and `vision` scenarios
with `horde: {"availability_mode": "fail_fast"}`, under rows named `<scenario>
fail_fast`. The `misuse` group is refused before anything is queued, and the
`lite` group keeps the body shape Lite sends, so neither gets the mode. It
measures the front's admission decision at the first status read
(`docs/reference/chat_requests.md`): the scenario's own outcome stays expected,
and a 503 `queue_unavailable` is allowed beside it as an operating condition. The
refusal has to carry `param: horde.availability_mode`, `Retry-After` and
`x-should-retry: false`, as a plain HTTP error for streamed and non-streamed
requests alike, or it is a contract violation; a `queue_unavailable` to a request
that did not ask `fail_fast` is one too. The benign rows count the refusals by
the reason the message states:

- `<row> [benign queue_unavailable no_eligible_threads]`: the pool had zero
  eligible worker threads and nothing processed the request.
- `<row> [benign queue_unavailable wait_exceeds_budget]`: the Horde's wait
  estimate exceeded the time left in the request's budget.

A refusal whose message states neither reason is counted under `<row> [benign
queue_unavailable]`.

### Stream-abandon share

`--chat-mix-stream-abandon-share` (0 to 1, default 0) makes that share of the
streamed front requests that expect a reply close the connection after
`--chat-mix-stream-abandon-chunks` (default 2) chunks carrying content or tool
calls. The client closes the socket, so the front sees the disconnect and
cancels the native request; a client that only stopped reading would not
trigger the cancel. Such a request's row is `<row> [abandoned]`, and its response
length is the body bytes received before the close. The client checks only what
it read: the line framing, the role chunk first, the values every chunk shares,
and no chunk carrying both content and tool calls. A stream that ends or carries
an error before the count is judged whole, as without the share. Each abandoned
stream is logged at INFO with its request id, content chunks, content
characters and bytes received.

The front's cancel settles a generation in progress on the content its worker
posted, so the share measures the settlement only with workers that post
progress: `--chat-mix-worker-progress` for the simulated workers, with a
generation time long enough for more deltas than the chunk count, that is a
`--chat-mix-gen-time-min` of at least the chunk count plus one, times
`--chat-mix-worker-progress-interval`. A close that lands after the worker
submitted its reply ends the request without a cancelled generation, so the
Horde can record fewer settlements than the run has `[abandoned]` rows.

The settlement is checked on the Horde, against the request ids the client
logged:

- The Horde log has a `New Cancelled Generation <generation id> worth <kudos>
  kudos, delivered by worker: <worker> for wp <request id>` line for the
  request: the worker was paid prorated kudos and the requesting user was
  charged the same.
- `processing_gens.settled_character_count` of that generation equals the
  content characters the client logged, or exceeds them by the content the
  worker posted after the last chunk the client read.
- The worker's `aborted_jobs` stays unchanged: a cancelled streamed generation
  carries no dropped-job penalty.

### Distributed runs

The chat mix runs under Locust's master and worker mode, with workers on several
hosts:

- Set every `--chat-mix-*` option, and the suite's other options such as the
  keys, on the master. Locust sends the master's custom options with each spawn
  message and they replace a worker's own command line and environment values,
  so a worker's `HORDE_CHAT_MIX_*` variables have no effect. The keys therefore
  travel to the workers over Locust's unencrypted connection; tunnel it.
- Locust's dispatcher spreads a fixed count over the worker processes unevenly
  (six chat workers over two processes can land four and two), so each chat
  worker claims its slot, name and persona from the master when it starts.
  `--chat-mix-worker-names` needs one name per chat worker of the whole run.
  The master broadcasts current persona availability from the workers' stats
  reports, so front and native clients can use personas served by another
  process. Offline workers are removed, and availability expires after 15
  seconds without fresh reports.
- Each worker process adds its coverage to its stats report every few seconds;
  the master merges the latest report of each worker and runs the coverage gate
  at test stop, where it also writes the statistics files.
- Model card checks run once per 30 seconds per process, whatever the number of
  front clients; the budgets and the ledger are per process and bounded by the
  run's worker names and limits, not by its users.
- A master cannot remove a user class once its dispatcher exists, so it gives
  unrequested opt-in classes and classes a tag filter emptied a weight of 0; a
  worker keeps every class, since the master's spawn messages name them all.
- The front allows 5 open requests per client address: with one address per
  load generator, front clients beyond 5 per host mostly measure 503
  `FrontAtCapacity`. Raise `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE` on the front for a
  load test, or put the volume on native clients.

A master and a worker on one host, and a second worker on another host that
reaches the master's port 5557 through an SSH tunnel (both checkouts at the same
revision):

```
# master (runs no users; writes the CSV and HTML)
locust -f tests/stress/locustfile.py --master --master-bind-host 127.0.0.1 --master-bind-port 5557 \
    --expect-workers 2 --host https://dev.example --headless --only-summary -u 220 -r 10 --run-time 30m \
    --bootstrap-requestors 0 --bootstrap-workers 0 --preflight-fail-hard \
    --requestor-api-keys "$REQUESTOR_KEYS" --worker-api-keys "$WORKER_KEYS" \
    --chat-mix-front-host https://oai.dev.example/v1 --chat-mix-front-clients 10 --chat-mix-native-clients 20 \
    --chat-mix-workers 6 --chat-mix-worker-names N0,N1,N2,N3,N4,N5 --chat-mix-worker-api-key "$WORKER_OWNER_KEY" \
    --chat-mix-anonymous-share 0.2 --csv run/chat --html run/chat.html

# worker on the master's host
locust -f tests/stress/locustfile.py --worker --master-host 127.0.0.1 --master-port 5557

# worker on another host, after: ssh -N -L 5557:127.0.0.1:5557 <master-host>
locust -f tests/stress/locustfile.py --worker --master-host 127.0.0.1 --master-port 5557

python tests/stress/check_smoke_results.py --stats run/chat_stats.csv
```

### Measurements

The run records `CHAT-METRIC` rows for correlation with the front's and the
Horde's telemetry: `[queue_wait_seconds] <persona>` from `x_horde`,
`[time-to-first-keepalive] stream`, `[time-to-first-content] stream`,
`[queue_wait_seconds] native chat` and `[submit-to-done] native chat`. A
streamed request's own row times the response headers; its content timing is
the metric row. At test stop each worker's submits are logged beside the
Horde's `nonconformant_replies` and `uncompleted_jobs` counters.

The front allows 5 open requests per client address by default, so more than 5
front clients from one load generator see 503 `FrontAtCapacity` (benign);
native clients are not limited by it.

The chat counts are pinned, so `-u` has to cover them plus the users the rest of
the mix should spawn; the example leaves 6 for the default classes:

```
locust -f tests/stress/locustfile.py --host https://dev.example --headless -u 11 -r 5 --run-time 10m \
    --chat-mix-front-host https://oai.dev.example/v1 \
    --chat-mix-front-clients 1 --chat-mix-native-clients 1 --chat-mix-workers 3 \
    --chat-mix-worker-names NameA,NameB,NameC --chat-mix-worker-api-key "$WORKER_OWNER_KEY" --csv run/chat
python tests/stress/check_smoke_results.py --stats run/chat_stats.csv
```

## Chat front scale demo

The chat front (`python -m horde.oai`) holds each open `/v1/chat/completions`
request as an idle coroutine and wakes it on the Horde's Redis completion
signal, so a waiting client should cost the Horde one submit, one or two status
reads and a publish, and no waitress thread while it waits. The demo measures
that: it holds thousands of clients open against the front and records the
Horde instances' waitress occupancy, the front's in-flight count and memory,
and the calls the front makes. It reports numbers and passes or fails nothing.

Unlike the other scenarios, the demo starts its own processes.
`run_chat_scale_demo.sh` brings up the backends through
`tests/run_with_test_stack.sh`, then starts:

- N Horde instances (`server.py --waitress_threads 45`, the production default,
  on consecutive ports from 7101; the first holds `--quorum`), each logging to
  `horde-<port>.log` in the run directory;
- the front on port 7002, listing every instance in `HORDE_OAI_HORDE_URLS` (the
  front round-robins, so no load balancer is needed), logging to `front.log`
  with one `httpx` line per call it makes to a Horde;
- `chat_scale_demo.py sample`, which reads every few seconds each instance's
  `GET /api/v2/status/heartbeat` (the route serving the waitress `active_count`,
  `queue` and `threads` gauges), the front's `GET /healthz` `inflight` count,
  Redis `INFO`, the Postgres client backend count, and the RSS and CPU of each
  process listening on a sampled port, into `samples.csv`;
- Locust with `locustfile_chat.py`, headless.

It stops every process it started on exit. The backends stay up unless `--down`
is given. Pass the run directory as the last argument:

```
bash tests/stress/run_chat_scale_demo.sh path/to/run
bash tests/stress/run_chat_scale_demo.sh --stages 50,200 --stage-seconds 60 path/to/smaller-run
bash tests/stress/run_chat_scale_demo.sh --instances 1 --sample-only 20 path/to/idle-run
```

`--sample-only S` starts everything except Locust and samples the idle stack for
S seconds, which checks the wiring without load.

The Horde instances create the schema in the stack's database and write to its
Redis. When another test run is using the default stack, give the demo its own
Compose project and ports so the two never share data:

```
AI_HORDE_TEST_POSTGRES_PORT=25432 AI_HORDE_TEST_REDIS_PORT=26379 \
AI_HORDE_TEST_GARAGE_S3_PORT=23900 AI_HORDE_TEST_GARAGE_RPC_PORT=23901 \
AI_HORDE_TEST_GARAGE_ADMIN_PORT=23903 \
bash tests/stress/run_chat_scale_demo.sh --compose-project chat-scale-demo --down path/to/run
```

### Populations

| User | Flag | Behavior |
| --- | --- | --- |
| `ChatWorkerSimulator` | `--chat-workers` (fixed count) | Pops `/api/v2/generate/text/pop` from the instances round-robin with a chat declaration (`chat_jobs`, `chat_features` `seed` and `presence_penalty`, `chat_limits`, `model_id`), built by `tests/integration/_fake_chat_worker.py`. Fails the run if a chat job carries a `prompt` or no `messages`, waits `--sim-gen-time-min` to `--sim-gen-time-max` seconds (3 to 8 in this scenario), and submits a structured reply: `message`, `finish_reason`, `prompt_tokens`, `completion_tokens` and `model_id`, without `generation`. `--chat-unreported-usage-chance` of submits leave the counts out so the front's estimate runs. The reply carries the time the submit was sent. With `--chat-worker-progress`, a chat job whose payload carries `stream_progress` also posts the reply text to `/api/v2/generate/text/progress` while it generates: one delta per `--chat-worker-progress-interval` seconds (default 1) of the generation time, at least one, with `seq` counting from 1. The submitted reply starts with the posted text. Posts are recorded as `/api/v2/generate/text/progress [<status>]` rows; 404 and 413 stop the job's posting, 403 and 409 stop it and fail the post, and 429, 503 and transport errors resend the delta at the next interval. |
| `ChatStreamClient` | `--chat-stream-clients` (share) | Posts `stream: true` with `stream_options.include_usage` and reads the events to `[DONE]`. Records time to the first keepalive, time to the content chunk and total time as `SSE` rows. An error chunk or a stream without `[DONE]` fails the request. |
| `ChatClient` | `--chat-clients` (share) | Posts non-streamed requests and waits for the body, up to 30 seconds past `--chat-nonstream-timeout`, so the front's 408 arrives first. |
| `ChatImpatientClient` | `--chat-impatient-clients` (share) | Posts a streamed (`--chat-impatient-stream-chance`) or non-streamed request and disconnects after `--chat-impatient-timeout` seconds, so the front cancels the job on the Horde. |

The client flags are shares of each stage's client count (Locust weights);
0 leaves a population out. Clients send `Authorization: Bearer <key>` with the
requestor keys in round-robin order, so no key reaches the Horde's per-user
concurrency cap before the others. The runner registers one key per 25 clients
of the largest stage and one worker key per three workers. Like an OpenAI SDK
client, each client reuses its connection between requests and waits 2 to 6
seconds after a refused or failed connection before its next request.

408, 429 and 503 from the front count as successes and appear under their own
`[expected]` rows, as do deliberate disconnects; 5xx, transport errors and
malformed streams fail.

### Stages and knobs

`ChatStagesShape` runs the workers alone for `--chat-warmup-seconds`, then holds
`--chat-workers` plus each `--chat-stages` count for `--chat-stage-seconds`,
moving between stages at `--chat-spawn-rate` users a second, and stops after the
last stage. The stage clock starts after the test-start hooks have registered
the keys, so a slow registration does not shorten a stage.

| Runner option | Environment variable | Default |
| --- | --- | --- |
| `--instances` | `CHAT_DEMO_HORDE_INSTANCES` | `3` |
| `--horde-base-port` | `CHAT_DEMO_HORDE_BASE_PORT` | `7101` |
| `--front-port` | `CHAT_DEMO_FRONT_PORT` | `7002` |
| `--waitress-threads` | `CHAT_DEMO_WAITRESS_THREADS` | `45` |
| `--workers` | `CHAT_DEMO_WORKERS` | `50` |
| `--stages` | `CHAT_DEMO_STAGES` | `500,2000,5000` |
| `--stage-seconds` | `CHAT_DEMO_STAGE_SECONDS` | `120` |
| `--warmup-seconds` | `CHAT_DEMO_WARMUP_SECONDS` | `20` |
| `--spawn-rate` | `CHAT_DEMO_SPAWN_RATE` | `100` |
| `--sample-interval` | `CHAT_DEMO_SAMPLE_INTERVAL` | `5` |
| `--compose-project` | `CHAT_DEMO_COMPOSE_PROJECT` | unset (the default stack) |
| | `CHAT_DEMO_FRONT_MAX_INFLIGHT` | 1.2 times the largest stage plus 100, above every stage so a 503 marks the front's own limit |
| | `CHAT_DEMO_RATELIMIT_DISABLED` | `1`; every client reaches the Horde from the front's one address, so per-IP limits would cap the run |
| | `CHAT_DEMO_SIM_GEN_TIME_MIN`, `CHAT_DEMO_SIM_GEN_TIME_MAX` | `3`, `8` |
| | `CHAT_DEMO_PYTHON` | the repository's `.venv` interpreter |

Arguments after `--` go to Locust unchanged. Locust runs as one process.

### Reading the summary

`chat_scale_demo.py summarize --run-dir <run>` (run by the script at the end)
writes `summary.md` and `summary.json` with one column per stage:

| Row | Source | Reading |
| --- | --- | --- |
| Front in-flight, peak / mean | `samples.csv`, `/healthz` | Open client requests on the front. Close to the stage's client count when clients spend little time between requests. |
| Horde waitress active and queue | `samples.csv`, heartbeat | Threads busy per instance and tasks waiting for one. Flat across stages means waiting clients cost no threads; a non-zero queue means an instance ran out of threads. |
| Front to Horde calls per second, by route | `front.log` `httpx` lines | Submits (`POST /v2/generate/chat/async`), status reads (`GET .../chat/status/<id>`) and cancels (`DELETE`) the front sent. |
| Workers pop and submit per second | `locust_stats_history.csv` | The worker side of the Horde load. |
| Status reads per finished request | derived | Status reads divided by the front's finished lines; near 1 to 2 when the Redis signal wakes requests, higher when they fall back to polling. |
| Woken by | `front.log` finished lines | `signal`, `poll` or `immediate` (finished before the wait began). |
| Worker submit to front wake | evidence plus `front.log` | The front's `woke_at` minus the time the worker sent the submit, joined by request id. Includes the submit's own handling up to the publish. |
| Stream timings | `chat_evidence.jsonl` | Time to the first keepalive (only requests that waited past a heartbeat have one), to the content, and total. |
| Usage reported / estimated | `chat_evidence.jsonl` | `x_horde.estimated_usage` on completed requests. |
| Client statuses, refusal codes, outcomes | `chat_evidence.jsonl` | Counts of 408, 429, 503 and the rest, the error codes behind them, and completed, refused, disconnected and failed requests. |
| Front RSS | `samples.csv` | Per stage and the run's peak. |
| Postgres connections | `samples.csv` | Client backends against `max_connections`, the usual first limit for several instances. |

The log pattern counts below the table show where a stage that fell short of
its client count stopped. Locust's own `CPU usage above` warnings and socket
errors (`10055` no buffer space and `10048` address in use on Windows, `Too many
open files` elsewhere) mean the load generator was the limit. `QueuePool limit`
or `too many clients already` in a Horde log mean Postgres connections were.
`refused the connection` and status `unreachable` in the front log mean a Horde
instance stopped answering. `run.env` records the settings and the open file
limit the run had.

