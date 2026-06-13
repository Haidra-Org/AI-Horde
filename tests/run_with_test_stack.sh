#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
compose_file="$script_dir/docker-compose.yml"
teardown_after_run=0

usage() {
  cat <<EOF
Usage: bash tests/run_with_test_stack.sh [--down] -- <command> [args...]

Starts the local Postgres/Redis/Garage stack if needed, bootstraps Garage,
injects the test env into the child process, and then runs the requested
command without touching the repository's .env files.

Options:
  --down      Stop the local test stack after the command exits.
  -h, --help  Show this help message.

Examples:
  bash tests/run_with_test_stack.sh -- .venv/bin/python -m pytest tests/integration/test_image.py -q -rs
  bash tests/run_with_test_stack.sh --down -- env | grep '^R2_TRANSIENT_ACCOUNT='
EOF
}

compose() {
  docker compose -f "$compose_file" "$@"
}

cleanup() {
  if [[ "$teardown_after_run" == "1" ]]; then
    echo "Stopping local test stack..."
    compose down
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --down)
        teardown_after_run=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        break
        ;;
      *)
        break
        ;;
    esac
    shift
  done

  if [[ $# -eq 0 ]]; then
    echo "No command provided." >&2
    usage >&2
    exit 1
  fi

  command_to_run=("$@")
}

main() {
  local -a command_to_run

  parse_args "$@"
  trap cleanup EXIT

  cd "$repo_root"

  echo "Ensuring local test credentials..."
  bash "$script_dir/garage/ensure_test_credentials.sh" >/dev/null

  echo "Starting local test stack..."
  compose up -d

  echo "Bootstrapping Garage and loading test env..."
  eval "$(bash "$script_dir/bootstrap_garage.sh" --shell)"

  echo "Running command with local test stack env..."
  "${command_to_run[@]}"
}

main "$@"