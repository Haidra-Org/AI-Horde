#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"

usage() {
  cat <<EOF
Usage: bash tools/chat_stack/start_front.sh [options] <run-dir>

Runs the OpenAI-compatible front (python -m horde.oai) in the foreground
against the Horde that start_horde.sh started, from <run-dir>/front/. Run it in
a background shell and wait on GET /v1/models.

Options (each also read from the environment variable shown):
  --port P         Front port (HORDE_OAI_PORT, 7002)
  --listen ADDR    Address to bind (HORDE_OAI_LISTEN, 127.0.0.1)
  --horde-url URL  Horde API root (HORDE_OAI_HORDE_URLS, http://127.0.0.1:7101/api)
  --redis-url URL  Horde's Redis (HORDE_OAI_REDIS_URL,
                   redis://127.0.0.1:\$AI_HORDE_TEST_REDIS_PORT/7, port default 46379)
  --python PATH    Interpreter (CHAT_STACK_PYTHON, the repository's .venv/bin/python)
  -h, --help       Show this help

Every other HORDE_OAI_* variable in the environment reaches the front unchanged.
EOF
}

port="${HORDE_OAI_PORT:-7002}"
listen="${HORDE_OAI_LISTEN:-127.0.0.1}"
horde_url="${HORDE_OAI_HORDE_URLS:-http://127.0.0.1:7101/api}"
redis_url="${HORDE_OAI_REDIS_URL:-redis://127.0.0.1:${AI_HORDE_TEST_REDIS_PORT:-46379}/7}"
python="${CHAT_STACK_PYTHON:-$repo_root/.venv/bin/python}"
run_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) port="$2"; shift ;;
    --listen) listen="$2"; shift ;;
    --horde-url) horde_url="$2"; shift ;;
    --redis-url) redis_url="$2"; shift ;;
    --python) python="$2"; shift ;;
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

export HORDE_OAI_PORT="$port" HORDE_OAI_LISTEN="$listen" HORDE_OAI_HORDE_URLS="$horde_url" HORDE_OAI_REDIS_URL="$redis_url"
export PYTHONPATH="$repo_root"
mkdir -p "$run_dir/front"
cd "$run_dir/front"
exec "$python" -m horde.oai
