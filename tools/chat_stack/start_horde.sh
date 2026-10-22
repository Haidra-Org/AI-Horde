#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"

usage() {
  cat <<EOF
Usage: bash tools/chat_stack/start_horde.sh [options] <run-dir>

Brings up the test backends through tests/run_with_test_stack.sh as their own
Compose project and runs one quorum Horde instance in the foreground, with test
API keys and rate limits disabled. Records the injected stack env to
<run-dir>/stack.env; the Horde writes its log under <run-dir>/horde-<port>/.
Run it in a background shell and wait on GET /api/v2/status/heartbeat.

Options (each also read from the environment variable shown):
  --port P               Horde port (CHAT_STACK_HORDE_PORT, 7101)
  --python PATH          Interpreter (CHAT_STACK_PYTHON, the repository's .venv/bin/python)
  --compose-project NAME Compose project of the backends (COMPOSE_PROJECT_NAME, chatlocal)
  -h, --help             Show this help

The backend ports default to a set apart from the default test stack's, so a
test run and this stack never share data: AI_HORDE_TEST_POSTGRES_PORT 45432,
AI_HORDE_TEST_REDIS_PORT 46379, AI_HORDE_TEST_GARAGE_S3_PORT 43900,
AI_HORDE_TEST_GARAGE_RPC_PORT 43901, AI_HORDE_TEST_GARAGE_ADMIN_PORT 43903.
Export any of them to override.
EOF
}

port="${CHAT_STACK_HORDE_PORT:-7101}"
python="${CHAT_STACK_PYTHON:-$repo_root/.venv/bin/python}"
compose_project="${COMPOSE_PROJECT_NAME:-chatlocal}"
inner=0
run_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) port="$2"; shift ;;
    --python) python="$2"; shift ;;
    --compose-project) compose_project="$2"; shift ;;
    # Internal: the second pass, run by tests/run_with_test_stack.sh with the stack env injected.
    --inner) inner=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    *) run_dir="$1" ;;
  esac
  shift
done

if [[ -z "$run_dir" ]]; then
  echo "No run directory given." >&2
  usage >&2
  exit 1
fi

if [[ "$inner" == "0" ]]; then
  mkdir -p "$run_dir"
  # run_with_test_stack.sh changes to the repository root, so the second pass needs an absolute run directory.
  run_dir="$(cd -- "$run_dir" && pwd)"
  export COMPOSE_PROJECT_NAME="$compose_project"
  export AI_HORDE_TEST_POSTGRES_PORT="${AI_HORDE_TEST_POSTGRES_PORT:-45432}"
  export AI_HORDE_TEST_REDIS_PORT="${AI_HORDE_TEST_REDIS_PORT:-46379}"
  export AI_HORDE_TEST_GARAGE_S3_PORT="${AI_HORDE_TEST_GARAGE_S3_PORT:-43900}"
  export AI_HORDE_TEST_GARAGE_RPC_PORT="${AI_HORDE_TEST_GARAGE_RPC_PORT:-43901}"
  export AI_HORDE_TEST_GARAGE_ADMIN_PORT="${AI_HORDE_TEST_GARAGE_ADMIN_PORT:-43903}"
  exec bash "$repo_root/tests/run_with_test_stack.sh" -- \
    bash "$script_dir/start_horde.sh" --inner --port "$port" --python "$python" "$run_dir"
fi

env | grep -E '^(POSTGRES_URL|REDIS_IP|REDIS_PORT|R2_|AI_HORDE_TEST_|COMPOSE_PROJECT_NAME)' > "$run_dir/stack.env"
# The dev test-user route creates the requester and worker keys; one address drives every client.
export HORDE_TEST_APIKEYS=1 HORDE_TEST_RATELIMIT_DISABLED=1
export REDIS_SERVERS="${REDIS_SERVERS:-[\"${REDIS_IP:-127.0.0.1}\"]}"
mkdir -p "$run_dir/horde-$port"
cd "$run_dir/horde-$port"
exec "$python" "$repo_root/server.py" -vvvv -i --horde stable --allow_all_ips --port "$port" --waitress_threads 45 --quorum
