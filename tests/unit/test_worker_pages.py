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
    page_is_missing_too_many_rows,
)

WALK_KEY: str = WorkerPageFilters().walk_key("public", None)
"""The walk key of an unfiltered public walk, used by the cursor tests that do not check the key."""


# Clients send the cursor in a URL, and worker names are free text in any script.
@pytest.mark.parametrize("name", ["ångström", "夢見る機械", "name with spaces & symbols/?#=", ""])
def test_a_name_cursor_survives_its_round_trip_through_a_url_query(name: str) -> None:
    cursor = WorkerPageCursor(sort=WorkerListSort.NAME, sort_value=name, worker_id=str(uuid.uuid4()), walk_key=WALK_KEY)

    encoded = cursor.encode()

    assert set(encoded) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert WorkerPageCursor.decode(encoded, expected_sort=WorkerListSort.NAME, expected_walk_key=WALK_KEY) == cursor


@pytest.mark.parametrize("sort", [sort for sort in WorkerListSort if sort.descending])
@pytest.mark.parametrize("count", [0, 1, 2**63 - 1])
def test_a_count_cursor_survives_its_round_trip(sort: WorkerListSort, count: int) -> None:
    cursor = WorkerPageCursor(sort=sort, sort_value=count, worker_id=str(uuid.uuid4()), walk_key=WALK_KEY)

    assert WorkerPageCursor.decode(cursor.encode(), expected_sort=sort, expected_walk_key=WALK_KEY) == cursor


def _encode_document(document: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(document).encode()).decode().rstrip("=")


def _valid_document(**overrides: object) -> dict[str, object]:
    return {"v": 1, "s": "name", "k": "dreamer", "i": str(uuid.uuid4()), "w": WALK_KEY, **overrides}


# Positions in different orders cannot be compared. A name cursor used with a count order would start the walk at an
# arbitrary point and skip workers without any error. A cursor from a walk with other filters would skip, without any
# error, the matching workers before its position. The cursor's value is bound into the keyset query, so a count
# outside the range of the count columns would fail in the database. Every malformed cursor must be refused.
@pytest.mark.parametrize(
    ("encoded", "expected_sort"),
    [
        pytest.param(_encode_document(_valid_document()), WorkerListSort.UPTIME, id="other-sort"),
        pytest.param(_encode_document(_valid_document(v=2)), WorkerListSort.NAME, id="other-version"),
        pytest.param(_encode_document(_valid_document(w="0" * 16)), WorkerListSort.NAME, id="other-walk"),
        pytest.param(_encode_document(_valid_document(w=None)), WorkerListSort.NAME, id="no-walk-key"),
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
        WorkerPageCursor.decode(encoded, expected_sort=expected_sort, expected_walk_key=WALK_KEY)


# The cursor for the longest accepted worker name must still be accepted by the server. A four-byte character is the
# largest one in UTF-8.
def test_the_cursor_of_the_longest_worker_name_fits_under_the_length_limit() -> None:
    longest_name = "𝔀" * MAX_WORKER_NAME_CHARACTERS
    cursor = WorkerPageCursor(sort=WorkerListSort.NAME, sort_value=longest_name, worker_id=str(uuid.uuid4()), walk_key=WALK_KEY)

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


# A cursor continues only the walk that issued it. Each part of the key changes which workers are listed.
@pytest.mark.parametrize(
    ("filters", "view", "account_id"),
    [
        pytest.param(WorkerPageFilters(worker_type="text"), "public", None, id="type"),
        pytest.param(WorkerPageFilters(model="x"), "public", None, id="model"),
        pytest.param(WorkerPageFilters(team_id=str(uuid.uuid4())), "public", None, id="team"),
        pytest.param(WorkerPageFilters(owner_id=1), "public", None, id="owner"),
        pytest.param(WorkerPageFilters(name_contains="x"), "public", None, id="name"),
        pytest.param(WorkerPageFilters(online=False), "public", None, id="online"),
        pytest.param(WorkerPageFilters(), "moderator", None, id="view"),
        pytest.param(WorkerPageFilters(), "owner", 1, id="account"),
    ],
)
def test_a_walk_key_differs_for_other_filters_views_and_accounts(
    filters: WorkerPageFilters,
    view: str,
    account_id: int | None,
) -> None:
    assert filters.walk_key(view, account_id) != WALK_KEY


def test_the_same_filters_give_the_same_walk_key_and_the_key_hides_the_search_text() -> None:
    search_text = "distinctive-search-text"
    first_key = WorkerPageFilters(name_contains=search_text, online=True).walk_key("public", None)
    second_key = WorkerPageFilters(name_contains=search_text, online=True).walk_key("public", None)
    cursor = WorkerPageCursor(sort=WorkerListSort.NAME, sort_value="a", worker_id=str(uuid.uuid4()), walk_key=first_key)

    encoded = cursor.encode()
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")

    assert first_key == second_key
    assert search_text not in decoded


# The cases below assume MAX_MISSING_ROW_SHARE of a tenth and TOLERATED_MISSING_ROWS of two.
@pytest.mark.parametrize(
    ("page_worker_count", "missing_row_count"),
    [
        (0, 0),
        (100, 0),
        (100, 2),
        (100, 10),
        (30, 3),
        (10, 2),
        (3, 2),
        (2, 1),
    ],
)
def test_a_page_with_a_few_missing_rows_is_served(page_worker_count: int, missing_row_count: int) -> None:
    assert not page_is_missing_too_many_rows(page_worker_count, missing_row_count)


@pytest.mark.parametrize(
    ("page_worker_count", "missing_row_count"),
    [
        (100, 11),
        (100, 95),
        (25, 3),
        (4, 3),
    ],
)
def test_a_page_missing_more_than_the_tolerated_rows_and_share_is_refused(page_worker_count: int, missing_row_count: int) -> None:
    assert page_is_missing_too_many_rows(page_worker_count, missing_row_count)


# A small page is still refused when every row is missing, because an empty page with a cursor looks like an empty list.
@pytest.mark.parametrize("page_worker_count", [1, 2, 3])
def test_a_small_page_without_any_stored_row_is_refused(page_worker_count: int) -> None:
    assert page_is_missing_too_many_rows(page_worker_count, page_worker_count)
