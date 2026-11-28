# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behaviour of ``worker_list_entries`` and of the page query that reads it.

The refresh job copies the columns that order and filter the paged worker list from ``workers`` into
``worker_list_entries``. ``page_registered_workers`` reads one page of worker IDs from those entries. These tests
create workers in the database, run the job directly and read pages through the function. Every test module has its
own schema, so the workers of earlier tests in this module stay registered, and each test narrows its pages to its own
workers with a filter unless it walks the whole list.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.conftest import _QueryRecorder

if TYPE_CHECKING:
    from flask import Flask

    from horde.classes.base.worker import WorkerTemplate
    from horde.worker_pages import WorkerListSort, WorkerPageFilters
    from horde.worker_rows import WorkerListView
    from tests.fixture_types import ApiUser, MakeApiUser

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

SMALL_PAGE: int = 3
"""A page limit small enough that every walk crosses several pages."""
LARGE_PAGE: int = 10_000
"""A page limit larger than every worker created by this module, so one page reads a whole filtered list."""
WALK_PAGE_LIMIT: int = 1_000
"""The most pages a walk reads before the test fails, so a cursor that does not advance cannot loop forever."""
STALE_CHECK_IN_AGE: timedelta = timedelta(days=30)
"""How long ago an inactive worker last checked in. Well past the five minutes after which a worker is inactive."""
COUNT_COLUMNS_BY_SORT: dict[str, str] = {
    "uptime": "uptime",
    "requests_fulfilled": "fulfilments",
    "kudos_rewards": "kudos",
}
"""The ``workers`` column behind each count order, by the order's value."""
IMAGE_MODEL: str = "stable_diffusion"
"""A model advertised by the image workers of the model filter tests."""
TEXT_MODEL: str = "elinas/chronos-70b-v2"
"""A model advertised by the text workers of the model filter tests."""


@dataclass(frozen=True)
class StoredEntry:
    """Represents the values of one worker as ``worker_list_entries`` holds them, with IDs as text."""

    worker_id: str
    """The worker ID."""
    worker_type: str
    """The worker's polymorphic identity."""
    user_id: int | None
    """The owner's account ID."""
    team_id: str | None
    """The team ID, or None."""
    lower_name: str
    """The name lower-cased with ``str.lower()``."""
    uptime: int
    """The uptime."""
    fulfilments: int
    """The number of completed jobs."""
    kudos: int
    """The kudos earned."""


def _worker_classes() -> list[type[WorkerTemplate]]:
    """Return the worker classes of the list."""
    from horde.classes.kobold.worker import TextWorker
    from horde.classes.stable.interrogation_worker import InterrogationWorker
    from horde.classes.stable.worker import ImageWorker

    return [ImageWorker, TextWorker, InterrogationWorker]


