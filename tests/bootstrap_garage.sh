#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
compose_file="$script_dir/docker-compose.yml"
output_mode="human"

# Ensure the git-ignored test credentials exist, then load them so the access
# key/secret we import into Garage match what the test process consumes.
credentials_file="$(bash "$script_dir/garage/ensure_test_credentials.sh")"
# shellcheck disable=SC1090
source "$credentials_file"

garage_access_key_name="${AI_HORDE_TEST_GARAGE_ACCESS_KEY_NAME:-ai-horde-tests}"
garage_access_key_id="${AI_HORDE_TEST_GARAGE_ACCESS_KEY_ID}"
garage_secret_key="${AI_HORDE_TEST_GARAGE_SECRET_KEY}"
garage_capacity="${AI_HORDE_TEST_GARAGE_CAPACITY:-1G}"
garage_admin_port="${AI_HORDE_TEST_GARAGE_ADMIN_PORT:-3903}"
garage_s3_port="${AI_HORDE_TEST_GARAGE_S3_PORT:-3900}"

transient_bucket="${AI_HORDE_TEST_R2_TRANSIENT_BUCKET:-stable-horde}"
permanent_bucket="${AI_HORDE_TEST_R2_PERMANENT_BUCKET:-stable-horde}"
source_image_bucket="${AI_HORDE_TEST_R2_SOURCE_IMAGE_BUCKET:-stable-horde-source-images}"
prompts_bucket="${AI_HORDE_TEST_R2_PROMPTS_BUCKET:-prompts}"

compose() {
  docker compose -f "$compose_file" "$@"
}

usage() {
  cat <<EOF
Usage: bash tests/bootstrap_garage.sh [--shell]

Bootstraps the local Garage instance used by the image integration tests.

Options:
  --shell    Print only export statements so the caller can eval/source them.
  -h, --help Show this help message.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --shell)
        output_mode="shell"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage >&2
        return 1
        ;;
    esac
    shift
  done
}

garage_exec() {
  compose exec -T garage /garage -c /etc/garage.toml "$@"
}

wait_for_cli() {
  local retries=15

  while [[ "$retries" -gt 0 ]]; do
    if garage_exec status >/dev/null 2>&1; then
      return 0
    fi
    retries=$((retries - 1))
    sleep 2
  done

  echo "Garage CLI did not become ready in time." >&2
  return 1
}

wait_for_admin_health() {
  local retries=15
  local health_code

  while [[ "$retries" -gt 0 ]]; do
    health_code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${garage_admin_port}/health" || printf '000')"
    if [[ "$health_code" == "200" ]]; then
      return 0
    fi
    retries=$((retries - 1))
    sleep 2
  done

  echo "Garage admin health endpoint did not become ready in time." >&2
  return 1
}

ensure_garage_running() {
  local garage_id

  garage_id="$(compose ps -q garage)"
  if [[ -z "$garage_id" ]]; then
    echo "Garage is not running. Start the test stack first: docker compose -f tests/docker-compose.yml up -d" >&2
    return 1
  fi
}

parse_node_id() {
  local node_id
  local status_output

  node_id="$(garage_exec node id | sed -nE 's/^([0-9a-f]{16,64})@.*$/\1/p' | head -1)"
  if [[ -n "$node_id" ]]; then
    printf '%s\n' "$node_id"
    return 0
  fi

  status_output="$(garage_exec status)"
  node_id="$(printf '%s\n' "$status_output" | sed -nE 's/^([0-9a-f]{16,64})[[:space:]].*$/\1/p' | head -1)"
  if [[ -z "$node_id" ]]; then
    echo "Could not parse Garage node ID for layout assignment." >&2
    return 1
  fi

  printf '%s\n' "$node_id"
}

ensure_layout() {
  local node_id
  local health_code

  health_code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${garage_admin_port}/health" || printf '000')"
  if [[ "$health_code" == "200" ]]; then
    return 0
  fi

  node_id="$(parse_node_id)"
  garage_exec layout assign "$node_id" --zone dc1 --capacity "$garage_capacity" >/dev/null
  garage_exec layout apply --version 1 >/dev/null
}

ensure_key() {
  local output

  if ! output="$(garage_exec key import --yes -n "$garage_access_key_name" "$garage_access_key_id" "$garage_secret_key" 2>&1)"; then
    if [[ "$output" != *"KeyAlreadyExists"* ]]; then
      printf '%s\n' "$output" >&2
      return 1
    fi
  fi
}

ensure_buckets() {
  local bucket
  local output
  local -A seen=()
  local buckets=("$transient_bucket" "$permanent_bucket" "$source_image_bucket" "$prompts_bucket")

  for bucket in "${buckets[@]}"; do
    if [[ -n "${seen[$bucket]:-}" ]]; then
      continue
    fi
    seen[$bucket]=1

    if ! output="$(garage_exec bucket create "$bucket" 2>&1)"; then
      if [[ "$output" != *"BucketAlreadyExists"* ]]; then
        printf '%s\n' "$output" >&2
        return 1
      fi
    fi

    garage_exec bucket allow --read --write --owner "$bucket" --key "$garage_access_key_name" >/dev/null
  done
}

emit_exports() {
  cat <<EOF
export AWS_ACCESS_KEY_ID="$garage_access_key_id"
export AWS_SECRET_ACCESS_KEY="$garage_secret_key"
export SHARED_AWS_ACCESS_ID="$garage_access_key_id"
export SHARED_AWS_ACCESS_KEY="$garage_secret_key"
export R2_TRANSIENT_ACCOUNT="http://127.0.0.1:${garage_s3_port}"
export R2_PERMANENT_ACCOUNT="http://127.0.0.1:${garage_s3_port}"
export R2_TRANSIENT_BUCKET="$transient_bucket"
export R2_PERMANENT_BUCKET="$permanent_bucket"
export R2_SOURCE_IMAGE_BUCKET="$source_image_bucket"
export POSTGRES_URL="localhost:${AI_HORDE_TEST_POSTGRES_PORT:-5432}/${AI_HORDE_TEST_POSTGRES_DB:-postgres}"
export REDIS_IP="127.0.0.1"
export REDIS_PORT="${AI_HORDE_TEST_REDIS_PORT:-6379}"
EOF
}

print_exports() {
  if [[ "$output_mode" == "shell" ]]; then
    emit_exports
    return 0
  fi

  cat <<EOF
Garage bootstrap complete.

Export these variables before running the image integration tests:
EOF
  emit_exports
  cat <<EOF

The Garage bootstrap also ensures the hard-coded prompt upload bucket exists:
  $prompts_bucket
EOF
}

main() {
  parse_args "$@"
  cd "$repo_root"
  ensure_garage_running
  wait_for_cli
  ensure_layout
  ensure_key
  ensure_buckets
  wait_for_admin_health
  print_exports
}

main "$@"