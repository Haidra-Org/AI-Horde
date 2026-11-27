# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check the cursor and the parts of a worker page request that do not touch the database.

The paging itself is a database query, so ``tests/integration/test_worker_list_entries.py`` covers walks, orders and
filters.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest

from horde.classes.base.worker import MAX_WORKER_NAME_CHARACTERS
from horde.worker_pages import (
    DEFAULT_WORKER_LIST_SORT,
    InvalidWorkerCursorError,
    WorkerListSort,
    WorkerPageCursor,
    WorkerPageFilters,
    WorkerPageLimits,
)


# Clients send the cursor in a URL, and worker names are free text in any script.
@pytest.mark.parametrize("name", ["ångström", "夢見る機械", "name with spaces & symbols/?#=", ""])
def test_a_name_cursor_survives_its_round_trip_through_a_url_query(name: str) -> None:
    cursor = WorkerPageCursor(sort=WorkerListSort.NAME, sort_value=name, worker_id=str(uuid.uuid4()))

    encoded = cursor.encode()

    assert set(encoded) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert WorkerPageCursor.decode(encoded, expected_sort=WorkerListSort.NAME) == cursor


@pytest.mark.parametrize("sort", [sort for sort in WorkerListSort if sort.descending])
@pytest.mark.parametrize("count", [0, 1, 2**63 - 1])
def test_a_count_cursor_survives_its_round_trip(sort: WorkerListSort, count: int) -> None:
    cursor = WorkerPageCursor(sort=sort, sort_value=count, worker_id=str(uuid.uuid4()))

    assert WorkerPageCursor.decode(cursor.encode(), expected_sort=sort) == cursor


def _encode_document(document: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(document).encode()).decode().rstrip("=")


def _valid_document(**overrides: object) -> dict[str, object]:
    return {"v": 1, "s": "name", "k": "dreamer", "i": str(uuid.uuid4()), **overrides}


# Positions in different orders cannot be compared. A name cursor used with a count order would start the walk at an
# arbitrary point and skip workers without any error. The cursor's value is bound into the keyset query, so a count
# outside the range of the count columns would fail in the database. Every malformed cursor must be refused.
@pytest.mark.parametrize(
    ("encoded", "expected_sort"),
    [
        pytest.param(_encode_document(_valid_document()), WorkerListSort.UPTIME, id="other-sort"),
        pytest.param(_encode_document(_valid_document(v=2)), WorkerListSort.NAME, id="other-version"),
        pytest.param(_encode_document(_valid_document(i="not-a-uuid")), WorkerListSort.NAME, id="bad-worker-id"),
        pytest.param(_encode_document(_valid_document(k=5)), WorkerListSort.NAME, id="number-for-name"),
        pytest.param(_encode_document(_valid_document(s="uptime", k="5")), WorkerListSort.UPTIME, id="text-for-count"),
        pytest.param(_encode_document(_valid_document(s="uptime", k=True)), WorkerListSort.UPTIME, id="bool-for-count"),
        pytest.param(_encode_document(_valid_document(s="uptime", k=5.5)), WorkerListSort.UPTIME, id="float-for-count"),
        pytest.param(_encode_document(_valid_document(s="uptime", k=2**63)), WorkerListSort.UPTIME, id="count-too-large"),
        pytest.param(_encode_document(_valid_document(s="uptime", k=-(2**63) - 1)), WorkerListSort.UPTIME, id="count-too-small"),
        pytest.param(_encode_document(["v", 1]), WorkerListSort.NAME, id="not-an-object"),
        pytest.param("%%%not-base64%%%", WorkerListSort.NAME, id="not-base64"),
        pytest.param(base64.urlsafe_b64encode(b"\xff\xfe").decode(), WorkerListSort.NAME, id="not-utf8"),
        pytest.param("A" * (WorkerPageLimits.MAX_CURSOR_CHARACTERS + 1), WorkerListSort.NAME, id="too-long"),
    ],
)
def test_a_cursor_that_this_server_did_not_issue_for_the_requested_sort_is_refused(
    encoded: str,
    expected_sort: WorkerListSort,
) -> None:
    with pytest.raises(InvalidWorkerCursorError):
        WorkerPageCursor.decode(encoded, expected_sort=expected_sort)


# The cursor for the longest accepted worker name must still be accepted by the server. A four-byte character is the
# largest one in UTF-8.
def test_the_cursor_of_the_longest_worker_name_fits_under_the_length_limit() -> None:
    longest_name = "𝔀" * MAX_WORKER_NAME_CHARACTERS
    cursor = WorkerPageCursor(sort=WorkerListSort.NAME, sort_value=longest_name, worker_id=str(uuid.uuid4()))

    assert len(cursor.encode()) <= WorkerPageLimits.MAX_CURSOR_CHARACTERS


# Front ends show the workers with the longest service first. Names chosen to sort first alphabetically must not fill
# the first page.
def test_without_a_sort_the_list_is_ordered_by_uptime_descending() -> None:
    assert DEFAULT_WORKER_LIST_SORT is WorkerListSort.UPTIME
    assert DEFAULT_WORKER_LIST_SORT.descending
    assert not WorkerListSort.NAME.descending


def test_filters_report_their_names_in_a_fixed_order_for_the_metric_label() -> None:
    filters = WorkerPageFilters(
        online=False,
        name_contains="x",
        owner_id=1,
        model="x",
        worker_type="image",
        team_id=str(uuid.uuid4()),
    )

    assert filters.applied_names == ["type", "model", "team", "owner", "name", "online"]
    assert WorkerPageFilters().applied_names == []