def _unique_name(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _create_worker(
    app: Flask,
    owner: ApiUser,
    name: str,
    *,
    worker_class: type[WorkerTemplate] | None = None,
    models: Collection[str] = (),
    **column_values: object,
) -> str:
    """Create a worker of ``worker_class`` owned by ``owner`` with ``column_values``, and return its ID.

    The worker is written straight to the database, as a pop does when a worker first checks in.
    """
    from horde.classes.base.worker import WorkerModel
    from horde.classes.kobold.worker import TextWorker
    from horde.flask import db

    with app.app_context():
        worker = (worker_class or TextWorker)(name=name, user_id=owner.id)
        for column_name, column_value in column_values.items():
            setattr(worker, column_name, column_value)
        db.session.add(worker)
        db.session.commit()
        db.session.add_all(WorkerModel(worker_id=worker.id, model=model) for model in models)
        db.session.commit()
        return str(worker.id)


def _update_worker(app: Flask, worker_id: str, **column_values: object) -> None:
    """Write ``column_values`` to the worker's row directly, as the horde does on a check-in or an owner's change."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    with app.app_context():
        db.session.query(WorkerTemplate).filter(WorkerTemplate.id == uuid.UUID(worker_id)).update(
            {getattr(WorkerTemplate, column_name): column_value for column_name, column_value in column_values.items()},
        )
        db.session.commit()


def _delete_worker(app: Flask, worker_id: str) -> None:
    """Delete a worker, as its owner or a moderator does through the API."""
    from horde.classes.base.worker import WorkerTemplate
    from horde.flask import db

    with app.app_context():
        db.session.get(WorkerTemplate, uuid.UUID(worker_id)).delete()


def _create_team(app: Flask, owner: ApiUser) -> str:
    """Create a team owned by ``owner`` and return its ID."""
    from horde.classes.base.team import Team

    with app.app_context():
        team = Team(owner_id=owner.id, name=_unique_name("Entry Team"))
        team.create()
        return str(team.id)


def _set_public_workers(app: Flask, owner: ApiUser, *, public: bool) -> None:
    """Set whether ``owner`` shows the owner of their workers to everyone."""
    from horde.classes.base.user import User
    from horde.flask import db

    with app.app_context():
        db.session.query(User).filter(User.id == owner.id).update({User.public_workers: public})
        db.session.commit()


def _refresh() -> None:
    """Run the refresh job of the quorum node."""
    from horde.database.threads import refresh_worker_list_entries

    refresh_worker_list_entries()


def _stored_entries(app: Flask) -> dict[str, StoredEntry]:
    """Return every entry of ``worker_list_entries``, keyed by worker ID."""
    from horde.classes.base.worker import WorkerListEntry
    from horde.flask import db

    with app.app_context():
        return {
            str(entry.worker_id): StoredEntry(
                worker_id=str(entry.worker_id),
                worker_type=entry.worker_type,
                user_id=entry.user_id,
                team_id=str(entry.team_id) if entry.team_id is not None else None,
                lower_name=entry.lower_name,
                uptime=entry.uptime,
                fulfilments=entry.fulfilments,
                kudos=entry.kudos,
            )
            for entry in db.session.query(WorkerListEntry).all()
        }


def _registered_workers(app: Flask) -> dict[str, StoredEntry]:
    """Return the values that an entry of each registered worker of a listed type must hold, keyed by worker ID."""
    from horde.flask import db

    with app.app_context():
        return {
            str(worker.id): StoredEntry(
                worker_id=str(worker.id),
                worker_type=worker.worker_type,
                user_id=worker.user_id,
                team_id=str(worker.team_id) if worker.team_id is not None else None,
                lower_name=worker.name.lower(),
                uptime=worker.uptime,
                fulfilments=worker.fulfilments,
                kudos=worker.kudos,
            )
            for worker_class in _worker_classes()
            for worker in db.session.query(worker_class).populate_existing().all()
        }


def _page(
    app: Flask,
    *,
    sort: WorkerListSort | None = None,
    filters: WorkerPageFilters | None = None,
    limit: int = LARGE_PAGE,
    cursor: object = None,
    view: WorkerListView | None = None,
    account_id: int | None = None,
    active_worker_ids: Collection[str] | None = None,
):
    """Read one page through ``page_registered_workers`` with the public view and the default order unless set."""
    from horde.database.functions import page_registered_workers
    from horde.worker_pages import DEFAULT_WORKER_LIST_SORT, WorkerPageFilters
    from horde.worker_rows import WorkerListView

    with app.app_context():
        return page_registered_workers(
            filters=filters or WorkerPageFilters(),
            sort=sort or DEFAULT_WORKER_LIST_SORT,
            limit=limit,
            after=cursor,
            view=view or WorkerListView.PUBLIC,
            account_id=account_id,
            active_worker_ids=active_worker_ids,
        )


def _walk(
    app: Flask,
    *,
    limit: int = SMALL_PAGE,
    between_pages: Callable[[int], None] | None = None,
    **page_arguments: object,
) -> tuple[list[str], list[int]]:
    """Read every page of a walk and return the worker IDs in order and the total reported by each page."""
    worker_ids: list[str] = []
    totals: list[int] = []
    cursor = None
    for _ in range(WALK_PAGE_LIMIT):
        if totals and between_pages is not None:
            between_pages(len(totals))
        page = _page(app, limit=limit, cursor=cursor, **page_arguments)
        worker_ids.extend(page.worker_ids)
        totals.append(page.total)
        if page.next_cursor is None:
            return worker_ids, totals
        cursor = page.next_cursor
    raise AssertionError("the walk did not end")


def _name_filter(text: str) -> WorkerPageFilters:
    from horde.worker_pages import WorkerPageFilters

    return WorkerPageFilters(name_contains=text)


class TestRefresh:
    """The refresh job keeps one entry per registered worker of a listed type, equal to the worker's columns."""

    def test_after_a_refresh_the_entries_equal_the_registered_workers(self, app: Flask, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        team_id = _create_team(app, owner)
        for worker_class in _worker_classes():
            _create_worker(app, owner, _unique_name("Equal ÅNGSTRÖM"), worker_class=worker_class, uptime=12, kudos=7)
        _create_worker(app, owner, _unique_name("Equal Team"), team_id=uuid.UUID(team_id), fulfilments=3)

        _refresh()

        assert _stored_entries(app) == _registered_workers(app)

    def test_a_rename_a_team_change_a_deletion_and_a_new_worker_are_each_picked_up_by_the_next_refresh(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        team_id = _create_team(app, owner)
        renamed_id = _create_worker(app, owner, _unique_name("Before Rename"))
        moved_id = _create_worker(app, owner, _unique_name("Moving"))
        deleted_id = _create_worker(app, owner, _unique_name("Leaving"))
        _refresh()
        new_name = _unique_name("After RENAME")

        _update_worker(app, renamed_id, name=new_name)
        _update_worker(app, moved_id, team_id=uuid.UUID(team_id))
        _delete_worker(app, deleted_id)
        new_id = _create_worker(app, owner, _unique_name("Joining"))
        _refresh()

        entries = _stored_entries(app)
        assert entries[renamed_id].lower_name == new_name.lower()
        assert entries[moved_id].team_id == team_id
        assert deleted_id not in entries
        assert new_id in entries
        assert entries == _registered_workers(app)

    def test_a_refresh_without_changes_issues_only_its_reads(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        _create_worker(app, owner, _unique_name("Quiet"))
        _refresh()
        with assert_query_count() as fewer_workers_queries:
            _refresh()

        for worker_class in _worker_classes():
            _create_worker(app, owner, _unique_name("Quiet More"), worker_class=worker_class)
        _refresh()
        with assert_query_count() as more_workers_queries:
            _refresh()

        assert len(fewer_workers_queries) == 2, fewer_workers_queries.describe()
        assert len(fewer_workers_queries.of_kind("SELECT")) == 2, fewer_workers_queries.describe()
        assert fewer_workers_queries.commits == [], fewer_workers_queries.describe()
        assert len(more_workers_queries) == len(fewer_workers_queries), more_workers_queries.describe()

    def test_a_refresh_writes_only_the_changed_workers_in_one_statement(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = [_create_worker(app, owner, _unique_name("Counting")) for _ in range(3)]
        _refresh()
        for worker_id in worker_ids[:2]:
            _update_worker(app, worker_id, uptime=600)

        with assert_query_count() as queries:
            _refresh()

        assert len(queries.of_kind("SELECT")) == 2, queries.describe()
        assert len(queries.of_kind("INSERT")) == 1, queries.describe()
        assert len(queries) == 3, queries.describe()
        assert len(queries.commits) == 1, queries.describe()
        entries = _stored_entries(app)
        assert [entries[worker_id].uptime for worker_id in worker_ids] == [600, 600, 0]


class TestRowStoreReconciliation:
    """The refresh job corrects the row store against the registered workers."""

    def test_rows_of_an_unregistered_worker_are_removed(self, app: Flask) -> None:
        from horde.horde_redis import horde_redis
        from horde.worker_rows import WORKER_ROWS_LIFETIME, WorkerListView, read_worker_rows

        unregistered_worker_id = str(uuid.uuid4())
        for view in WorkerListView:
            horde_redis.horde_r_hset_many(view.rows_key, {unregistered_worker_id: b"{}"}, WORKER_ROWS_LIFETIME)

        _refresh()

        for view in WorkerListView:
            assert read_worker_rows(view, [unregistered_worker_id]) == [None]

    def test_a_registered_worker_without_rows_is_marked_dirty(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.horde_redis import horde_redis
        from horde.worker_rows import WORKER_ROWS_DIRTY_KEY, remove_worker_rows

        owner = make_api_user(trusted=True, kudos=100)
        worker_id = _create_worker(app, owner, _unique_name("Rowless"))
        remove_worker_rows([worker_id])
        horde_redis.horde_r_delete(WORKER_ROWS_DIRTY_KEY)

        _refresh()

        assert horde_redis.horde_r.sismember(WORKER_ROWS_DIRTY_KEY, worker_id)

    def test_after_the_row_hashes_are_lost_one_refresh_marks_every_registered_worker_dirty_in_chunks(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import horde.worker_rows as worker_rows_module
        from horde.horde_redis import horde_redis
        from horde.worker_rows import WORKER_ROWS_DIRTY_KEY, WorkerListView

        chunk_size = 2
        monkeypatch.setattr(worker_rows_module, "WORKER_ROWS_DIRTY_MARK_CHUNK_SIZE", chunk_size)
        monkeypatch.setattr(worker_rows_module, "WORKER_ROWS_RECONCILE_LIMIT", 1)
        marked_batches: list[list[str]] = []
        mark_worker_rows_dirty = worker_rows_module.mark_worker_rows_dirty

        def _record_marks(worker_ids: Collection[str]) -> None:
            marked_batches.append(list(worker_ids))
            mark_worker_rows_dirty(worker_ids)

        monkeypatch.setattr(worker_rows_module, "mark_worker_rows_dirty", _record_marks)
        owner = make_api_user(trusted=True, kudos=100)
        for _ in range(5):
            _create_worker(app, owner, _unique_name("Rebuilt"))
        for view in WorkerListView:
            horde_redis.horde_r_delete(view.rows_key)
        horde_redis.horde_r_delete(WORKER_ROWS_DIRTY_KEY)

        _refresh()

        registered_worker_ids = set(_registered_workers(app))
        dirty_members = horde_redis.horde_r.smembers(WORKER_ROWS_DIRTY_KEY)
        dirty_worker_ids = {member.decode() if isinstance(member, bytes) else member for member in dirty_members}
        assert dirty_worker_ids == registered_worker_ids
        assert all(len(batch) <= chunk_size for batch in marked_batches), marked_batches
        assert sum(len(batch) for batch in marked_batches) == len(registered_worker_ids)


class TestWalks:
    """A walk in any order lists every registered worker once, active or not, in the order of the list contract."""

    @pytest.mark.parametrize("sort_value", ["uptime", "requests_fulfilled", "kudos_rewards", "name"])
    def test_a_walk_lists_every_registered_worker_once_including_inactive_ones(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        sort_value: str,
    ) -> None:
        from horde.worker_pages import WorkerListSort

        sort = WorkerListSort(sort_value)
        owner = make_api_user(trusted=True, kudos=100)
        for index, worker_class in enumerate(_worker_classes()):
            _create_worker(
                app,
                owner,
                _unique_name(f"Walker {index}"),
                worker_class=worker_class,
                uptime=index % 2,
                fulfilments=index % 2,
                kudos=index % 2,
            )
        inactive_id = _create_worker(app, owner, _unique_name("Walker Asleep"), last_check_in=datetime.utcnow() - STALE_CHECK_IN_AGE)
        _refresh()
        entries = _stored_entries(app)

        walked_ids, totals = _walk(app, sort=sort)

        whole_list = _page(app, sort=sort)
        assert whole_list.next_cursor is None
        assert walked_ids == whole_list.worker_ids
        assert len(walked_ids) == len(set(walked_ids)) == len(entries)
        assert set(walked_ids) == set(entries)
        assert inactive_id in walked_ids
        assert set(totals) == {len(entries)}
        if sort.descending:
            count_column = COUNT_COLUMNS_BY_SORT[sort.value]
            expected_ids = sorted(entries, key=lambda worker_id: (-getattr(entries[worker_id], count_column), worker_id))
            assert walked_ids == expected_ids

    @pytest.mark.parametrize("sort_value", list(COUNT_COLUMNS_BY_SORT))
    def test_a_count_order_descends_and_breaks_ties_by_ascending_id(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        sort_value: str,
    ) -> None:
        from horde.worker_pages import WorkerListSort

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        worker_ids = [
            _create_worker(app, owner, f"Tied {token} {index}", **{COUNT_COLUMNS_BY_SORT[sort_value]: count})
            for index, count in enumerate([200, 100, 100, 50])
        ]
        _refresh()

        walked_ids, _ = _walk(app, sort=WorkerListSort(sort_value), filters=_name_filter(token), limit=1)

        assert walked_ids == [worker_ids[0], *sorted(worker_ids[1:3]), worker_ids[3]]

    def test_the_name_order_ascends_ignoring_case(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.worker_pages import WorkerListSort

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        beta_id = _create_worker(app, owner, f"beta {token}")
        alpha_id = _create_worker(app, owner, f"Alpha {token}")
        gamma_id = _create_worker(app, owner, f"GAMMA {token}")
        _refresh()

        walked_ids, _ = _walk(app, sort=WorkerListSort.NAME, filters=_name_filter(token), limit=1)

        assert walked_ids == [alpha_id, beta_id, gamma_id]

    def test_without_a_sort_the_workers_with_the_longest_uptime_come_first(self, app: Flask, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        newcomer_id = _create_worker(app, owner, f"Able {token} Newcomer", uptime=10)
        veteran_id = _create_worker(app, owner, f"Baker {token} Veteran", uptime=5000)
        regular_id = _create_worker(app, owner, f"Charlie {token} Regular", uptime=300)
        _refresh()

        page = _page(app, filters=_name_filter(token))

        assert page.worker_ids == [veteran_id, regular_id, newcomer_id]


class TestWalkingWhileWorkersComeAndGo:
    """Workers register, stop checking in and are deleted while a client walks the list."""

    def test_workers_joining_and_leaving_between_pages_do_not_shift_the_rest_of_the_walk(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        from horde.worker_pages import WorkerListSort

        owner = make_api_user(trusted=True, kudos=100)
        prefix = f"Churn {uuid.uuid4().hex[:6]}"
        worker_ids = [_create_worker(app, owner, f"{prefix} {index:02d}") for index in range(8)]
        _refresh()
        joined: dict[str, str] = {}

        def workers_change_between_pages(pages_read: int) -> None:
            if pages_read != 1:
                return
            # One listed worker stops checking in and one unlisted worker is deleted. One new worker sorts before the
            # cursor and one after it.
            _update_worker(app, worker_ids[1], last_check_in=datetime.utcnow() - STALE_CHECK_IN_AGE)
            _delete_worker(app, worker_ids[6])
            joined["behind"] = _create_worker(app, owner, f"{prefix} 00a")
            joined["ahead"] = _create_worker(app, owner, f"{prefix} 99")
            _refresh()

        walked_ids, _ = _walk(
            app,
            sort=WorkerListSort.NAME,
            filters=_name_filter(prefix),
            between_pages=workers_change_between_pages,
        )

        assert len(walked_ids) == len(set(walked_ids)), "a worker was listed twice"
        assert set(worker_ids) - {worker_ids[6]} <= set(walked_ids), "a worker that was not renamed or deleted was skipped"
        assert worker_ids[6] not in walked_ids, "a worker deleted before its page was still listed"
        assert joined["ahead"] in walked_ids, "a worker that joined ahead of the cursor was not listed"
        assert joined["behind"] not in walked_ids, "a worker that joined behind the cursor is listed only by the next walk"

    def test_a_page_after_the_last_remaining_worker_is_empty_and_still_reports_the_total(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        from horde.worker_pages import WorkerListSort

        owner = make_api_user(trusted=True, kudos=100)
        prefix = f"Tail {uuid.uuid4().hex[:6]}"
        worker_ids = [_create_worker(app, owner, f"{prefix} {index}") for index in range(3)]
        _refresh()
        first_page = _page(app, sort=WorkerListSort.NAME, filters=_name_filter(prefix), limit=2)
        _delete_worker(app, worker_ids[2])
        _refresh()

        next_page = _page(app, sort=WorkerListSort.NAME, filters=_name_filter(prefix), limit=2, cursor=first_page.next_cursor)

        assert first_page.worker_ids == worker_ids[:2]
        assert first_page.total == 3
        assert next_page.worker_ids == []
        assert next_page.next_cursor is None
        assert next_page.total == 2


class TestOwnerPrivacy:
    """Owners decide whether the public may see which workers are theirs."""

    def test_the_public_cannot_find_a_private_owners_workers_by_owner(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.worker_pages import WorkerPageFilters

        private_owner = make_api_user(trusted=True, kudos=100)
        _set_public_workers(app, private_owner, public=False)
        _create_worker(app, private_owner, _unique_name("Private"))
        _refresh()

        page = _page(app, filters=WorkerPageFilters(owner_id=private_owner.id))

        assert page.worker_ids == []
        assert page.total == 0, "the count alone would reveal how many workers the account runs"

    def test_an_owner_who_publishes_their_workers_is_found_by_owner_at_once(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.worker_pages import WorkerPageFilters

        owner = make_api_user(trusted=True, kudos=100)
        _set_public_workers(app, owner, public=False)
        worker_id = _create_worker(app, owner, _unique_name("Published"))
        _refresh()

        _set_public_workers(app, owner, public=True)
        page = _page(app, filters=WorkerPageFilters(owner_id=owner.id))

        assert page.worker_ids == [worker_id]
        assert page.total == 1

    def test_a_moderator_finds_a_private_owners_workers_by_owner(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.worker_pages import WorkerPageFilters
        from horde.worker_rows import WorkerListView

        private_owner = make_api_user(trusted=True, kudos=100)
        _set_public_workers(app, private_owner, public=False)
        worker_id = _create_worker(app, private_owner, _unique_name("Reviewed"))
        _refresh()

        page = _page(app, filters=WorkerPageFilters(owner_id=private_owner.id), view=WorkerListView.MODERATOR)

        assert page.worker_ids == [worker_id]


class TestFilters:
    """Each filter returns exactly the matching workers, and the filters combine freely."""

    def test_the_model_and_type_filters_find_the_workers_serving_a_model(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.classes.stable.worker import ImageWorker
        from horde.worker_pages import WorkerPageFilters
        from horde.worker_rows import WorkerListView

        owner = make_api_user(trusted=True, kudos=100)
        text_worker_id = _create_worker(app, owner, _unique_name("Model Scribe"), models=[TEXT_MODEL])
        image_worker_id = _create_worker(app, owner, _unique_name("Model Painter"), worker_class=ImageWorker, models=[IMAGE_MODEL])
        _refresh()

        by_model = _page(app, filters=WorkerPageFilters(owner_id=owner.id, model=IMAGE_MODEL), view=WorkerListView.MODERATOR)
        by_type = _page(app, filters=WorkerPageFilters(owner_id=owner.id, worker_type="text"), view=WorkerListView.MODERATOR)
        by_model_and_type = _page(
            app,
            filters=WorkerPageFilters(owner_id=owner.id, model=IMAGE_MODEL, worker_type="text"),
            view=WorkerListView.MODERATOR,
        )

        assert by_model.worker_ids == [image_worker_id]
        assert by_type.worker_ids == [text_worker_id]
        assert by_model_and_type.worker_ids == []
        assert by_model_and_type.total == 0

    def test_the_team_filter_follows_a_worker_moving_into_a_team(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.worker_pages import WorkerPageFilters

        owner = make_api_user(trusted=True, kudos=100)
        in_team_id = _create_worker(app, owner, _unique_name("Team Scribe"))
        _create_worker(app, owner, _unique_name("Solo Scribe"))
        team_id = _create_team(app, owner)
        _refresh()

        _update_worker(app, in_team_id, team_id=uuid.UUID(team_id))
        _refresh()
        page = _page(app, filters=WorkerPageFilters(team_id=team_id))

        assert page.worker_ids == [in_team_id]

    def test_a_name_search_lists_the_workers_whose_name_contains_the_text_in_any_case(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
    ) -> None:
        from horde.worker_pages import WorkerListSort

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        first_match_id = _create_worker(app, owner, f"Lantern{token} Scribe")
        second_match_id = _create_worker(app, owner, f"Old {token.upper()} Scribe")
        _create_worker(app, owner, _unique_name("Candle Scribe"))
        _refresh()

        page = _page(app, sort=WorkerListSort.NAME, filters=_name_filter(token.upper()))

        assert set(page.worker_ids) == {first_match_id, second_match_id}
        assert page.total == 2

    # The search runs as a SQL LIKE match. Unescaped, "%" matches any text and "_" any one character.
    def test_a_name_search_matches_percent_and_underscore_as_plain_characters(self, app: Flask, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        literal_id = _create_worker(app, owner, f"{token} 100%_done")
        lookalike_id = _create_worker(app, owner, f"{token} 100xydone")
        _refresh()

        page = _page(app, filters=_name_filter("0%_d"))

        assert [worker_id for worker_id in page.worker_ids if worker_id in {literal_id, lookalike_id}] == [literal_id]

    def test_filters_combine(self, app: Flask, make_api_user: MakeApiUser) -> None:
        from horde.classes.stable.worker import ImageWorker
        from horde.worker_pages import WorkerPageFilters
        from horde.worker_rows import WorkerListView

        owner = make_api_user(trusted=True, kudos=100)
        stranger = make_api_user(trusted=True, kudos=100)
        team_id = _create_team(app, owner)
        token = uuid.uuid4().hex[:8]
        matching_id = _create_worker(
            app,
            owner,
            f"Combo {token} Painter",
            worker_class=ImageWorker,
            models=[IMAGE_MODEL],
            team_id=uuid.UUID(team_id),
        )
        _create_worker(app, owner, f"Combo {token} Scribe", models=[TEXT_MODEL], team_id=uuid.UUID(team_id))
        _create_worker(app, owner, f"Combo {token} Loner", worker_class=ImageWorker, models=[IMAGE_MODEL])
        _create_worker(app, stranger, f"Combo {token} Other", worker_class=ImageWorker, models=[IMAGE_MODEL], team_id=uuid.UUID(team_id))
        _refresh()
        combined_filters = WorkerPageFilters(
            worker_type="image",
            model=IMAGE_MODEL,
            team_id=team_id,
            owner_id=owner.id,
            name_contains=token,
        )

        page = _page(app, filters=combined_filters, view=WorkerListView.MODERATOR)
        owners_page = _page(app, filters=WorkerPageFilters(team_id=team_id, worker_type="image"), account_id=owner.id)

        assert page.worker_ids == [matching_id]
        assert page.total == 1
        assert owners_page.worker_ids == [matching_id]

    @pytest.mark.parametrize("online", [True, False])
    def test_the_online_filter_follows_the_given_active_workers(self, app: Flask, make_api_user: MakeApiUser, online: bool) -> None:
        from horde.worker_pages import WorkerPageFilters

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        online_id = _create_worker(app, owner, f"Running {token}")
        offline_id = _create_worker(app, owner, f"Stopped {token}")
        _refresh()

        page = _page(
            app,
            filters=WorkerPageFilters(name_contains=token, online=online),
            active_worker_ids={online_id, str(uuid.uuid4())},
        )

        assert page.worker_ids == [online_id if online else offline_id]
        assert page.total == 1

    def test_without_active_workers_the_online_filter_is_empty_without_a_statement(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        from horde.worker_pages import WorkerPageFilters

        owner = make_api_user(trusted=True, kudos=100)
        token = uuid.uuid4().hex[:8]
        worker_id = _create_worker(app, owner, f"Idle {token}")
        _refresh()

        with assert_query_count() as queries:
            online_page = _page(app, filters=WorkerPageFilters(name_contains=token, online=True), active_worker_ids=set())
        offline_page = _page(app, filters=WorkerPageFilters(name_contains=token, online=False), active_worker_ids=set())

        assert online_page.worker_ids == []
        assert online_page.total == 0
        assert online_page.next_cursor is None
        assert len(queries) == 0, queries.describe()
        assert offline_page.worker_ids == [worker_id]

    def test_the_online_filter_needs_the_active_workers(self, app: Flask) -> None:
        from horde.worker_pages import WorkerPageFilters

        with pytest.raises(ValueError, match="active workers"):
            _page(app, filters=WorkerPageFilters(online=True))


class TestOwnersListingTheirOwnWorkers:
    """Owners check on every worker they run, including private workers and workers that dropped out."""

    def test_an_owner_lists_their_private_and_offline_workers(self, app: Flask, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        stranger = make_api_user(trusted=True, kudos=100)
        _set_public_workers(app, owner, public=False)
        online_id = _create_worker(app, owner, _unique_name("Home Scribe"))
        offline_id = _create_worker(app, owner, _unique_name("Stalled Scribe"), last_check_in=datetime.utcnow() - STALE_CHECK_IN_AGE)
        _create_worker(app, stranger, _unique_name("Neighbour Scribe"))
        _refresh()

        page = _page(app, account_id=owner.id)

        assert set(page.worker_ids) == {online_id, offline_id}
        assert page.total == 2

    def test_a_walk_lists_every_owned_worker_once(self, app: Flask, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = {_create_worker(app, owner, _unique_name("Fleet Scribe")) for _ in range(5)}
        _refresh()

        walked_ids, totals = _walk(app, account_id=owner.id, limit=2)

        assert sorted(walked_ids) == sorted(worker_ids)
        assert totals == [5, 5, 5], "every page of a walk counts the whole list"

    def test_a_worker_shows_under_its_new_owner_after_the_next_refresh(self, app: Flask, make_api_user: MakeApiUser) -> None:
        previous_owner = make_api_user(trusted=True, kudos=100)
        new_owner = make_api_user(trusted=True, kudos=100)
        worker_id = _create_worker(app, previous_owner, _unique_name("Handed Scribe"))
        _refresh()

        _update_worker(app, worker_id, user_id=new_owner.id)
        _refresh()

        assert _page(app, account_id=new_owner.id).worker_ids == [worker_id]
        assert _page(app, account_id=previous_owner.id).total == 0

    def test_an_account_without_workers_gets_an_empty_list(self, app: Flask, make_api_user: MakeApiUser) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        moderator = make_api_user(moderator=True)
        _create_worker(app, owner, _unique_name("Watched Scribe"))
        _refresh()

        page = _page(app, account_id=moderator.id)

        assert page.worker_ids == []
        assert page.next_cursor is None
        assert page.total == 0


class TestPageStatements:
    """A page is one statement whatever the number of workers."""

    def test_the_page_statement_count_does_not_grow_with_the_number_of_workers(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        from horde.classes.stable.worker import ImageWorker
        from horde.worker_pages import WorkerPageFilters

        owner = make_api_user(trusted=True, kudos=100)
        _create_worker(app, owner, _unique_name("Budget Scribe"), models=[TEXT_MODEL])
        _refresh()
        filters = WorkerPageFilters(model=TEXT_MODEL, online=False)
        with assert_query_count() as fewer_workers_queries:
            first_page = _page(app, filters=filters, account_id=owner.id, limit=1, active_worker_ids={str(uuid.uuid4())})
        for _ in range(3):
            _create_worker(app, owner, _unique_name("Budget Scribe"), models=[TEXT_MODEL])
            _create_worker(app, owner, _unique_name("Budget Painter"), worker_class=ImageWorker, models=[IMAGE_MODEL])
        _refresh()

        with assert_query_count() as more_workers_queries:
            later_page = _page(app, filters=filters, account_id=owner.id, limit=1, active_worker_ids={str(uuid.uuid4())})
        with assert_query_count() as cursor_queries:
            _page(app, filters=filters, account_id=owner.id, limit=1, cursor=later_page.next_cursor, active_worker_ids=set())

        assert first_page.total == 1
        assert later_page.total == 4
        assert len(fewer_workers_queries) == 1, fewer_workers_queries.describe()
        assert len(more_workers_queries) == 1, more_workers_queries.describe()
        assert len(cursor_queries) == 1, cursor_queries.describe()

    def test_a_cursor_past_the_end_adds_one_count_statement(
        self,
        app: Flask,
        make_api_user: MakeApiUser,
        assert_query_count: QueryCounter,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        worker_ids = [_create_worker(app, owner, _unique_name("Ending")) for _ in range(2)]
        _refresh()
        first_page = _page(app, account_id=owner.id, limit=1)
        for worker_id in worker_ids:
            _delete_worker(app, worker_id)
        _refresh()

        with assert_query_count() as queries:
            past_end_page = _page(app, account_id=owner.id, limit=1, cursor=first_page.next_cursor)

        assert past_end_page.worker_ids == []
        assert past_end_page.total == 0
        assert len(queries) == 2, queries.describe()
