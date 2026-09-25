# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared-key funding must not turn account lookup into request-token discovery."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE

from horde.classes.base.user import SharedKeyUsageSummary, User, UserSharedKey
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.enums import UserRoleTypes
from horde.flask import cache, db
from horde.limiter import limiter
from horde.utils import hash_api_key
from tests.fixture_types import MakeUser, MakeUserRole
from tests.unit.model_reference_seed import seed_image_reference

if TYPE_CHECKING:
    from flask.testing import FlaskClient

    from horde.horde_redis import HordeRedis
    from tests.unit.conftest import _QueryRecorder

pytestmark: pytest.MarkDecorator = pytest.mark.unit


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch, fake_redis: HordeRedis) -> None:
    """Set up hermetic model references and disable endpoint rate limits."""
    seed_image_reference(monkeypatch, {"stable_diffusion": KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})
    monkeypatch.setattr(limiter, "enabled", False)


def make_wp(
    owner: User,
    sharedkey: UserSharedKey | None = None,
    kind: str = "image",
    **overrides: Any,
) -> ImageWaitingPrompt | TextWaitingPrompt:
    """Create a retained request with four queued generations by default."""
    cls = ImageWaitingPrompt if kind == "image" else TextWaitingPrompt
    kwargs = {
        "prompt": "private submitted prompt",
        "user_id": owner.id,
        "sharedkey_id": sharedkey.id if sharedkey else None,
        "params": {"n": 4, "width": 512, "height": 512, "max_length": 80},
        "active": True,
        "created": datetime.utcnow() - timedelta(seconds=120),
        "expiry": datetime.utcnow() + timedelta(minutes=10),
    }
    kwargs.update(overrides)
    wp = cls([], ["stable_diffusion"] if kind == "image" else ["test-text-model"], **kwargs)
    db.session.commit()
    return wp


@pytest.fixture
def owner_and_key(make_user: MakeUser) -> tuple[User, str, UserSharedKey]:
    """Create an account, its personal credential, and a shared key."""
    raw_key = uuid.uuid4().hex
    owner = make_user(api_key=hash_api_key(raw_key))
    sharedkey = UserSharedKey(user_id=owner.id)
    db.session.add(sharedkey)
    db.session.commit()
    return owner, raw_key, sharedkey


@pytest.mark.parametrize("privilege", [1, 2])
def test_account_details_exclude_all_shared_key_requests(owner_and_key: tuple[User, str, UserSharedKey], privilege: int) -> None:
    """Only requests submitted with the personal key appear in ``active_generations``.

    A request funded through a style's shared key records that key's ``sharedkey_id`` just as a direct shared-key
    submission does, whoever the original caller was, so the shared-key rows here cover style funding as well.
    """
    owner, _, sharedkey = owner_and_key
    direct = [make_wp(owner, kind=kind) for kind in ("image", "text")]
    for kind in ("image", "text"):
        make_wp(owner, sharedkey, kind)
    assert owner.get_details(privilege)["active_generations"] == {
        kind: [str(wp.id)] for kind, wp in zip(("image", "text"), direct, strict=True)
    }


def test_owner_and_moderator_endpoints_ignore_old_cached_request_ids(
    owner_and_key: tuple[User, str, UserSharedKey],
    client: FlaskClient,
    fake_redis: HordeRedis,
    make_user: MakeUser,
    make_user_role: MakeUserRole,
) -> None:
    owner, raw_key, sharedkey = owner_and_key
    direct = make_wp(owner)
    private = make_wp(owner, sharedkey)
    moderator_key = uuid.uuid4().hex
    moderator = make_user(api_key=hash_api_key(moderator_key))
    make_user_role(moderator, UserRoleTypes.MODERATOR)
    db.session.commit()
    leaked = {"id": owner.id, "active_generations": {"image": [str(private.id)]}}
    for privilege in (1, 2):
        fake_redis.horde_r_setex_json(f"cached_user_id_{owner.id}_privilege_{privilege}", timedelta(seconds=30), leaked)
    fake_redis.horde_r_setex_json(f"cached_apikey_user_{hash_api_key(raw_key)}", timedelta(seconds=30), leaked)
    for path, credential in (
        ("find_user", raw_key),
        (f"users/{owner.id}", raw_key),
        (f"users/{owner.id}", moderator_key),
    ):
        response = client.get(f"/api/v2/{path}", headers={"apikey": credential})
        assert response.status_code == 200
        assert response.json["active_generations"] == {"image": [str(direct.id)]}
        assert str(private.id) not in response.get_data(as_text=True)
    holder = client.get("/api/v2/find_user", headers={"apikey": str(sharedkey.id)})
    assert holder.status_code == 200
    assert str(private.id) not in holder.get_data(as_text=True)
    assert str(direct.id) not in holder.get_data(as_text=True)


