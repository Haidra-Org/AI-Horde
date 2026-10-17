#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"

usage() {
  cat <<EOF
Usage: bash tests/stress/run_chat_scale_demo.sh [options] <run-dir> [-- <extra locust args>]

Runs the chat front scale demo: brings up the test backends through
tests/run_with_test_stack.sh, starts N Horde instances and the chat front,
drives them with tests/stress/locustfile_chat.py through its client stages,
samples the waitress gauges, the front's in-flight count, Redis and Postgres
every few seconds, and writes samples.csv, summary.md and summary.json into
<run-dir>. Every process it started is stopped on exit.

Options (each also read from the environment variable shown):
  --instances N          Horde instances (CHAT_DEMO_HORDE_INSTANCES, 3)
  --horde-base-port P    First Horde port; the rest follow (CHAT_DEMO_HORDE_BASE_PORT, 7101)
  --front-port P         Front port (CHAT_DEMO_FRONT_PORT, 7002)
  --waitress-threads N   Threads per Horde instance (CHAT_DEMO_WAITRESS_THREADS, 45)
  --workers N            Simulated chat workers (CHAT_DEMO_WORKERS, 50)
  --stages LIST          Client counts per stage (CHAT_DEMO_STAGES, 500,2000,5000)
  --stage-seconds S      Length of each stage (CHAT_DEMO_STAGE_SECONDS, 120)
  --warmup-seconds S     Workers-only warm-up (CHAT_DEMO_WARMUP_SECONDS, 20)
  --spawn-rate R         Users started per second (CHAT_DEMO_SPAWN_RATE, 100)
  --sample-interval S    Seconds between gauge samples (CHAT_DEMO_SAMPLE_INTERVAL, 5)
  --sample-only S        Start the stack, Horde instances and front, sample for
                         S seconds without Locust, then stop (no summary)
  --compose-project NAME Run the backends as their own Compose project, so the
                         demo never touches the default test stack's data
                         (CHAT_DEMO_COMPOSE_PROJECT, unset). Give it its own
                         AI_HORDE_TEST_*_PORT values.
  --down                 Stop the backends when the run ends
  -h, --help             Show this help

Other environment variables:
  CHAT_DEMO_PYTHON                 Interpreter (default: the repository's .venv)
  CHAT_DEMO_FRONT_MAX_INFLIGHT     HORDE_OAI_MAX_INFLIGHT (default: 1.2 x the largest stage)
  CHAT_DEMO_RATELIMIT_DISABLED     HORDE_TEST_RATELIMIT_DISABLED for the Horde instances (default 1;
                                   every client reaches the Horde from one address through the front)
  CHAT_DEMO_HORDE_VERBOSITY        Horde log verbosity flag (default -vvvv)
  CHAT_DEMO_SIM_GEN_TIME_MIN/MAX   Simulated generation seconds (default 3 and 8)

Example:
  bash tests/stress/run_chat_scale_demo.sh --stages 50,200 --stage-seconds 60 /tmp/chat-demo
EOF
}

instances="${CHAT_DEMO_HORDE_INSTANCES:-3}"
horde_base_port="${CHAT_DEMO_HORDE_BASE_PORT:-7101}"
front_port="${CHAT_DEMO_FRONT_PORT:-7002}"
waitress_threads="${CHAT_DEMO_WAITRESS_THREADS:-45}"
workers="${CHAT_DEMO_WORKERS:-50}"
stages="${CHAT_DEMO_STAGES:-500,2000,5000}"
stage_seconds="${CHAT_DEMO_STAGE_SECONDS:-120}"
warmup_seconds="${CHAT_DEMO_WARMUP_SECONDS:-20}"
spawn_rate="${CHAT_DEMO_SPAWN_RATE:-100}"
sample_interval="${CHAT_DEMO_SAMPLE_INTERVAL:-5}"
sample_only_seconds=""
compose_project="${CHAT_DEMO_COMPOSE_PROJECT:-}"
stack_down=0
run_dir=""
extra_locust_args=()
original_args=("$@")

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instances) instances="$2"; shift ;;
      --horde-base-port) horde_base_port="$2"; shift ;;
      --front-port) front_port="$2"; shift ;;
      --waitress-threads) waitress_threads="$2"; shift ;;
      --workers) workers="$2"; shift ;;
      --stages) stages="$2"; shift ;;
      --stage-seconds) stage_seconds="$2"; shift ;;
      --warmup-seconds) warmup_seconds="$2"; shift ;;
      --spawn-rate) spawn_rate="$2"; shift ;;
      --sample-interval) sample_interval="$2"; shift ;;
      --sample-only) sample_only_seconds="$2"; shift ;;
      --compose-project) compose_project="$2"; shift ;;
      --down) stack_down=1 ;;
      -h|--help) usage; exit 0 ;;
      --) shift; extra_locust_args=("$@"); break ;;
      -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
      *)
        if [[ -n "$run_dir" ]]; then
          echo "Only one run directory may be given." >&2
          exit 1
        fi
        run_dir="$1"
        ;;
    esac
    shift
  done
  if [[ -z "$run_dir" ]]; then
    echo "A run directory is required." >&2
    usage >&2
    exit 1
  fi
}

