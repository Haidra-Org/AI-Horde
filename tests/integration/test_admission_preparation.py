# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Admission writes a prepared request in one transaction before anything else can see it.

The waiting prompt constructor prepares the request without committing. The endpoint then commits the inactive row
with its model rows and its job payload, before source uploads, kudos checks and activation. Another connection, such
as a worker's pop, therefore sees either no row or a row with its models and payload, never a partly prepared one.

A request refused after that commit is deleted, as is a dry run once its quote is computed, so neither leaves a row.
A downgraded request is queued with the payload written by its downgrade. A deadlock retry of activation rolls back
to the committed row, so the request keeps its models and payload.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from tests.conftest import _QueryRecorder
from tests.fixture_types import ApiUser, MakeApiUser

pytestmark = pytest.mark.integration

QueryCounter = Callable[[], AbstractContextManager[_QueryRecorder]]

AGENT: str = "aihorde_ci_client:1.0:(test)ci"
"""The client agent sent with every request."""

IMAGE_MODEL: str = "stable_diffusion"
"""The image model requested in these tests."""

TEXT_MODEL: str = "elinas/chronos-70b-v2"
"""The text and chat model requested in these tests."""

WRITE_KINDS: frozenset[str] = frozenset({"INSERT", "UPDATE", "DELETE"})
"""The statement kinds that change rows."""

PREPARED_REQUEST_WRITES: list[tuple[str, str]] = [
    ("INSERT", "waiting_prompts"),
    ("INSERT", "wp_models"),
]
"""The writes of a request that lists one model and no workers, from admission's start to its first upload step."""

IMAGE_PARAMS: dict[str, Any] = {
    "width": 512,
    "height": 512,
    "steps": 8,
    "cfg_scale": 1.5,
    "sampler_name": "k_euler_a",
    "n": 1,
}
"""Parameters of a small image request that needs no kudos up front."""

GATED_IMAGE_STEPS: int = 50
"""A step count above the free-tier ceiling, so the request needs kudos up front."""

OVERSIZED_MAX_LENGTH: int = 8192
"""A text ``max_length`` above every free output allowance, so the request needs kudos up front."""


@dataclass(frozen=True)
class _AdmissionCase:
    """Represents one admission route and a request queued through it."""

    label: str
    """The test id."""
    path: str
    """The route that receives the request."""
    status_family: str
    """The route family of the request's status and cancel route."""
    body: dict[str, Any]
    """The request body."""
    model: str
    """The only model listed by the request."""
    needs_online_worker: bool = False
    """Whether the route refuses the request unless a worker serving its model is online."""


ADMISSION_CASES: list[_AdmissionCase] = [
    _AdmissionCase(
        label="image",
        path="/api/v2/generate/async",
        status_family="",
        body={
            "prompt": "a horde of robots preparing their own requests",
            "nsfw": True,
            "params": IMAGE_PARAMS,
            "models": [IMAGE_MODEL],
            "allow_downgrade": False,
        },
        model=IMAGE_MODEL,
    ),
    _AdmissionCase(
        label="text",
        path="/api/v2/generate/text/async",
        status_family="text/",
        body={
            "prompt": "a text request preparing itself",
            "params": {"max_length": 80, "max_context_length": 1024, "n": 1},
            "models": [TEXT_MODEL],
        },
        model=TEXT_MODEL,
    ),
    _AdmissionCase(
        label="chat",
        path="/api/v2/generate/chat/async",
        status_family="chat/",
        body={
            "model": TEXT_MODEL,
            "messages": [{"role": "user", "content": "Say hello."}],
            "max_tokens": 64,
        },
        model=TEXT_MODEL,
        needs_online_worker=True,
    ),
]
"""The image, text and chat admissions, each queueing a request that needs no kudos up front."""


@dataclass(frozen=True)
class _PreparedRowSnapshot:
    """Represents a request row as an independent connection read it when admission reached its upload step."""

    active: bool
    """Whether the row was on the queue."""
    gen_payload: dict[str, Any]
    """The job payload stored with the row."""
    total_usage: float
    """The row's total usage, an input to its price."""
    model_names: list[str]
    """The models stored for the row."""


