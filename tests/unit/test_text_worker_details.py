# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify that text worker details carry every column a text worker stores, under the shape the API marshals."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa
from flask_restx import marshal

from horde.apis.v2.base import models
from horde.classes.base.worker import Worker
from horde.classes.kobold.worker import TextWorker
from horde.flask import db

pytestmark = pytest.mark.unit

TEXT_WORKER_COLUMN_VALUES: dict[str, Any] = {
    "max_length": 123,
    "max_context_length": 4567,
    "accepts_chat_jobs": True,
    "accepts_prompt_jobs": False,
    "chat_features": ["seed"],
    "chat_max_context_length": 8191,
    "chat_max_length": 1023,
    "chat_tools_max_temperature": 0.4,
    "chat_model_id": "details-drift-model",
    "image_transports": ["base64"],
}
"""A distinct value for each column, so a value found at a details path can only have come from its column."""

TEXT_WORKER_DETAILS_PATHS: dict[str, tuple[str, ...]] = {
    "max_length": ("max_length",),
    "max_context_length": ("max_context_length",),
    "accepts_chat_jobs": ("accepts_chat_jobs",),
    "accepts_prompt_jobs": ("accepts_prompt_jobs",),
    "chat_features": ("chat", "features"),
    "chat_max_context_length": ("chat", "limits", "max_context_length"),
    "chat_max_length": ("chat", "limits", "max_length"),
    "chat_tools_max_temperature": ("chat", "limits", "tools_max_temperature"),
    "chat_model_id": ("chat", "model_id"),
    "image_transports": ("chat", "image_transports"),
}
"""Where each text worker column appears in the marshalled worker details."""

TEXT_WORKER_COLUMNS_KEPT_OUT_OF_DETAILS: frozenset[str] = frozenset()
"""Text worker columns deliberately left out of worker details. Every other column has a path above."""


def _text_worker_columns() -> set[str]:
    """Return the columns ``TextWorker`` maps beyond the ones every worker type shares."""
    return {column.key for column in sa.inspect(TextWorker).column_attrs} - {column.key for column in sa.inspect(Worker).column_attrs}


def test_every_text_worker_column_has_a_place_in_worker_details() -> None:
    """A new text worker column fails here until it is given a details path or kept out of details on purpose."""
    assert set(TEXT_WORKER_DETAILS_PATHS) | TEXT_WORKER_COLUMNS_KEPT_OUT_OF_DETAILS == _text_worker_columns()
    assert set(TEXT_WORKER_COLUMN_VALUES) == set(TEXT_WORKER_DETAILS_PATHS)


def test_marshalled_text_worker_details_hold_each_column_at_its_path(
    db_session: Any,
    fake_redis: Any,
    make_user: Any,
) -> None:
    """``get_details`` and the ``WorkerDetails`` model agree: every column's value survives marshalling at its path.

    The endpoints marshal with ``skip_none``, so a key the model does not define, or a model field the details do not
    fill, would drop the value without an error.
    """
    user = make_user()
    worker = TextWorker(
        user_id=user.id,
        name=f"text-details-{uuid.uuid4().hex[:12]}",
        last_check_in=datetime.utcnow(),
        allow_unsafe_ipaddr=True,
        **TEXT_WORKER_COLUMN_VALUES,
    )
    db.session.add(worker)
    db.session.commit()

    details = marshal(worker.get_details(), models.response_model_worker_details, skip_none=True)

    for column, path in TEXT_WORKER_DETAILS_PATHS.items():
        value: Any = details
        for key in path:
            assert key in value, f"{column} is missing from worker details at {'.'.join(path)}"
            value = value[key]
        assert value == TEXT_WORKER_COLUMN_VALUES[column], column
