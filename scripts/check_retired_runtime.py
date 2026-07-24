# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail CI if retired Horde runtime dependencies return to Grid code."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    ROOT / "grid_api",
    ROOT / "scripts",
    ROOT / "deploy" / "bootstrap.sh",
    ROOT / "deploy" / "systemd",
    ROOT / "Dockerfile",
    ROOT / "docker-compose.yaml",
    ROOT / "requirements.txt",
    ROOT / "requirements-grid.txt",
    ROOT / "requirements.dev.txt",
    ROOT / "pyproject.toml",
)
FORBIDDEN = {
    "legacy Python import": re.compile(r"^\s*(?:from|import)\s+horde\b", re.MULTILINE),
    "legacy table adapter": re.compile(
        r"\b(?:users_table|worker_models_table|waiting_prompts_table|processing_gens_table|LEGACY_WORKER_DEFAULTS)\b",
    ),
    "legacy Flask bridge": re.compile(r"\b(?:FLASK_API_BASE|flask_api_base)\b"),
    "legacy service unit": re.compile(r"\baipg-horde@"),
}


def source_files(root: Path):
    if root.is_file():
        if root.resolve() != Path(__file__).resolve():
            yield root
        return
    for path in root.rglob("*"):
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.resolve() != Path(__file__).resolve()
        ):
            yield path


def main() -> int:
    failures: list[str] = []
    for scan_root in SCAN_ROOTS:
        for path in source_files(scan_root):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in FORBIDDEN.items():
                if pattern.search(text):
                    failures.append(f"{path.relative_to(ROOT)}: {label}")

    if failures:
        print("retired runtime dependency detected:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("retired Horde runtime gate: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
