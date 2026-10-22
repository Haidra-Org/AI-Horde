# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Decision-matrix tests for ``horde.classes.base.worker.Worker.can_generate``.

``can_generate`` is the most-called function on the request path
(per the perf-iteration tracing). Its base form decides worker availability
based on attribute-level state on the worker, the waiting prompt, and the
user. No DB queries are performed by the method itself, so we test the
pure decision logic with mocks for both ``self`` and the ``waiting_prompt``.

Each parametrised case names a single varying input; the rest are held at
``allow``-defaults that pass every other gate. A failure tells you exactly
which gate broke.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, MetaData, ScalarSelect, String, Table, create_engine, insert, select
from sqlalchemy.dialects import postgresql, sqlite

from horde.classes.base.waiting_prompt import RequestEligibilityInputs, WaitingPrompt
from horde.classes.base.worker import (
    Worker,
    WorkerEligibilityInputs,
    WorkerModel,
    worker_blacklist_words_clause,
    worker_model_names_clause,
)
from horde.classes.kobold.worker import TextWorker, text_worker_softprompt_names_clause

pytestmark = pytest.mark.unit


_user_id_counter = 0


def _allowing_user(*, trusted: bool = True):
    # SimpleNamespace.__eq__ compares attribute-wise; identity by id avoids
    # accidentally treating two distinct user objects as equal.
    global _user_id_counter
    _user_id_counter += 1
    return SimpleNamespace(trusted=trusted, id=_user_id_counter)


def _allowing_worker(**overrides):
    """Return a MagicMock pre-configured to pass every default gate."""
    user = overrides.pop("user", _allowing_user())
    defaults = {
        "maintenance": False,
        "user": user,
        "nsfw": True,
        "blacklist": [],
        "id": "worker-1",
    }
    defaults.update(overrides)
    worker = MagicMock(
        spec_set=list(defaults.keys()) + ["is_stale", "get_model_names", "get_eligibility_inputs", "can_generate_with_eligibility_inputs"],
    )
    for k, v in defaults.items():
        setattr(worker, k, v)
    worker.is_stale.return_value = overrides.get("_is_stale", False)
    worker.get_model_names.return_value = []
    # The real input builder and rule chain run against the mock, so ``Worker.can_generate`` runs against the stand-in without a database.
    worker.get_eligibility_inputs = partial(Worker.get_eligibility_inputs, worker)
    worker.can_generate_with_eligibility_inputs = partial(Worker.can_generate_with_eligibility_inputs, worker)
    return worker


def _allowing_wp(**overrides):
    """Return a MagicMock waiting_prompt pre-configured to pass every default gate."""
    user = overrides.pop("user", _allowing_user())
    defaults = {
        "user": user,
        "nsfw": False,
        "trusted_workers": False,
        "prompt": "a friendly prompt",
        "worker_blacklist": False,
        "workers": [],
    }
    defaults.update(overrides)
    wp = MagicMock(spec_set=list(defaults.keys()) + ["tricked_workers", "get_model_names", "get_worker_ids", "get_eligibility_inputs"])
    for k, v in defaults.items():
        setattr(wp, k, v)
    wp.tricked_workers = [SimpleNamespace(worker_id=worker_id) for worker_id in overrides.get("_tricked_worker_ids", [])]
    wp.get_model_names.return_value = []
    wp.get_worker_ids.return_value = overrides.get("_worker_ids", [])
    wp.get_eligibility_inputs = partial(WaitingPrompt.get_eligibility_inputs, wp)
    return wp


def _request_inputs(**overrides):
    """Return request eligibility inputs that pass every default gate."""
    defaults = {
        "model_names": (),
        "model_name_set": frozenset(),
        "has_targeted_workers": False,
        "targeted_worker_ids": frozenset(),
        "tricked_worker_ids": frozenset(),
    }
    defaults.update(overrides)
    return RequestEligibilityInputs(**defaults)


def _worker_inputs(**overrides):
    """Return worker eligibility inputs that pass every default gate."""
    defaults = {
        "model_names": (),
        "softprompt_names": (),
        "blacklist_words": (),
        "owner_trusted": True,
    }
    defaults.update(overrides)
    return WorkerEligibilityInputs(**defaults)