parse_args "$@"

# The backends and their env come from run_with_test_stack.sh, which runs this
# script again inside the injected environment.
if [[ -z "${CHAT_DEMO_INSIDE_TEST_STACK:-}" ]]; then
  # run_with_test_stack.sh changes to the repository root, so a relative run
  # directory is resolved here, against the caller's directory.
  mkdir -p "$run_dir"
  CHAT_DEMO_RUN_DIR="$(cd -- "$run_dir" && pwd)"
  export CHAT_DEMO_RUN_DIR
  stack_args=()
  if [[ "$stack_down" == "1" ]]; then
    stack_args+=(--down)
  fi
  if [[ -n "$compose_project" ]]; then
    export COMPOSE_PROJECT_NAME="$compose_project"
  fi
  case "$(uname -s)" in
    MINGW*|MSYS*)
      # Git Bash rewrites arguments that look like POSIX paths, which turns the
      # in-container paths of the Garage bootstrap's `compose exec` into Windows
      # paths; those two are excluded, the host paths still convert.
      export MSYS2_ARG_CONV_EXCL="/garage;/etc/garage.toml"
      ;;
  esac
  exec bash "$repo_root/tests/run_with_test_stack.sh" "${stack_args[@]}" -- \
    env CHAT_DEMO_INSIDE_TEST_STACK=1 bash "${BASH_SOURCE[0]}" "${original_args[@]}"
fi

# Native Windows interpreters launched from Git Bash need Windows-form paths in
# environment variables; elsewhere paths pass through unchanged.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s\n' "$1"
  fi
}

resolve_python() {
  if [[ -n "${CHAT_DEMO_PYTHON:-}" ]]; then
    printf '%s\n' "$CHAT_DEMO_PYTHON"
  elif [[ -x "$repo_root/.venv/Scripts/python.exe" ]]; then
    printf '%s\n' "$repo_root/.venv/Scripts/python.exe"
  elif [[ -x "$repo_root/.venv/bin/python" ]]; then
    printf '%s\n' "$repo_root/.venv/bin/python"
  else
    command -v python
  fi
}

started_pids=()
started_names=()