@pytest.mark.parametrize("public_first", [True, False])
def test_only_owner_gets_usage_without_cross_credential_cache_leaks(
    owner_and_key: tuple[User, str, UserSharedKey],
    client: FlaskClient,
    make_user: MakeUser,
    public_first: bool,
) -> None:
    owner, raw_key, sharedkey = owner_and_key
    wp = make_wp(owner, sharedkey)
    outsider_key = uuid.uuid4().hex
    make_user(api_key=hash_api_key(outsider_key))
    db.session.commit()
    path = f"/api/v2/sharedkeys/{sharedkey.id}"
    cache.clear()
    if public_first:
        public = client.get(path)
        assert "active_usage" not in public.json
        assert public.headers["Cache-Control"] == "private, no-store"
    response = client.get(path, headers={"apikey": raw_key})
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    summary = response.json["active_usage"]
    assert summary["image"]["requests"] == 1
    assert summary["image"]["queued"] == 4
    assert summary["text"]["requests"] == 0
    assert str(wp.id) not in response.get_data(as_text=True)
    assert wp.prompt not in response.get_data(as_text=True)
    owner_metadata = {field_name: field_value for field_name, field_value in response.json.items() if field_name != "active_usage"}
    for headers in ({}, {"apikey": str(sharedkey.id)}, {"apikey": outsider_key}, {"apikey": "legacy-ignored-header"}):
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "private, no-store"
        assert "active_usage" not in response.json
        assert response.json == owner_metadata
    # Owner responses are fresh even when a public response was cached first.
    wp.n = 2
    db.session.commit()
    assert client.get(path, headers={"apikey": raw_key}).json["active_usage"]["image"]["queued"] == 2


def test_usage_counts_mixed_work_without_loading_request_contents(
    owner_and_key: tuple[User, str, UserSharedKey],
    assert_query_count: Callable[[], AbstractContextManager[_QueryRecorder]],
) -> None:
    owner, _, sharedkey = owner_and_key
    wp = make_wp(owner, sharedkey)
    text_wp = make_wp(owner, sharedkey, "text")
    text_wp.n = 2
    worker = ImageWorker(user_id=owner.id, name=f"privacy-worker-{uuid.uuid4().hex}")
    db.session.add(worker)
    db.session.commit()
    for attributes in ({"generation": "private output"}, {}, {"faulted": True}, {"fake": True}):
        ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="stable_diffusion", **attributes)
    # n is deliberately stale: reconcile it against the four requested slots.
    other = UserSharedKey(user_id=owner.id)
    db.session.add(other)
    db.session.commit()
    make_wp(owner, other)
    make_wp(owner)
    # Prime the key's scalar attributes before recording the summary query.
    sharedkey.id
    request_values_before = (wp.n, wp.jobs, dict(wp.params), text_wp.n, text_wp.jobs, dict(text_wp.params))
    key_values_before = (sharedkey.kudos, sharedkey.utilized, sharedkey.expiry)
    with assert_query_count() as queries:
        usage = sharedkey.get_active_usage()
    selects = queries.of_kind("SELECT")
    assert len(queries.statements) == 1  # No writes or per-request follow-up queries.
    assert len(selects) == 1
    assert (wp.n, wp.jobs, dict(wp.params), text_wp.n, text_wp.jobs, dict(text_wp.params)) == request_values_before
    assert (sharedkey.kudos, sharedkey.utilized, sharedkey.expiry) == key_values_before
    assert not db.session.dirty
    assert "waiting_prompts.prompt" not in selects[0]
    assert "waiting_prompts.params" not in selects[0]
    assert usage.image.requests == 1
    assert usage.image.queued == 2
    assert usage.image.processing == 1
    assert usage.image.finished == 1
    assert 120 <= usage.image.oldest_queued_age < 150
    assert usage.text.requests == 1
    assert usage.text.queued == 2


@pytest.mark.parametrize("state", ["inactive", "expired", "faulted", "cancelled", "completed"])
def test_usage_excludes_non_active_work(owner_and_key: tuple[User, str, UserSharedKey], state: str) -> None:
    owner, _, sharedkey = owner_and_key
    wp = make_wp(owner, sharedkey)
    if state == "inactive":
        wp.active = False
    elif state == "expired":
        wp.expiry = datetime.utcnow() - timedelta(seconds=1)
    elif state == "faulted":
        wp.faulted = True
    elif state == "cancelled":
        wp.terminal_outcome = "cancelled"
    else:
        wp.n = 0
    db.session.commit()
    usage = sharedkey.get_active_usage()
    assert usage.image == SharedKeyUsageSummary()
    assert usage.text == SharedKeyUsageSummary()


def test_processing_only_request_has_no_queued_age_and_disappears_when_finished(owner_and_key: tuple[User, str, UserSharedKey]) -> None:
    owner, _, sharedkey = owner_and_key
    wp = make_wp(owner, sharedkey, params={"n": 1, "width": 512, "height": 512})
    worker = ImageWorker(user_id=owner.id, name=f"privacy-worker-{uuid.uuid4().hex}")
    db.session.add(worker)
    db.session.commit()
    procgen = ImageProcessingGeneration(wp_id=wp.id, worker_id=worker.id, model="stable_diffusion")
    wp.n = 0
    db.session.commit()
    assert asdict(sharedkey.get_active_usage().image) == {
        "requests": 1,
        "queued": 0,
        "processing": 1,
        "finished": 0,
        "oldest_queued_age": 0,
    }
    procgen.generation = "private output"
    db.session.commit()
    assert sharedkey.get_active_usage().image == SharedKeyUsageSummary()


def test_refresh_cache_invalidates_current_account_views(
    owner_and_key: tuple[User, str, UserSharedKey],
    client: FlaskClient,
    fake_redis: HordeRedis,
) -> None:
    owner, raw_key, _ = owner_and_key
    client.get("/api/v2/find_user", headers={"apikey": raw_key})
    client.get(f"/api/v2/users/{owner.id}", headers={"apikey": raw_key})
    names = [f"cached_apikey_user_v2_{hash_api_key(raw_key)}", f"cached_user_id_v2_{owner.id}_privilege_1"]
    assert all(fake_redis.horde_r_get(name) for name in names)
    owner.refresh_cache()
    assert all(fake_redis.horde_r_get(name) is None for name in names)
