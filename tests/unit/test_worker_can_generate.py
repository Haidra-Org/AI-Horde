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

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from horde.classes.base.worker import Worker

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
    worker = MagicMock(spec_set=list(defaults.keys()) + ["is_stale"])
    for k, v in defaults.items():
        setattr(worker, k, v)
    worker.is_stale.return_value = overrides.get("_is_stale", False)
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
    wp = MagicMock(spec_set=list(defaults.keys()) + ["tricked_worker", "get_worker_ids"])
    for k, v in defaults.items():
        setattr(wp, k, v)
    wp.tricked_worker.return_value = overrides.get("_tricked", False)
    wp.get_worker_ids.return_value = overrides.get("_worker_ids", [])
    return wp

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
        worker = _allowing_worker()
        wp = _allowing_wp(_tricked=True)
        assert Worker.can_generate(worker, wp) == [False, "secret"]


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