class _AdmissionObserver:
    """Reads the database from an independent connection while one admission runs.

    One read runs after the request's parameters are extracted, the other when the endpoint reaches its upload step,
    which is the first step after the preparation commit.
    """

    def __init__(self, queries: _QueryRecorder) -> None:
        self.queries: _QueryRecorder = queries
        """The recorder active for the whole admission."""
        self.rows_while_preparing: int | None = None
        """The number of the requester's rows seen by another connection after parameter extraction, or None before that
        read."""
        self.snapshot: _PreparedRowSnapshot | None = None
        """The row as seen by another connection at the upload step, or None before that read."""
        self.writes_before_upload: list[tuple[str, str | None]] = []
        """The writes recorded from the start of admission to the upload step."""
        self.written_commits_before_upload: int | None = None
        """The committed transactions with writes from the start of admission to the upload step."""

    def read_while_preparing(self, user_id: int) -> None:
        """Count the requester's request rows from an independent connection."""
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session

        from horde.classes.base.waiting_prompt import WaitingPrompt
        from horde.flask import db

        with Session(db.engine) as other:
            self.rows_while_preparing = other.scalar(
                select(func.count()).select_from(WaitingPrompt).where(WaitingPrompt.user_id == user_id),
            )

    def read_at_upload(self, request_id: uuid.UUID) -> None:
        """Read the request row and its models from an independent connection, and the writes recorded so far."""
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from horde.classes.base.waiting_prompt import WaitingPrompt, WPModels
        from horde.flask import db

        with Session(db.engine) as other:
            row = other.execute(
                select(
                    WaitingPrompt.active,
                    WaitingPrompt.gen_payload,
                    WaitingPrompt.total_usage,
                ).where(WaitingPrompt.id == request_id),
            ).one()
            model_names = list(other.scalars(select(WPModels.model).where(WPModels.wp_id == request_id)))
        self.snapshot = _PreparedRowSnapshot(
            active=row.active,
            gen_payload=dict(row.gen_payload),
            total_usage=row.total_usage,
            model_names=model_names,
        )
        self.writes_before_upload = _writes(self.queries)
        self.written_commits_before_upload = len([transaction for transaction in self.queries.commits if transaction.wrote])


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Disable the rate limiter for the duration of a test."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _writes(queries: _QueryRecorder) -> list[tuple[str, str | None]]:
    return [(recorded.kind, recorded.table) for recorded in queries.recorded_statements if recorded.kind in WRITE_KINDS]


def _count_request_rows(app: Flask, user_id: int) -> int:
    from horde.classes.base.waiting_prompt import WaitingPrompt
    from horde.flask import db

    with app.app_context():
        return db.session.query(WaitingPrompt).filter(WaitingPrompt.user_id == user_id).count()


def _cancel(client: FlaskClient, case: _AdmissionCase, request_id: str, requester: ApiUser) -> None:
    """Cancel a queued request, so pops in later tests do not receive it."""
    response = client.delete(f"/api/v2/generate/{case.status_family}status/{request_id}", headers=_headers(requester.api_key))
    assert response.status_code == 200, response.get_data(as_text=True)


def _bring_worker_online(client: FlaskClient, request_headers: dict[str, str], case: _AdmissionCase) -> None:
    """Pop once with a chat worker serving the case's model when the route requires an online worker."""
    from tests.integration._fake_chat_worker import FakeChatWorker

    if not case.needs_online_worker:
        return
    worker_name = f"admission-preparation-{uuid.uuid4().hex[:12]}"
    FakeChatWorker(client=client, headers=request_headers, name=worker_name, models=[case.model]).pop()


def _observe_admission(monkeypatch: pytest.MonkeyPatch, case: _AdmissionCase, observer: _AdmissionObserver) -> None:
    """Hook the observer's reads into the preparation and the upload step of the case's endpoint."""
    from horde.apis.v2.kobold import ChatAsyncGenerate, TextAsyncGenerate
    from horde.apis.v2.stable import ImageAsyncGenerate
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.classes.stable.waiting_prompt import ImageWaitingPrompt

    endpoint_classes = {"image": ImageAsyncGenerate, "text": TextAsyncGenerate, "chat": ChatAsyncGenerate}
    endpoint_class = endpoint_classes[case.label]
    waiting_prompt_class = ImageWaitingPrompt if case.label == "image" else TextWaitingPrompt
    original_extract_params = waiting_prompt_class.extract_params
    original_upload = endpoint_class.upload_source_images

    def extract_params_then_read(waiting_prompt: Any, *args: Any, **kwargs: Any) -> None:
        original_extract_params(waiting_prompt, *args, **kwargs)
        observer.read_while_preparing(waiting_prompt.user_id)

    def read_then_upload(resource: Any) -> None:
        observer.read_at_upload(resource.wp.id)
        original_upload(resource)

    monkeypatch.setattr(waiting_prompt_class, "extract_params", extract_params_then_read)
    monkeypatch.setattr(endpoint_class, "upload_source_images", read_then_upload)