class TestHappyPath:
    def test_default_allowing_state_returns_true(self):
        worker = _allowing_worker()
        wp = _allowing_wp()
        assert Worker.can_generate(worker, wp) == [True, None]


class TestMaintenance:
    def test_maintenance_blocks_other_users_silently(self):
        owner = _allowing_user()
        non_owner = _allowing_user()
        worker = _allowing_worker(maintenance=True, user=owner)
        wp = _allowing_wp(user=non_owner)
        # Silent rejection: reason is None so the queue does not surface it.
        assert Worker.can_generate(worker, wp) == [False, None]

    def test_maintenance_does_not_block_owner(self):
        owner = _allowing_user()
        worker = _allowing_worker(maintenance=True, user=owner)
        wp = _allowing_wp(user=owner)
        assert Worker.can_generate(worker, wp) == [True, None]


class TestStaleness:
    def test_stale_worker_is_silently_rejected(self):
        worker = _allowing_worker(_is_stale=True)
        wp = _allowing_wp()
        assert Worker.can_generate(worker, wp) == [False, None]


class TestNsfw:
    def test_nsfw_request_to_sfw_worker_rejected(self):
        worker = _allowing_worker(nsfw=False)
        wp = _allowing_wp(nsfw=True)
        assert Worker.can_generate(worker, wp) == [False, "nsfw"]

    def test_sfw_request_to_nsfw_worker_allowed(self):
        worker = _allowing_worker(nsfw=True)
        wp = _allowing_wp(nsfw=False)
        assert Worker.can_generate(worker, wp) == [True, None]

    def test_sfw_request_to_sfw_worker_allowed(self):
        worker = _allowing_worker(nsfw=False)
        wp = _allowing_wp(nsfw=False)
        assert Worker.can_generate(worker, wp) == [True, None]


class TestTrustedWorkersOnly:
    def test_request_requires_trusted_owner_untrusted_blocks(self):
        worker = _allowing_worker(user=_allowing_user(trusted=False))
        wp = _allowing_wp(trusted_workers=True)
        assert Worker.can_generate(worker, wp) == [False, "untrusted"]

    def test_request_requires_trusted_owner_trusted_passes(self):
        worker = _allowing_worker(user=_allowing_user(trusted=True))
        wp = _allowing_wp(trusted_workers=True)
        assert Worker.can_generate(worker, wp) == [True, None]


class TestTrickedWorker:
    def test_tricked_worker_rejected_with_secret_reason(self):
        worker = _allowing_worker(id="worker-1")
        wp = _allowing_wp(_tricked_worker_ids=["worker-1"])
        assert Worker.can_generate(worker, wp) == [False, "secret"]

    def test_worker_tricked_by_another_request_passes(self):
        worker = _allowing_worker(id="worker-1")
        wp = _allowing_wp(_tricked_worker_ids=["worker-2"])
        assert Worker.can_generate(worker, wp) == [True, None]


class TestBlacklist:
    def test_prompt_with_blacklisted_word_rejected(self):
        worker = _allowing_worker(blacklist=[SimpleNamespace(word="evil")])
        wp = _allowing_wp(prompt="something evil happens")
        assert Worker.can_generate(worker, wp) == [False, "blacklist"]

    def test_blacklist_match_is_case_insensitive(self):
        worker = _allowing_worker(blacklist=[SimpleNamespace(word="EVIL")])
        wp = _allowing_wp(prompt="evil things")
        assert Worker.can_generate(worker, wp) == [False, "blacklist"]

    def test_clean_prompt_passes_blacklist(self):
        worker = _allowing_worker(blacklist=[SimpleNamespace(word="evil")])
        wp = _allowing_wp(prompt="a sunny day in the park")
        assert Worker.can_generate(worker, wp) == [True, None]

    def test_empty_blacklist_short_circuits(self):
        worker = _allowing_worker(blacklist=[])
        wp = _allowing_wp(prompt="anything goes")
        assert Worker.can_generate(worker, wp) == [True, None]


