# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Describe a request for one page of the list of every registered worker, and the cursor between two pages.

The list includes every registered worker, active or inactive. The page query
[`page_registered_workers`][horde.database.functions.page_registered_workers] reads the worker IDs of a page with a
keyset query on ``worker_list_entries``, and the caller reads the rows of those IDs from the row store
(``horde.worker_rows``). This module holds the parts of a page request that do not touch the database: the limits, the
orders, the filters and the cursor.

A page boundary is a position in a sort order and not an offset. The cursor records the sort value and the worker ID
of the last row of a page, and the next page starts strictly after that pair. Workers joining or leaving between pages
therefore do not shift the rows of later pages. A worker's sort value can change during a walk, as uptime does at
each check-in, so a walk can skip or repeat a worker whose value moves it past the cursor. Page boundaries are best
effort.

A cursor continues only the walk it was issued for. It holds a digest of the walk's filters, view and account, and
the server refuses it with other filters, another view or another account. A position in one filtered list does not
correspond to a position in another list. A walk continued under new filters would skip, without any error, the
matching workers before the cursor.

Public members:

- [`WorkerPageLimits`][horde.worker_pages.WorkerPageLimits]: the bounds of one page request.
- [`WorkerListSort`][horde.worker_pages.WorkerListSort]: the available page orders.
- [`DEFAULT_WORKER_LIST_SORT`][horde.worker_pages.DEFAULT_WORKER_LIST_SORT]: the order used when a request does not
  set one.
- [`WorkerPageFilters`][horde.worker_pages.WorkerPageFilters]: the filters in a request.
- [`WorkerPageCursor`][horde.worker_pages.WorkerPageCursor]: the encoded position between two pages.
- [`page_is_missing_too_many_rows`][horde.worker_pages.page_is_missing_too_many_rows]: whether a page with rows
  missing from the row store is refused instead of served.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeGuard

__all__ = [
    "DEFAULT_WORKER_LIST_SORT",
    "InvalidWorkerCursorError",
    "WorkerListSort",
    "WorkerPageCursor",
    "WorkerPageFilters",
    "WorkerPageLimits",
    "page_is_missing_too_many_rows",
]


class WorkerPageLimits:
    """Group the bounds of one page request."""

    DEFAULT_LIMIT: int = 25
    """Rows in a page when the request does not set a limit."""
    MAX_LIMIT: int = 100
    """The largest number of rows in one page.

    The rows of a page are read from the row store in one command and returned in one response, so the limit bounds
    the size of both. At 100 rows a walk over a few thousand workers still needs only a few dozen requests.
    """
    MAX_NAME_QUERY_CHARACTERS: int = 100
    """The longest ``name_contains`` value accepted, which is the longest worker name."""
    MAX_CURSOR_CHARACTERS: int = 1024
    """The longest cursor accepted.

    A name cursor holds the lower-cased name. A 100-character name is at most 400 bytes in UTF-8 after lower-casing,
    and with the worker ID the cursor encodes to under 700 characters.
    """


class WorkerListSort(StrEnum):
    """Represent the orders available for a worker page.

    Every order breaks ties by worker ID, ascending. Each order is therefore total, and a cursor points to exactly one
    position.
    """

    UPTIME = "uptime"
    """Longest total uptime first."""
    REQUESTS_FULFILLED = "requests_fulfilled"
    """Most jobs completed first."""
    KUDOS_REWARDS = "kudos_rewards"
    """Most kudos earned first."""
    NAME = "name"
    """Worker name ascending, ignoring case."""

    @property
    def descending(self) -> bool:
        """Return whether the order puts larger sort values first."""
        return self is not WorkerListSort.NAME


DEFAULT_WORKER_LIST_SORT: WorkerListSort = WorkerListSort.UPTIME
"""The order of a page when the request does not set one.

Workers with a long service record come first. Names chosen to sort first alphabetically do not crowd the first page.
"""

SortValue = str | int
"""The first element of a sort key: a lower-cased name, or a count."""

_LARGEST_COUNT: int = 2**63 - 1
"""The largest value of the 64-bit integer columns that hold the counts."""

