# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cross-model checks for the complete served Swagger 2 document."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _schema_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _schema_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_nodes(child)


def test_swagger_primitive_defaults_match_their_declared_types(client) -> None:
    response = client.get("/api/swagger.json")
    assert response.status_code == 200

    swagger = response.get_json()
    primitive_types = {
        "array": list,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "object": dict,
        "string": str,
    }
    for schema in _schema_nodes(swagger):
        if "default" not in schema or schema.get("type") not in primitive_types:
            continue
        expected_type = primitive_types[schema["type"]]
        assert isinstance(schema["default"], expected_type), schema


def test_swagger_operation_ids_are_unique(client) -> None:
    response = client.get("/api/swagger.json")
    assert response.status_code == 200

    swagger = response.get_json()
    operation_ids = [
        operation["operationId"]
        for operations in swagger["paths"].values()
        for operation in operations.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))
    assert swagger["paths"]["/v2/styles/image_by_name/{style_name}"]["get"]["operationId"] == ("get_single_image_style_by_name")
    assert swagger["paths"]["/v2/styles/text_by_name/{style_name}"]["get"]["operationId"] == ("get_single_text_style_by_name")