class TestWorkerTargeting:
    """The ``workers`` list on a WP is an allowlist OR denylist depending on the
    ``worker_blacklist`` flag. The semantics flip and are important to lock down."""

    def test_allowlist_worker_in_list_passes(self):
        worker = _allowing_worker(id="worker-1")
        wp = _allowing_wp(
            worker_blacklist=False,
            workers=["worker-1", "worker-2"],
            _worker_ids=["worker-1", "worker-2"],
        )
        assert Worker.can_generate(worker, wp) == [True, None]

    def test_allowlist_worker_not_in_list_rejected(self):
        worker = _allowing_worker(id="worker-99")
        wp = _allowing_wp(
            worker_blacklist=False,
            workers=["worker-1", "worker-2"],
            _worker_ids=["worker-1", "worker-2"],
        )
        assert Worker.can_generate(worker, wp) == [False, "worker_id"]

    def test_denylist_worker_in_list_rejected(self):
        worker = _allowing_worker(id="worker-1")
        wp = _allowing_wp(
            worker_blacklist=True,
            workers=["worker-1"],
            _worker_ids=["worker-1"],
        )
        assert Worker.can_generate(worker, wp) == [False, "worker_id"]

    def test_denylist_worker_not_in_list_passes(self):
        worker = _allowing_worker(id="worker-99")
        wp = _allowing_wp(
            worker_blacklist=True,
            workers=["worker-1"],
            _worker_ids=["worker-1"],
        )
        assert Worker.can_generate(worker, wp) == [True, None]

    def test_empty_workers_list_passes_allowlist(self):
        # Quirk: when ``workers`` is empty, the worker_id gate is skipped
        # regardless of allowlist/denylist mode (the `len(workers)` guard).
        worker = _allowing_worker(id="worker-1")
        wp = _allowing_wp(worker_blacklist=False, workers=[], _worker_ids=[])
        assert Worker.can_generate(worker, wp) == [True, None]


class TestRejectionPriority:
    """Some gates are checked before others; a regression that reorders them
    can change the *reason* surfaced to the user even if the boolean is right."""

    def test_maintenance_takes_priority_over_nsfw(self):
        owner = _allowing_user()
        non_owner = _allowing_user()
        worker = _allowing_worker(maintenance=True, user=owner, nsfw=False)
        wp = _allowing_wp(user=non_owner, nsfw=True)
        # Maintenance check fires first → silent rejection (None), not "nsfw".
        assert Worker.can_generate(worker, wp) == [False, None]

    def test_stale_takes_priority_over_nsfw(self):
        worker = _allowing_worker(_is_stale=True, nsfw=False)
        wp = _allowing_wp(nsfw=True)
        assert Worker.can_generate(worker, wp) == [False, None]

    def test_nsfw_takes_priority_over_trusted(self):
        worker = _allowing_worker(nsfw=False, user=_allowing_user(trusted=False))
        wp = _allowing_wp(nsfw=True, trusted_workers=True)
        assert Worker.can_generate(worker, wp) == [False, "nsfw"]


class TestEligibilityInputs:
    """``can_generate_with_eligibility_inputs`` judges a worker from the inputs it is given, which the availability
    query selects as aggregates; it must not fall back to the worker's or the request's collections."""

    def test_blacklist_words_come_from_the_worker_inputs(self):
        worker = _allowing_worker(blacklist=[])
        wp = _allowing_wp(prompt="something evil happens")
        result = Worker.can_generate_with_eligibility_inputs(worker, wp, _request_inputs(), _worker_inputs(blacklist_words=("EVIL",)))
        assert result == [False, "blacklist"]

    def test_owner_trust_comes_from_the_worker_inputs(self):
        worker = _allowing_worker(user=_allowing_user(trusted=True))
        wp = _allowing_wp(trusted_workers=True)
        result = Worker.can_generate_with_eligibility_inputs(worker, wp, _request_inputs(), _worker_inputs(owner_trusted=False))
        assert result == [False, "untrusted"]

    def test_tricked_worker_ids_come_from_the_request_inputs(self):
        worker = _allowing_worker(id="worker-1")
        wp = _allowing_wp()
        request_inputs = _request_inputs(tricked_worker_ids=frozenset({"worker-1"}))
        assert Worker.can_generate_with_eligibility_inputs(worker, wp, request_inputs, _worker_inputs()) == [False, "secret"]

    def test_targeted_worker_ids_come_from_the_request_inputs(self):
        worker = _allowing_worker(id="worker-99")
        wp = _allowing_wp(worker_blacklist=False, workers=[])
        request_inputs = _request_inputs(has_targeted_workers=True, targeted_worker_ids=frozenset({"worker-1"}))
        assert Worker.can_generate_with_eligibility_inputs(worker, wp, request_inputs, _worker_inputs()) == [False, "worker_id"]

    def test_request_inputs_skip_the_worker_id_cache_without_targeted_workers(self):
        wp = _allowing_wp(workers=[], _worker_ids=["worker-1"])
        request_inputs = wp.get_eligibility_inputs()
        assert request_inputs.has_targeted_workers is False
        assert request_inputs.targeted_worker_ids == frozenset()
        wp.get_worker_ids.assert_not_called()