WALK_KEY_CHARACTERS: int = 16
"""The length of a walk key, in hexadecimal characters.

The key tells walks apart. It does not protect anything, because a crafted cursor only starts a walk at a position
chosen by the caller. 64 bits keep an accidental match between two different walks out of reach and keep the cursor
short.
"""

MAX_MISSING_ROW_SHARE: float = 0.1
"""The largest share of a page's workers that can lack a stored row while the page is still served.

A few rows are missing in normal operation: a worker registered after the last write of the row store, or deleted
since the last refresh of ``worker_list_entries``. Both are rare against the workers of one page, and the missing
workers are left out of it. More than a tenth missing means the row hashes were lost and the row store is being
rebuilt. Such a page would show a near-empty list while ``total`` still counts every worker, so the request is refused
with a ``Retry-After`` instead. A tenth keeps a full page of ``WorkerPageLimits.MAX_LIMIT`` rows close to full and
leaves room for several deletions between two refreshes of the entries.
"""

TOLERATED_MISSING_ROWS: int = 2
"""The number of missing rows up to which a page is not refused by the ``MAX_MISSING_ROW_SHARE`` rule.

On a small page, such as one requested with a limit of 1 to 3 or a last page with a few workers, a single missing row
is already more than ``MAX_MISSING_ROW_SHARE``. Refusing it would block a walk with a small limit for as long as the
row stays missing, which can be until the next refresh of the entries. A page whose rows are all missing is still
refused, because it would be served empty with a cursor.
"""


def page_is_missing_too_many_rows(page_worker_count: int, missing_row_count: int) -> bool:
    """Return whether a page with rows missing from the row store is refused instead of served without them.

    A page is refused when every one of its rows is missing, or when more than ``TOLERATED_MISSING_ROWS`` rows and more
    than ``MAX_MISSING_ROW_SHARE`` of its workers are missing.

    Args:
        page_worker_count: The number of worker IDs returned by the page query.
        missing_row_count: The number of those workers without a stored row.
    """
    if page_worker_count == 0:
        return False
    if missing_row_count == page_worker_count:
        return True
    if missing_row_count <= TOLERATED_MISSING_ROWS:
        return False
    return missing_row_count > page_worker_count * MAX_MISSING_ROW_SHARE


class InvalidWorkerCursorError(ValueError):
    """Raised when a cursor was not produced by this module, or belongs to another sort order or another walk."""


@dataclass(frozen=True)
class WorkerPageCursor:
    """Represents the position after the last row of a page.

    The encoded form is URL-safe base64 of a small JSON document, which clients treat as opaque. The cursor holds the
    values of the last row and a digest of the walk's filters, so it is not signed. A crafted cursor only starts a walk
    at a position chosen by the caller.
    """

    FORMAT_VERSION = 1
    """The version of the encoded form. A cursor of another version is refused, so the form can change later."""

    sort: WorkerListSort
    """The order of the page that the cursor follows. A cursor continues a walk only in the same order."""
    sort_value: SortValue
    """The last row's value of the sort column, as read from the database."""
    worker_id: str
    """The ID of the last row, which breaks ties in ``sort_value``."""
    walk_key: str
    """The digest of the walk's filters, view and account, from ``WorkerPageFilters.walk_key``."""

    def encode(self) -> str:
        """Return the opaque string that a client passes back as ``cursor``."""
        document = {
            "v": self.FORMAT_VERSION,
            "s": self.sort.value,
            "k": self.sort_value,
            "i": self.worker_id,
            "w": self.walk_key,
        }
        serialized = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, encoded: str, *, expected_sort: WorkerListSort, expected_walk_key: str) -> WorkerPageCursor:
        """Parse a cursor passed back by a client and check that it continues the walk of the current request.

        Args:
            encoded: The ``next_cursor`` of an earlier page.
            expected_sort: The order of the current request.
            expected_walk_key: The walk key of the current request's filters, view and account.

        Returns:
            The decoded position.

        Raises:
            InvalidWorkerCursorError: The cursor is too long, malformed, of another version, of another sort order,
                of another walk, or holds a value of the wrong type or range for its order.
        """
        if len(encoded) > WorkerPageLimits.MAX_CURSOR_CHARACTERS:
            raise InvalidWorkerCursorError("cursor is longer than the longest cursor issued by this server")
        padding = "=" * (-len(encoded) % 4)
        try:
            document = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError) as err:
            raise InvalidWorkerCursorError("cursor was not issued by this server") from err
        if not isinstance(document, dict) or document.get("v") != cls.FORMAT_VERSION:
            raise InvalidWorkerCursorError("cursor was not issued by this server")
        if document.get("s") != expected_sort.value:
            raise InvalidWorkerCursorError("cursor continues a walk in another sort order")
        if document.get("w") != expected_walk_key:
            raise InvalidWorkerCursorError(
                "cursor continues a walk with other filters, another view or another account. Request the first page again.",
            )
        worker_id = document.get("i")
        sort_value = document.get("k")
        if not isinstance(worker_id, str) or not _is_uuid(worker_id):
            raise InvalidWorkerCursorError("cursor does not hold a valid worker ID")
        if not _sort_value_fits(sort_value, expected_sort):
            raise InvalidWorkerCursorError("cursor holds a value of the wrong type or range for its sort order")
        return cls(sort=expected_sort, sort_value=sort_value, worker_id=worker_id, walk_key=expected_walk_key)


