#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

# Run the same Grid-native static and offline checks used by CI.

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

"$PYTHON" scripts/check_retired_runtime.py
"$PYTHON" -m black --check grid_api scripts
"$PYTHON" -m ruff check grid_api scripts
"$PYTHON" -m pytest grid_api -q