@pytest.mark.parametrize("case", ADMISSION_CASES, ids=[case.label for case in ADMISSION_CASES])
def test_admission_commits_the_prepared_request_with_its_models_and_payload_in_one_transaction(
    client: FlaskClient,
    request_headers: dict[str, str],
    make_api_user: MakeApiUser,
    assert_query_count: QueryCounter,
    monkeypatch: pytest.MonkeyPatch,
    case: _AdmissionCase,
) -> None:
    """Up to the upload step, admission commits once, writing the request row and its model row and nothing else."""
    _bring_worker_online(client, request_headers, case)
    requester = make_api_user()
    with assert_query_count() as queries:
        observer = _AdmissionObserver(queries)
        _observe_admission(monkeypatch, case, observer)
        response = client.post(case.path, json=case.body, headers=_headers(requester.api_key))
    assert response.status_code == 202, response.get_data(as_text=True)
    _cancel(client, case, response.get_json()["id"], requester)

    assert observer.writes_before_upload == PREPARED_REQUEST_WRITES, queries.describe()
    assert observer.written_commits_before_upload == 1, queries.describe()


@pytest.mark.parametrize("case", ADMISSION_CASES, ids=[case.label for case in ADMISSION_CASES])
def test_other_connections_see_a_request_only_once_its_models_and_payload_are_written(
    client: FlaskClient,
    request_headers: dict[str, str],
    make_api_user: MakeApiUser,
    assert_query_count: QueryCounter,
    monkeypatch: pytest.MonkeyPatch,
    case: _AdmissionCase,
) -> None:
    """A pop racing admission finds no row during preparation, then an inactive row with its models and payload."""
    _bring_worker_online(client, request_headers, case)
    requester = make_api_user()
    with assert_query_count() as queries:
        observer = _AdmissionObserver(queries)
        _observe_admission(monkeypatch, case, observer)
        response = client.post(case.path, json=case.body, headers=_headers(requester.api_key))
    assert response.status_code == 202, response.get_data(as_text=True)
    _cancel(client, case, response.get_json()["id"], requester)

    assert observer.rows_while_preparing == 0
    assert observer.snapshot is not None
    assert observer.snapshot.active is False
    assert observer.snapshot.model_names == [case.model]
    assert observer.snapshot.gen_payload
    assert observer.snapshot.total_usage > 0


@pytest.mark.parametrize("case", ADMISSION_CASES[:2], ids=[case.label for case in ADMISSION_CASES[:2]])
def test_a_dry_run_prepares_the_request_and_leaves_no_row(
    app: Flask,
    client: FlaskClient,
    make_api_user: MakeApiUser,
    assert_query_count: QueryCounter,
    monkeypatch: pytest.MonkeyPatch,
    case: _AdmissionCase,
) -> None:
    """A dry run computes its quote from a committed request row, then deletes the row and its model rows."""
    from horde.apis.v2 import base

    original_redis_get = base.hr.horde_r_get

    def redis_get_without_cached_quotes(key: str) -> Any:
        # A cached quote is returned for a dry run before any row is written, which would leave nothing to check.
        if key.startswith("payload_kudos_"):
            return None
        return original_redis_get(key)

    monkeypatch.setattr(base.hr, "horde_r_get", redis_get_without_cached_quotes)
    requester = make_api_user()

    with assert_query_count() as queries:
        response = client.post(case.path, json={**case.body, "dry_run": True}, headers=_headers(requester.api_key))

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["kudos"] > 0
    writes = _writes(queries)
    assert ("INSERT", "waiting_prompts") in writes, queries.describe()
    assert ("DELETE", "waiting_prompts") in writes, queries.describe()
    assert _count_request_rows(app, requester.id) == 0


def test_an_image_request_refused_for_upfront_kudos_leaves_no_row(
    app: Flask,
    client: FlaskClient,
    make_api_user: MakeApiUser,
) -> None:
    """A request refused after its preparation commit is deleted with its model rows."""
    requester = make_api_user()
    gated_body = {**ADMISSION_CASES[0].body, "params": {**IMAGE_PARAMS, "steps": GATED_IMAGE_STEPS}}

    response = client.post("/api/v2/generate/async", json=gated_body, headers=_headers(requester.api_key))

    assert response.status_code == 403, response.get_data(as_text=True)
    assert response.get_json()["rc"] == "KudosUpfront"
    assert _count_request_rows(app, requester.id) == 0