# On Windows a venv's python.exe is a launcher that starts the interpreter as a
# child process; ending only the launcher leaves the interpreter serving, so the
# whole process tree is ended there. Elsewhere the process gets 15 seconds to
# exit after SIGTERM before SIGKILL.
stop_pid() {
  local pid="$1"
  local name="$2"
  local waited=0

  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  echo "Stopping $name (pid $pid)"
  if [[ -r "/proc/$pid/winpid" ]]; then
    taskkill //F //T //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1 || true
  else
    kill -TERM "$pid" 2>/dev/null || true
    while kill -0 "$pid" 2>/dev/null && [[ "$waited" -lt 15 ]]; do
      sleep 1
      waited=$((waited + 1))
    done
    kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local index
  for ((index = ${#started_pids[@]} - 1; index >= 0; index--)); do
    stop_pid "${started_pids[$index]}" "${started_names[$index]}"
  done
}

wait_for_url() {
  local url="$1"
  local name="$2"
  local timeout_seconds="$3"
  local pid="$4"
  local waited=0

  until curl -sf -o /dev/null "$url"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$name exited before answering at $url; see its log in $run_dir." >&2
      return 1
    fi
    if [[ "$waited" -ge "$timeout_seconds" ]]; then
      echo "$name did not answer at $url within ${timeout_seconds}s; see its log in $run_dir." >&2
      return 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "$name is up at $url"
}

port_is_free() {
  local port="$1"
  ! curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/"
}

max_stage() {
  local largest=0
  local stage
  IFS=',' read -r -a stage_list <<<"$stages"
  for stage in "${stage_list[@]}"; do
    if [[ "$stage" -gt "$largest" ]]; then
      largest="$stage"
    fi
  done
  printf '%s\n' "$largest"
}

require_stack_env() {
  local name
  local missing=()
  for name in POSTGRES_URL REDIS_IP REDIS_PORT R2_TRANSIENT_ACCOUNT; do
    if [[ -z "${!name:-}" ]]; then
      missing+=("$name")
    fi
  done
  if [[ "${#missing[@]}" -gt 0 ]]; then
    # Without the stack's env the Horde falls back to localhost defaults, which
    # can be a different stack's Redis; starting it there would mix the runs.
    echo "The test stack env is missing ${missing[*]} (the Garage bootstrap may have timed out); rerun once the stack is healthy." >&2
    exit 1
  fi
}

main() {
  trap cleanup EXIT
  trap 'exit 130' INT TERM
  require_stack_env

  run_dir="${CHAT_DEMO_RUN_DIR:-$run_dir}"
  mkdir -p "$run_dir"
  run_dir="$(cd -- "$run_dir" && pwd)"
  local python
  python="$(resolve_python)"
  local largest_stage
  largest_stage="$(max_stage)"
  local front_max_inflight="${CHAT_DEMO_FRONT_MAX_INFLIGHT:-$((largest_stage + largest_stage / 5 + 100))}"
  local horde_verbosity="${CHAT_DEMO_HORDE_VERBOSITY:--vvvv}"

  export HORDE_TEST_RATELIMIT_DISABLED="${CHAT_DEMO_RATELIMIT_DISABLED:-1}"
  export REDIS_SERVERS="${REDIS_SERVERS:-[\"${REDIS_IP:-127.0.0.1}\"]}"

  # Thousands of concurrent client sockets exceed common default descriptor
  # limits; raise the soft limit where the platform allows and record it.
  ulimit -n 65535 2>/dev/null || ulimit -n "$(ulimit -Hn)" 2>/dev/null || true

  local horde_ports=()
  local horde_urls=()
  local horde_api_urls=()
  local index
  for ((index = 0; index < instances; index++)); do
    horde_ports+=("$((horde_base_port + index))")
    horde_urls+=("http://127.0.0.1:$((horde_base_port + index))")
    horde_api_urls+=("http://127.0.0.1:$((horde_base_port + index))/api")
  done
  local horde_urls_csv horde_api_urls_csv
  horde_urls_csv="$(IFS=,; printf '%s' "${horde_urls[*]}")"
  horde_api_urls_csv="$(IFS=,; printf '%s' "${horde_api_urls[*]}")"
  local front_url="http://127.0.0.1:$front_port"

  for port in "${horde_ports[@]}" "$front_port"; do
    if ! port_is_free "$port"; then
      echo "Port $port already answers HTTP; stop that process or choose other ports." >&2
      exit 1
    fi
  done

  cat >"$run_dir/run.env" <<EOF
instances=$instances
horde_ports=${horde_ports[*]}
front_port=$front_port
waitress_threads=$waitress_threads
workers=$workers
stages=$stages
stage_seconds=$stage_seconds
warmup_seconds=$warmup_seconds
spawn_rate=$spawn_rate
sample_interval=$sample_interval
front_max_inflight=$front_max_inflight
ratelimit_disabled=$HORDE_TEST_RATELIMIT_DISABLED
compose_project=${COMPOSE_PROJECT_NAME:-}
open_file_limit=$(ulimit -n)
uname=$(uname -s)
EOF

  local quorum_flag=()
  local -A horde_pids=()
  for ((index = 0; index < instances; index++)); do
    local port="${horde_ports[$index]}"
    quorum_flag=()
    if [[ "$index" == "0" ]]; then
      # One instance holds the quorum and runs the background threads, as in
      # the two-node CI job; the others serve requests only.
      quorum_flag=(--quorum)
    fi
    mkdir -p "$run_dir/horde-$port"
    # Each instance runs in its own directory so its rotating horde.log file
    # is not shared with the others.
    (cd "$run_dir/horde-$port" && exec "$python" "$(native_path "$repo_root/server.py")" "$horde_verbosity" -i \
      --horde stable --allow_all_ips --port "$port" --waitress_threads "$waitress_threads" "${quorum_flag[@]}") \
      >"$run_dir/horde-$port.log" 2>&1 &
    started_pids+=("$!")
    started_names+=("horde-$port")
    horde_pids[$port]="$!"
  done
  for port in "${horde_ports[@]}"; do
    wait_for_url "http://127.0.0.1:$port/api/v2/status/heartbeat" "Horde $port" 300 "${horde_pids[$port]}"
  done

  mkdir -p "$run_dir/front"
  (cd "$run_dir/front" && exec env \
    PYTHONPATH="$(native_path "$repo_root")" \
    HORDE_OAI_HORDE_URLS="$horde_api_urls_csv" \
    HORDE_OAI_LISTEN=127.0.0.1 \
    HORDE_OAI_PORT="$front_port" \
    HORDE_OAI_REDIS_URL="redis://${REDIS_IP:-127.0.0.1}:${REDIS_PORT:-6379}/7" \
    HORDE_OAI_MAX_INFLIGHT="$front_max_inflight" \
    "$python" "$(native_path "$script_dir/chat_scale_demo.py")" front) \
    >"$run_dir/front.log" 2>&1 &
  local front_pid="$!"
  started_pids+=("$front_pid")
  started_names+=("front")
  wait_for_url "$front_url/healthz" "Front" 120 "$front_pid"

  local postgres_dsn="postgresql://${POSTGRES_USER:-postgres}@${POSTGRES_URL:-localhost:5432/postgres}"
  local sampler_args=(
    sample
    --horde-urls "$horde_urls_csv"
    --front-url "$front_url"
    --redis-host "${REDIS_IP:-127.0.0.1}"
    --redis-port "${REDIS_PORT:-6379}"
    --postgres-dsn "$postgres_dsn"
    --interval "$sample_interval"
    --out "$(native_path "$run_dir/samples.csv")"
  )
  if [[ -n "$sample_only_seconds" ]]; then
    sampler_args+=(--duration "$sample_only_seconds")
  fi
  "$python" "$(native_path "$script_dir/chat_scale_demo.py")" "${sampler_args[@]}" >"$run_dir/sampler.log" 2>&1 &
  local sampler_pid="$!"
  started_pids+=("$sampler_pid")
  started_names+=("sampler")

  if [[ -n "$sample_only_seconds" ]]; then
    echo "Sampling for ${sample_only_seconds}s without load."
    wait "$sampler_pid" || true
    echo "Samples written to $run_dir/samples.csv"
    return 0
  fi

  local bootstrap_requestors=$(((largest_stage + 24) / 25 + 2))
  local bootstrap_workers=$(((workers + 2) / 3 + 2))
  local locust_status=0
  (cd "$repo_root" && "$python" -m locust -f tests/stress/locustfile_chat.py \
    --host "${horde_urls[0]}" --headless \
    --chat-front-host "$front_url" \
    --chat-horde-urls "$horde_urls_csv" \
    --chat-workers "$workers" \
    --chat-stages "$stages" \
    --chat-stage-seconds "$stage_seconds" \
    --chat-warmup-seconds "$warmup_seconds" \
    --chat-spawn-rate "$spawn_rate" \
    --sim-gen-time-min "${CHAT_DEMO_SIM_GEN_TIME_MIN:-3}" \
    --sim-gen-time-max "${CHAT_DEMO_SIM_GEN_TIME_MAX:-8}" \
    --bootstrap-requestors "$bootstrap_requestors" \
    --bootstrap-workers "$bootstrap_workers" \
    --chat-evidence-path "$(native_path "$run_dir/chat_evidence.jsonl")" \
    --csv "$(native_path "$run_dir/locust")" --csv-full-history \
    --logfile "$(native_path "$run_dir/locust.log")" \
    "${extra_locust_args[@]}") >"$run_dir/locust.out" 2>&1 || locust_status=$?
  echo "Locust exited with status $locust_status (non-zero when any request failed; see summary.md)."

  stop_pid "$sampler_pid" "sampler"
  "$python" "$(native_path "$script_dir/chat_scale_demo.py")" summarize --run-dir "$(native_path "$run_dir")" | tee "$run_dir/summary.log"
}

main