_ELIGIBILITY_NAME_CLAUSES = [
    pytest.param(Worker, worker_model_names_clause, id="model_names"),
    pytest.param(Worker, worker_blacklist_words_clause, id="blacklist_words"),
    pytest.param(TextWorker, text_worker_softprompt_names_clause, id="softprompt_names"),
]
"""The name aggregates the availability query selects, each with the worker class it correlates to."""


class TestEligibilityNameAggregates:
    """The name aggregates of the availability query render under both supported dialects, and the SQLite form
    decodes to the names in row id order."""

    @pytest.mark.parametrize(("worker_class", "clause_factory"), _ELIGIBILITY_NAME_CLAUSES)
    def test_postgres_aggregate_is_an_ordered_array_agg(
        self,
        monkeypatch: pytest.MonkeyPatch,
        worker_class: type[Worker],
        clause_factory: Callable[[], ScalarSelect],
    ):
        monkeypatch.setattr("horde.classes.base.worker.SQLITE_MODE", False)
        statement = select(worker_class.id, clause_factory().label("names"))
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        assert re.search(r"array_agg\(\S+ ORDER BY \S+\.id\)", compiled)

    @pytest.mark.parametrize(("worker_class", "clause_factory"), _ELIGIBILITY_NAME_CLAUSES)
    def test_sqlite_aggregate_renders_as_json_group_array(
        self,
        monkeypatch: pytest.MonkeyPatch,
        worker_class: type[Worker],
        clause_factory: Callable[[], ScalarSelect],
    ):
        monkeypatch.setattr("horde.classes.base.worker.SQLITE_MODE", True)
        statement = select(worker_class.id, clause_factory().label("names"))
        compiled = str(statement.compile(dialect=sqlite.dialect()))
        assert "json_group_array(" in compiled
        assert "array_agg" not in compiled

    def test_sqlite_model_names_decode_in_row_id_order(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("horde.classes.base.worker.SQLITE_MODE", True)
        # The full workers table carries Postgres-only column types, so only the columns the statement reads exist.
        metadata = MetaData()
        Table(
            "workers",
            metadata,
            Column("id", Worker.__table__.c.id.type, primary_key=True),
            Column("worker_type", String(30)),
        )
        worker_models_table = WorkerModel.__table__.to_metadata(metadata)
        engine = create_engine("sqlite://")
        metadata.create_all(engine)
        serving_worker_id = uuid.uuid4()
        idle_worker_id = uuid.uuid4()
        # A comma and a quote would split or break a delimited aggregate.
        later_sorting_name = 'z-model, "quoted"'
        with engine.begin() as connection:
            connection.execute(
                insert(metadata.tables["workers"]),
                [
                    {"id": serving_worker_id, "worker_type": "worker"},
                    {"id": idle_worker_id, "worker_type": "worker"},
                ],
            )
            connection.execute(
                insert(worker_models_table),
                [
                    {"id": 1, "worker_id": serving_worker_id, "model": later_sorting_name},
                    {"id": 2, "worker_id": serving_worker_id, "model": "a-model"},
                ],
            )
            rows = connection.execute(select(Worker.id, worker_model_names_clause().label("model_names"))).all()
        names_by_worker = {worker_id: tuple(model_names) for worker_id, model_names in rows}
        assert names_by_worker == {
            serving_worker_id: (later_sorting_name, "a-model"),
            idle_worker_id: (),
        }