def test_a_downgraded_image_request_is_queued_with_its_downgraded_payload(
    app: Flask,
    client: FlaskClient,
    make_api_user: MakeApiUser,
) -> None:
    """A request downgraded to the free-tier step budget is queued with the lowered steps in its row and payload."""
    from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
    from horde.flask import db

    requester = make_api_user()
    downgradable_body = {
        **ADMISSION_CASES[0].body,
        "params": {**IMAGE_PARAMS, "steps": GATED_IMAGE_STEPS},
        "allow_downgrade": True,
    }

    response = client.post("/api/v2/generate/async", json=downgradable_body, headers=_headers(requester.api_key))

    assert response.status_code == 202, response.get_data(as_text=True)
    request_id = response.get_json()["id"]
    with app.app_context():
        waiting_prompt = db.session.get(ImageWaitingPrompt, uuid.UUID(request_id))
        assert waiting_prompt.active is True
        assert waiting_prompt.slow_workers is True
        assert waiting_prompt.params["steps"] < GATED_IMAGE_STEPS
        assert waiting_prompt.gen_payload["ddim_steps"] == waiting_prompt.params["steps"]
        assert waiting_prompt.get_model_names() == [IMAGE_MODEL]
    _cancel(client, ADMISSION_CASES[0], request_id, requester)


def test_a_downgraded_text_request_is_queued_with_its_downgraded_payload(
    app: Flask,
    client: FlaskClient,
    make_api_user: MakeApiUser,
) -> None:
    """A request downgraded to the free output allowance is queued with the lowered length in its row and payload."""
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    requester = make_api_user()
    downgradable_body = {
        **ADMISSION_CASES[1].body,
        "params": {"max_length": OVERSIZED_MAX_LENGTH, "max_context_length": 2 * OVERSIZED_MAX_LENGTH, "n": 1},
        "allow_downgrade": True,
    }

    response = client.post("/api/v2/generate/text/async", json=downgradable_body, headers=_headers(requester.api_key))

    assert response.status_code == 202, response.get_data(as_text=True)
    request_id = response.get_json()["id"]
    with app.app_context():
        waiting_prompt = db.session.get(TextWaitingPrompt, uuid.UUID(request_id))
        assert waiting_prompt.active is True
        assert waiting_prompt.slow_workers is True
        assert waiting_prompt.max_length < OVERSIZED_MAX_LENGTH
        assert waiting_prompt.gen_payload["max_length"] == waiting_prompt.max_length
        assert waiting_prompt.get_model_names() == [TEXT_MODEL]
    _cancel(client, ADMISSION_CASES[1], request_id, requester)


class _Deadlock(Exception):
    """Stands in for the DBAPI error raised by PostgreSQL for the victim of a lock cycle."""

    pgcode: str = "40P01"
    """The SQLSTATE of a deadlock."""


def test_an_activation_deadlock_retry_keeps_the_prepared_request(
    app: Flask,
    client: FlaskClient,
    make_api_user: MakeApiUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadlock retry rolls back the failed activation only, so the request is queued with its models and payload."""
    from sqlalchemy.exc import OperationalError

    from horde.classes.base import waiting_prompt as base_waiting_prompt
    from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
    from horde.flask import db

    original_activate = base_waiting_prompt.WaitingPrompt._activate
    activation_attempts: list[int] = []

    def deadlock_on_the_first_attempt(waiting_prompt: Any, *args: Any, **kwargs: Any) -> None:
        activation_attempts.append(len(activation_attempts) + 1)
        if len(activation_attempts) == 1:
            waiting_prompt.active = True
            db.session.flush()
            raise OperationalError("activate", {}, _Deadlock())
        original_activate(waiting_prompt, *args, **kwargs)

    monkeypatch.setattr(base_waiting_prompt.WaitingPrompt, "_activate", deadlock_on_the_first_attempt)
    monkeypatch.setattr(base_waiting_prompt.time, "sleep", lambda _seconds: None)
    requester = make_api_user()

    response = client.post("/api/v2/generate/async", json=ADMISSION_CASES[0].body, headers=_headers(requester.api_key))

    assert response.status_code == 202, response.get_data(as_text=True)
    assert activation_attempts == [1, 2]
    request_id = response.get_json()["id"]
    with app.app_context():
        waiting_prompt = db.session.get(ImageWaitingPrompt, uuid.UUID(request_id))
        assert waiting_prompt.active is True
        assert waiting_prompt.get_model_names() == [IMAGE_MODEL]
        assert waiting_prompt.gen_payload["ddim_steps"] == IMAGE_PARAMS["steps"]
    _cancel(client, ADMISSION_CASES[0], request_id, requester)
