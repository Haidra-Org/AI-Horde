# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for retrieving a stored request in its submitted shape.

A waiting prompt scatters the submitted request across its columns and relations
and normalizes the parameter dict on the way in. ``get_submitted_request``
reassembles normalized parameters and original prompt provenance, and ``assert_submitting_key`` decides which API key may see it.
"""

from __future__ import annotations

import json
import queue
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from flask_restx import marshal
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from sqlalchemy import inspect, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import joinedload

from horde import exceptions as e
from horde.apis.models.v2 import SUBMISSION_ONLY_FIELDS
from horde.apis.v2 import kobold, stable
from horde.apis.v2.base import assert_submitting_key
from horde.classes.base.user import User, UserSharedKey
from horde.classes.base.waiting_prompt import SubmittedPromptPurpose, WaitingPrompt
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.flask import db
from horde.utils import hash_api_key
from tests.unit.model_reference_seed import seed_image_reference

pytestmark = pytest.mark.unit

_IMAGE_MODEL = "stable_diffusion"
_TEXT_MODEL = "elinas/chronos-70b-v2"
PRIVATE_MARKER = "PRIVATE_SUBMISSION_MUST_NOT_ESCAPE_8b4517"


@pytest.fixture(autouse=True)
def _stub_model_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    seed_image_reference(monkeypatch, {_IMAGE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1})


def _make_image_wp(user: Any, **overrides: Any) -> ImageWaitingPrompt:
    kwargs: dict[str, Any] = {
        "prompt": "a unit-test prompt",
        "submitted_prompt": "a unit-test prompt",
        "user_id": user.id,
        "params": {
            "n": 2,
            "width": 640,
            "height": 512,
            "steps": 10,
            "sampler_name": "k_euler_a",
            "seed": "1234",
            "seed_variation": 3,
        },
        "nsfw": True,
        "censor_nsfw": False,
        "trusted_workers": True,
        "slow_workers": False,
        "webhook": "https://example.invalid/hook",
    }
    kwargs.update(overrides)
    wp = ImageWaitingPrompt([], [_IMAGE_MODEL], **kwargs)
    db.session.commit()
    return wp


def _make_text_wp(user: Any, **overrides: Any) -> TextWaitingPrompt:
    kwargs: dict[str, Any] = {
        "prompt": "a unit-test text prompt",
        "submitted_prompt": "a unit-test text prompt",
        "user_id": user.id,
        "params": {"n": 1, "max_length": 120, "max_context_length": 1024},
    }
    kwargs.update(overrides)
    wp = TextWaitingPrompt([], [_TEXT_MODEL], **kwargs)
    # Set on the row rather than through the constructor: the reconstruction reads the column, and the
    # constructor's softprompt kwarg does not survive parameter extraction.
    wp.softprompt = "unit_softprompt"
    db.session.commit()
    return wp


class TestImageSubmittedRequest:
    def test_restores_the_fields_the_row_keeps_outside_params(self, fake_redis, make_user) -> None:
        wp = _make_image_wp(make_user())

        submitted = wp.get_submitted_request()

        assert submitted["prompt"] == "a unit-test prompt"
        assert submitted["models"] == [_IMAGE_MODEL]
        assert submitted["params"]["n"] == 2
        assert submitted["params"]["seed"] == "1234"
        assert submitted["params"]["seed_variation"] == 3
        assert submitted["params"]["width"] == 640
        assert submitted["nsfw"] is True
        assert submitted["censor_nsfw"] is False
        assert submitted["trusted_workers"] is True
        assert submitted["slow_workers"] is False
        assert submitted["webhook"] == "https://example.invalid/hook"

    def test_reports_the_normalized_parameters(self, fake_redis, make_user) -> None:
        """Omitted parameters come back with the defaults the horde filled in, so the reply is what workers see."""
        wp = _make_image_wp(make_user(), params={"n": 1, "width": 512, "height": 512})

        params = wp.get_submitted_request()["params"]

        assert params["steps"] == 30
        assert params["sampler_name"] == "k_euler_a"
        assert "seed" not in params

    def test_does_not_leak_the_dispatch_payload(self, fake_redis, make_user) -> None:
        wp = _make_image_wp(make_user())

        params = wp.get_submitted_request()["params"]

        assert "ddim_steps" not in params
        assert "batch_size" not in params
        assert "prompt" not in params

    def test_marshals_in_the_input_shape_without_submission_only_fields(self, fake_redis, make_user) -> None:
        wp = _make_image_wp(make_user())

        marshalled = marshal(
            wp.get_submitted_request(),
            stable.models.response_model_submitted_request,
            skip_none=True,
        )

        assert SUBMISSION_ONLY_FIELDS.isdisjoint(marshalled)
        assert "source_image" not in marshalled
        assert marshalled["params"]["n"] == 2
        assert marshalled["params"]["seed"] == "1234"

    def test_reports_source_images_as_their_storage_references(self, fake_redis, make_user) -> None:
        """Source image and mask come back as the storage references the row holds, never re-encoded as base64."""
        wp = _make_image_wp(make_user(), source_processing="inpainting")
        wp.source_image = "https://storage.example.invalid/source_image.webp"
        wp.source_mask = "https://storage.example.invalid/source_mask.webp"
        db.session.commit()

        submitted = wp.get_submitted_request()

        assert submitted["source_image"] == "https://storage.example.invalid/source_image.webp"
        assert submitted["source_mask"] == "https://storage.example.invalid/source_mask.webp"
        assert submitted["source_processing"] == "inpainting"

    def test_restores_worker_targeting_proxy_and_extra_source_images(self, fake_redis, make_user) -> None:
        """The worker list, blacklist flag, proxied account, and extra source images are reported as submitted."""
        user = make_user()
        worker = ImageWorker(user_id=user.id, name=f"submitted-request-worker-{uuid.uuid4().hex}")
        db.session.add(worker)
        db.session.commit()
        extra_source_images = [{"image": "https://storage.example.invalid/esi_0.webp", "strength": 0.5}]
        wp = ImageWaitingPrompt(
            [str(worker.id)],
            [_IMAGE_MODEL],
            prompt="a unit-test prompt",
            user_id=user.id,
            params={"n": 1, "width": 512, "height": 512},
            worker_blacklist=True,
            proxied_account="proxied-user#1",
            extra_source_images={"esi": extra_source_images},
        )
        db.session.commit()

        submitted = wp.get_submitted_request()

        assert submitted["workers"] == [str(worker.id)]
        assert submitted["worker_blacklist"] is True
        assert submitted["proxied_account"] == "proxied-user#1"
        assert submitted["extra_source_images"] == extra_source_images


class TestTextSubmittedRequest:
    def test_includes_the_softprompt(self, fake_redis, make_user) -> None:
        wp = _make_text_wp(make_user())

        submitted = wp.get_submitted_request()

        assert submitted["softprompt"] == "unit_softprompt"
        assert submitted["models"] == [_TEXT_MODEL]
        assert submitted["params"]["n"] == 1
        assert submitted["params"]["max_length"] == 120

    def test_marshals_in_the_input_shape(self, fake_redis, make_user) -> None:
        wp = _make_text_wp(make_user())

        marshalled = marshal(
            wp.get_submitted_request(),
            kobold.models.response_model_submitted_request,
            skip_none=True,
        )

        assert SUBMISSION_ONLY_FIELDS.isdisjoint(marshalled)
        assert marshalled["softprompt"] == "unit_softprompt"


class TestSubmittingKeyCheck:
    """The request ID stays a bearer token for progress; the parameters need the submitting key."""

    @staticmethod
    def _user_with_key(make_user, **overrides: Any):
        raw_key = uuid.uuid4().hex
        user = make_user(api_key=hash_api_key(raw_key), **overrides)
        return user, raw_key

    def test_the_submitting_user_key_is_accepted(self, fake_redis, make_user) -> None:
        user, raw_key = self._user_with_key(make_user)
        wp = _make_image_wp(user)

        assert_submitting_key(raw_key, wp)

    def test_another_users_key_is_refused(self, fake_redis, make_user) -> None:
        owner, _ = self._user_with_key(make_user)
        _, other_key = self._user_with_key(make_user)
        wp = _make_image_wp(owner)

        with pytest.raises(e.NotRequestOwner):
            assert_submitting_key(other_key, wp)

    def test_an_unknown_key_is_unauthorized(self, fake_redis, make_user) -> None:
        wp = _make_image_wp(make_user())

        with pytest.raises(e.InvalidAPIKey):
            assert_submitting_key("not-a-real-key", wp)

    def test_the_anonymous_key_is_refused_even_for_anonymous_requests(self, fake_redis, make_user) -> None:
        anon, _ = self._user_with_key(make_user, oauth_id="anon")
        anon.api_key = hash_api_key("0000000000")
        db.session.flush()
        wp = _make_image_wp(anon)

        with pytest.raises(e.AnonForbidden):
            assert_submitting_key("0000000000", wp)

    def test_the_submitting_shared_key_and_its_owner_are_accepted(self, fake_redis, make_user) -> None:
        owner, owner_key = self._user_with_key(make_user)
        sharedkey = UserSharedKey(user_id=owner.id)
        db.session.add(sharedkey)
        db.session.flush()
        wp = _make_image_wp(owner, sharedkey_id=sharedkey.id)

        assert_submitting_key(str(sharedkey.id), wp)
        assert_submitting_key(owner_key, wp)

    def test_a_different_shared_key_of_the_same_owner_is_refused(self, fake_redis, make_user) -> None:
        owner, _ = self._user_with_key(make_user)
        submitting = UserSharedKey(user_id=owner.id)
        other = UserSharedKey(user_id=owner.id)
        db.session.add_all([submitting, other])
        db.session.flush()
        wp = _make_image_wp(owner, sharedkey_id=submitting.id)

        with pytest.raises(e.NotRequestOwner):
            assert_submitting_key(str(other.id), wp)

    def test_a_shared_key_is_refused_for_a_request_submitted_with_the_personal_key(self, fake_redis, make_user) -> None:
        """A shared key reads only its own requests, even when its owner submitted the request directly."""
        owner, _ = self._user_with_key(make_user)
        sharedkey = UserSharedKey(user_id=owner.id)
        db.session.add(sharedkey)
        db.session.flush()
        wp = _make_image_wp(owner)
        assert wp.sharedkey_id is None

        with pytest.raises(e.NotRequestOwner):
            assert_submitting_key(str(sharedkey.id), wp)


@pytest.mark.parametrize("make_prompt", [_make_image_wp, _make_text_wp])
def test_privileged_prompt_requires_a_named_purpose_and_stays_out_of_joins(fake_redis, make_user, make_prompt) -> None:
    owner = make_user()
    waiting = make_prompt(owner, prompt="effective worker input", submitted_prompt=PRIVATE_MARKER)
    request_id = waiting.id
    db.session.expunge_all()
    # Both polymorphic and concrete ORM queries, including ordinary/eager joins,
    # must omit the column. A bare Core table SELECT is deliberately not safe.
    for statement in (
        select(WaitingPrompt),
        select(type(waiting)),
        select(type(waiting), User).join(User, WaitingPrompt.user_id == User.id),
        select(User).options(joinedload(User.waiting_prompts)),
    ):
        assert "submitted_prompt" not in str(statement.compile())
    loaded_owner = (
        db.session.execute(
            select(User).where(User.id == owner.id).options(joinedload(User.waiting_prompts)),
        )
        .unique()
        .scalar_one()
    )
    loaded = next(prompt for prompt in loaded_owner.waiting_prompts if prompt.id == request_id)
    assert "submitted_prompt" in inspect(loaded).unloaded
    assert "submitted_prompt" not in vars(loaded)
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = loaded.submitted_prompt
    # The purpose is keyword-only and required, so no caller reaches the text
    # without naming which reviewed path it is.
    with pytest.raises(TypeError):
        loaded.read_privileged_submitted_prompt()
    with pytest.raises(TypeError):
        loaded.read_privileged_submitted_prompt(SubmittedPromptPurpose.REQUEST_OWNER)
    assert loaded.get_submitted_request()["prompt"] == PRIVATE_MARKER
    assert loaded.read_privileged_submitted_prompt(purpose=SubmittedPromptPurpose.MODERATION_EVIDENCE) == PRIVATE_MARKER
    # The explicit scalar read does not hydrate the sensitive ORM attribute.
    assert "submitted_prompt" not in vars(loaded)
    with pytest.raises(InvalidRequestError, match="raiseload"):
        _ = loaded.submitted_prompt


@pytest.mark.parametrize("make_prompt", [_make_image_wp, _make_text_wp])
def test_original_absent_from_existing_generation_exports(fake_redis, make_user, make_prompt, monkeypatch) -> None:
    from horde.classes.base import processing_generation as webhook_module
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.worker import TextWorker
    from horde.classes.stable.processing_generation import ImageProcessingGeneration
    from horde.classes.stable.worker import ImageWorker

    owner = make_user()
    waiting = make_prompt(owner, prompt="effective worker input", submitted_prompt=PRIVATE_MARKER)
    is_image = isinstance(waiting, ImageWaitingPrompt)
    worker_class = ImageWorker if is_image else TextWorker
    worker = worker_class(name=f"privacy-worker-{uuid.uuid4().hex[:8]}", user_id=owner.id)
    db.session.add(worker)
    db.session.flush()
    generation_class = ImageProcessingGeneration if is_image else TextProcessingGeneration
    generation = generation_class(wp_id=waiting.id, worker_id=worker.id, model=_IMAGE_MODEL if is_image else _TEXT_MODEL)
    generation.generation = "synthetic result"
    db.session.commit()
    captured = queue.Queue()
    monkeypatch.setattr(webhook_module, "_webhook_queue", captured)
    monkeypatch.setattr(webhook_module, "_ensure_webhook_sender", lambda: None)
    generation.send_webhook(1)
    exports = [waiting.get_job_payload(1), generation.get_details()]
    if is_image:
        monkeypatch.setattr("horde.classes.stable.waiting_prompt.generate_procgen_upload_url", lambda *_: "https://example.invalid")
        exports.append(waiting.get_share_metadata())
    else:
        waiting.webhook = "https://example.invalid"
        generation.send_webhook(1)
    fake_job = SimpleNamespace(id=generation.id, model=generation.model, job_ttl=150, worker=worker)
    exports.append(waiting.get_pop_payload([fake_job], dict(waiting.get_job_payload(1))))
    exports.append(captured.get_nowait()[1])
    for lite in (False, True):
        exports.append(
            waiting.get_status(
                request_avg=1,
                active_worker_count=(1, 1),
                has_valid_workers=True,
                wp_queue_stats=(0, 0, 0),
                lite=lite,
            )
        )
    for privilege in (0, 1, 2):
        exports.append(owner.get_details(details_privilege=privilege))
    for exported in exports:
        serialized = json.dumps(exported, default=str)
        assert PRIVATE_MARKER not in serialized
        assert "submitted_prompt" not in serialized
