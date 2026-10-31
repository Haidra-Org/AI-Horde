# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The examples of ``docs/reference/chat_wire_examples.md`` as the wire example tests read and compare them.

[`DOCUMENTED`][tests.integration._wire_examples.DOCUMENTED] holds the page's examples by marker id.
[`structure_differences`][tests.integration._wire_examples.structure_differences] compares a documented body with a
live one, and [`filled`][tests.integration._wire_examples.filled] replaces a documented request's placeholders with
live values before it is posted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DOCUMENTED",
    "FRONT_EXAMPLE_PREFIX",
    "MODEL_NAME_PLACEHOLDER",
    "PAGE_PATH",
    "DocumentedExample",
    "filled",
    "structure_differences",
]

PAGE_PATH = Path(__file__).resolve().parents[2] / "docs" / "reference" / "chat_wire_examples.md"
"""The page whose examples the wire example tests check."""

EXAMPLE_PATTERN = re.compile(r"<!-- wire-example: (?P<id>[a-z0-9-]+) -->\r?\n```json\r?\n(?P<body>.*?)```", re.DOTALL)
"""One example: its marker line, then the fenced ``json`` block that follows it."""

SNIPPET_SECTION_LINE = re.compile(r"^.*--8<-- \[(?:start|end):[a-z][-_0-9a-z]*\].*\r?\n", re.MULTILINE)
"""A ``pymdownx.snippets`` section marker line inside an example. Other pages include the example through its section,
and the build removes the marker lines."""

PLACEHOLDER_PATTERN = re.compile(r"<[a-z0-9-]+>")
"""A whole string the page writes in place of an id, a key, a name or a kudos value, such as ``<job-id>``."""

FRONT_EXAMPLE_PREFIX = "front-"
"""The id prefix of the OpenAI-compatible front's examples, whose scenarios live in
``test_chat_front_wire_examples.py`` because they need the front harness."""

MODEL_NAME_PLACEHOLDER = "<model-name>"
"""The placeholder the page writes for the chat model."""

VALUE_KEYS = frozenset(
    {
        "rc",
        "code",
        "type",
        "object",
        "state",
        "finish_reason",
        "kind",
        "reason",
    },
)
"""The keys whose string values are part of the contract, so a documented value is compared with the live one; every
other string can differ by scenario and is compared by type."""


@dataclass(frozen=True)
class DocumentedExample:
    """Represents one example as the page holds it."""

    body: Any
    """The parsed JSON block."""

    prose: str
    """The page text between the previous example and this example's marker."""


def _documented_examples() -> dict[str, DocumentedExample]:
    page = PAGE_PATH.read_text(encoding="utf-8")
    examples: dict[str, DocumentedExample] = {}
    previous_end = 0
    for match in EXAMPLE_PATTERN.finditer(page):
        assert match["id"] not in examples, f"The marker {match['id']} appears twice."
        body = json.loads(SNIPPET_SECTION_LINE.sub("", match["body"]))
        examples[match["id"]] = DocumentedExample(body=body, prose=page[previous_end : match.start()])
        previous_end = match.end()
    return examples


DOCUMENTED = _documented_examples()
"""The page's examples by marker id, read once at import."""


def filled(documented: Any, placeholders: dict[str, str]) -> Any:
    """Return a documented body with each placeholder string replaced by its live value.

    Args:
        documented: The documented body.
        placeholders: The live value of each placeholder.

    Returns:
        The body to post.
    """
    if isinstance(documented, dict):
        return {key: filled(value, placeholders) for key, value in documented.items()}
    if isinstance(documented, list):
        return [filled(element, placeholders) for element in documented]
    if isinstance(documented, str):
        return placeholders.get(documented, documented)
    return documented


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    return "string"


def _scalar_difference(documented: Any, live: Any, path: str, key: str | None) -> list[str]:
    is_placeholder = isinstance(documented, str) and PLACEHOLDER_PATTERN.fullmatch(documented) is not None
    if is_placeholder:
        return []
    is_compared_by_value = isinstance(documented, bool) or (isinstance(documented, str) and key in VALUE_KEYS)
    if is_compared_by_value:
        has_documented_value = _json_type(documented) == _json_type(live) and documented == live
        return [] if has_documented_value else [f"{path}: documented {documented!r}, live {live!r}"]
    if _json_type(documented) != _json_type(live):
        return [f"{path}: documented a {_json_type(documented)}, live {live!r}"]
    return []


def structure_differences(documented: Any, live: Any, path: str = "$", key: str | None = None) -> list[str]:
    """Return where a documented body departs from the live body.

    Objects are compared by their key sets and arrays by their first elements. A string under one of ``VALUE_KEYS``
    and every boolean are compared by value, and every other scalar by its JSON type; a placeholder such as
    ``<kudos>`` matches any live value.

    Args:
        documented: The documented body.
        live: The body the code sent or accepted.
        path: The JSON path of ``documented`` within the example, for the messages.
        key: The object key ``documented`` sits under; an array element inherits its array's key.

    Returns:
        One message per difference, empty when the bodies agree.
    """
    if isinstance(documented, dict) or isinstance(live, dict):
        if not (isinstance(documented, dict) and isinstance(live, dict)):
            return [f"{path}: one side is an object and the other is not"]
        differences = (
            [f"{path}: keys {sorted(documented.keys() ^ live.keys())} are on one side only"] if documented.keys() != live.keys() else []
        )
        for shared_key in sorted(documented.keys() & live.keys()):
            differences += structure_differences(documented[shared_key], live[shared_key], f"{path}.{shared_key}", shared_key)
        return differences
    if isinstance(documented, list) or isinstance(live, list):
        if not (isinstance(documented, list) and isinstance(live, list)):
            return [f"{path}: one side is an array and the other is not"]
        if bool(documented) != bool(live):
            return [f"{path}: one side is an empty array"]
        return structure_differences(documented[0], live[0], f"{path}[0]", key) if documented else []
    return _scalar_difference(documented, live, path, key)