def _is_uuid(candidate: str) -> bool:
    """Return whether ``candidate`` parses as a UUID."""
    try:
        uuid.UUID(candidate)
    except ValueError:
        return False
    return True


def _sort_value_fits(candidate: object, sort: WorkerListSort) -> TypeGuard[SortValue]:
    """Return whether ``candidate`` has the type and range of a sort column value under ``sort``.

    The value is bound into the keyset query, so a count outside the range of a 64-bit column would fail in the
    database instead of being refused here.
    """
    if sort is WorkerListSort.NAME:
        return isinstance(candidate, str)
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        return False
    return -_LARGEST_COUNT - 1 <= candidate <= _LARGEST_COUNT


@dataclass(frozen=True)
class WorkerPageFilters:
    """Represents the filters in a request. A field left as None does not narrow the list.

    The database query applies the filters with the same visibility as the caller's view. A filter on the owner
    matches, in the public view, only the workers of owners who made their workers public. A filter therefore cannot
    reveal an owner hidden from the caller.
    """

    worker_type: str | None = None
    """Only workers of this type: image, text or interrogation."""
    model: str | None = None
    """Only workers advertising this model name, compared exactly."""
    team_id: str | None = None
    """Only workers in the team with this ID."""
    owner_id: int | None = None
    """Only workers owned by this account."""
    name_contains: str | None = None
    """Only workers whose name contains this text, ignoring case."""
    online: bool | None = None
    """Only workers that are online (True) or offline (False)."""

    @property
    def applied_names(self) -> list[str]:
        """Return the names of the filters in use, in a fixed order, for a bounded metric label."""
        candidate_filters = {
            "type": self.worker_type,
            "model": self.model,
            "team": self.team_id,
            "owner": self.owner_id,
            "name": self.name_contains,
            "online": self.online,
        }
        return [filter_name for filter_name, filter_value in candidate_filters.items() if filter_value is not None]

    def walk_key(self, view: str, account_id: int | None) -> str:
        """Return the digest that ties a cursor to the walk of these filters, in ``view``, for ``account_id``.

        The view and the account are part of the key because they change which workers are listed. A public
        ``owner_id`` filter matches fewer workers than a moderator's, and the owner's list holds one account's workers.
        The filters are hashed after the request parser has normalized them, so the same request gives the same key.
        The search text is hashed and not stored, so it does not appear in the cursor.

        Args:
            view: The caller's view, as the value of ``horde.worker_rows.WorkerListView``.
            account_id: The account of the owner's list, or None for the list of every account.

        Returns:
            A hexadecimal digest of ``WALK_KEY_CHARACTERS`` characters.
        """
        walk = [
            view,
            account_id,
            self.worker_type,
            self.model,
            self.team_id,
            self.owner_id,
            self.name_contains,
            self.online,
        ]
        serialized = json.dumps(walk, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()[:WALK_KEY_CHARACTERS]
