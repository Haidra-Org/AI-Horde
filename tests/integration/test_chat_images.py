# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Chat request images end to end: storage at submit, eligibility, transports at pop, tax and cleanup."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from PIL import Image

from tests.integration._fake_chat_worker import FakeChatWorker

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]

CHAT_MODEL = "elinas/chronos-70b-v2"
VISION_FEATURES = ["seed", "presence_penalty", "vision"]
"""A worker declaration with the vision feature."""


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Lift the per-address submit limit, which this module's request volume would otherwise trip."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture(autouse=True)
def _empty_text_queue(app) -> Iterator[None]:
    """Leave no queued text request behind, so each test's workers only meet the requests it submits."""
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    yield
    with app.app_context():
        db.session.query(TextWaitingPrompt).update({TextWaitingPrompt.n: 0}, synchronize_session=False)
        db.session.commit()


def _worker(client, headers: dict[str, str], **pop_options: Any) -> FakeChatWorker:
    return FakeChatWorker(
        client=client,
        headers=headers,
        name=f"CICD Fake Vision Scribe {uuid.uuid4().hex[:8]}",
        models=[CHAT_MODEL],
        pop_options=pop_options,
    )


def _png_data_url(size: tuple[int, int] = (64, 48), colour: tuple[int, int, int] = (200, 30, 30)) -> str:
    image_buffer = BytesIO()
    Image.new("RGB", size, colour).save(image_buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(image_buffer.getvalue()).decode('ascii')}"


def _image_body(*image_urls: str, **overrides: Any) -> dict[str, Any]:
    chat_body: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "Describe images briefly."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in these?"},
                    *({"type": "image_url", "image_url": {"url": image_url}} for image_url in image_urls),
                ],
            },
        ],
        "max_tokens": 64,
    }
    chat_body.update(overrides)
    return chat_body


def _submit(client, headers: dict[str, str], chat_body: dict[str, Any]) -> str:
    response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    return response.get_json()["id"]


def _stored_request(app, request_id: str) -> SimpleNamespace:
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    with app.app_context():
        stored = db.session.get(TextWaitingPrompt, uuid.UUID(request_id))
        return SimpleNamespace(messages=stored.messages, prompt=stored.prompt)


def _source_image_store() -> tuple[Any, str]:
    from horde import r2

    return r2.s3_client, r2.r2_source_image_bucket


def _stored_object(key: str) -> bytes | None:
    from botocore.exceptions import ClientError

    store, bucket = _source_image_store()
    try:
        return store.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as err:
        if err.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return None
        raise


def _put_object(key: str, body: bytes) -> None:
    store, bucket = _source_image_store()
    store.put_object(Bucket=bucket, Key=key, Body=body)


def _image_keys(messages: list[dict[str, Any]]) -> list[str]:
    from horde.classes.kobold.chat_request import list_chat_image_keys

    return list_chat_image_keys(messages)


def _decoded_image(image_bytes: bytes) -> Image.Image:
    decoded = Image.open(BytesIO(image_bytes))
    decoded.load()
    return decoded


def test_a_valid_image_is_stored_as_normalised_jpeg_and_replaced_by_a_reference(app, client, request_headers: dict[str, str]) -> None:
    from horde.classes.kobold.chat_request import CHAT_IMAGE_PLACEHOLDER, CHAT_IMAGE_REFERENCE_PREFIX, build_chat_image_key

    request_id = _submit(client, request_headers, _image_body(_png_data_url((3000, 1500))))

    stored = _stored_request(app, request_id)
    image_part = stored.messages[1]["content"][1]
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": f"{CHAT_IMAGE_REFERENCE_PREFIX}{build_chat_image_key(request_id, 0)}"},
    }
    assert "base64" not in str(stored.messages)
    assert CHAT_IMAGE_PLACEHOLDER in stored.prompt
    assert build_chat_image_key(request_id, 0) == f"{request_id}_chat_0.jpg"
    stored_bytes = _stored_object(build_chat_image_key(request_id, 0))
    assert stored_bytes.startswith(b"\xff\xd8")
    stored_image = _decoded_image(stored_bytes)
    assert stored_image.format == "JPEG"
    assert stored_image.mode == "RGB"
    assert stored_image.size == (2048, 1024)


def test_a_worker_without_vision_or_a_transport_never_pops_an_image_request(client, request_headers: dict[str, str]) -> None:
    _submit(client, request_headers, _image_body(_png_data_url()))

    assert _worker(client, request_headers, image_transports=["url"]).pop()["id"] is None
    assert _worker(client, request_headers, chat_features=VISION_FEATURES).pop()["id"] is None
    assert _worker(client, request_headers, chat_features=VISION_FEATURES, image_transports=[]).pop()["id"] is None
    assert _worker(client, request_headers, chat_features=VISION_FEATURES, image_transports=["carrier-pigeon"]).pop()["id"] is None

    pop_result = _worker(client, request_headers, chat_features=VISION_FEATURES, image_transports=["url"]).pop()
    assert pop_result["id"] is not None
    assert pop_result["payload"]["messages"][1]["content"][1]["type"] == "image_url"


def test_a_worker_without_vision_still_pops_chat_requests_without_images(client, request_headers: dict[str, str]) -> None:
    _submit(client, request_headers, _image_body())

    assert _worker(client, request_headers).pop()["id"] is not None


def test_a_url_worker_receives_a_signed_url_valid_for_at_least_the_job_ttl(app, client, request_headers: dict[str, str]) -> None:
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.classes.kobold.waiting_prompt import MIN_CHAT_IMAGE_URL_SECONDS
    from horde.flask import db

    _submit(client, request_headers, _image_body(_png_data_url()))

    pop_result = _worker(client, request_headers, chat_features=VISION_FEATURES, image_transports=["base64", "url"]).pop()

    signed_url = pop_result["payload"]["messages"][1]["content"][1]["image_url"]["url"]
    query = parse_qs(urlparse(signed_url).query)
    if "X-Amz-Expires" in query:
        url_lifetime = int(query["X-Amz-Expires"][0])
    else:
        url_lifetime = int(query["Expires"][0]) - int(datetime.utcnow().timestamp())
    assert url_lifetime >= pop_result["ttl"]
    assert url_lifetime >= MIN_CHAT_IMAGE_URL_SECONDS - 5
    download = requests.get(signed_url, timeout=10)
    assert download.status_code == 200
    assert _decoded_image(download.content).format == "JPEG"
    with app.app_context():
        assert db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"])).image_transport == "url"


def test_a_base64_worker_receives_a_jpeg_data_url_built_after_the_queue_lock_is_released(
    app,
    client,
    request_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy

    from horde.classes.kobold import waiting_prompt as text_waiting_prompt
    from horde.classes.kobold.processing_generation import TextProcessingGeneration
    from horde.flask import db

    request_id = _submit(client, request_headers, _image_body(_png_data_url()))
    original_download = text_waiting_prompt.download_chat_image
    lock_observations: list[bool] = []

    def download_after_checking_the_row_lock(image_key: str) -> bytes:
        # A second connection takes the request row's lock without waiting; it fails if the pop still holds it.
        with db.engine.connect() as observer:
            try:
                observer.execute(
                    sqlalchemy.text("SELECT id FROM waiting_prompts WHERE id = :request_id FOR UPDATE NOWAIT"),
                    {"request_id": uuid.UUID(request_id)},
                )
                lock_observations.append(True)
            except sqlalchemy.exc.OperationalError:
                lock_observations.append(False)
            observer.rollback()
        return original_download(image_key)

    monkeypatch.setattr(text_waiting_prompt, "download_chat_image", download_after_checking_the_row_lock)

    pop_result = _worker(client, request_headers, chat_features=VISION_FEATURES, image_transports=["base64"]).pop()

    data_url = pop_result["payload"]["messages"][1]["content"][1]["image_url"]["url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    assert _decoded_image(base64.b64decode(data_url.removeprefix("data:image/jpeg;base64,"))).format == "JPEG"
    assert lock_observations == [True]
    with app.app_context():
        assert db.session.get(TextProcessingGeneration, uuid.UUID(pop_result["id"])).image_transport == "base64"


def test_each_image_is_charged_the_extra_source_image_tax(app, client, make_api_user, settle_kudos: Callable[[], int]) -> None:
    from horde.classes.base.waiting_prompt import EXTRA_SOURCE_IMAGE_HORDE_TAX
    from horde.database import functions as database

    requester = make_api_user(trusted=True, kudos=1000)
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:(test)ci"}

    def kudos_spent_on(chat_body: dict[str, Any]) -> float:
        settle_kudos()
        with app.app_context():
            before = database.find_user_by_id(requester.id).kudos
        _submit(client, headers, chat_body)
        settle_kudos()
        with app.app_context():
            return before - database.find_user_by_id(requester.id).kudos

    without_images = kudos_spent_on(_image_body())
    with_two_images = kudos_spent_on(_image_body(_png_data_url(), _png_data_url(colour=(0, 0, 200))))

    assert with_two_images - without_images == 2 * EXTRA_SOURCE_IMAGE_HORDE_TAX


def test_the_dry_run_quote_includes_the_image_tax_and_stores_nothing(app, client, request_headers: dict[str, str]) -> None:
    from horde.classes.base.waiting_prompt import EXTRA_SOURCE_IMAGE_HORDE_TAX

    def quote(chat_body: dict[str, Any]) -> float:
        response = client.post("/api/v2/generate/chat/async", json=chat_body, headers=request_headers)
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["kudos"]

    plain_quote = quote(_image_body(horde={"dry_run": True}))
    image_quote = quote(_image_body(_png_data_url(), horde={"dry_run": True}))

    assert image_quote - plain_quote == EXTRA_SOURCE_IMAGE_HORDE_TAX


def test_more_than_one_image_from_an_untrusted_user_is_refused(client, make_api_user) -> None:
    requester = make_api_user(kudos=1000)
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:(test)ci"}

    response = client.post("/api/v2/generate/chat/async", json=_image_body(_png_data_url(), _png_data_url()), headers=headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "MoreThanMinExtraSourceImage"


@pytest.mark.parametrize("image_url", ["http://10.0.0.5/a.png", "http://127.0.0.1:9/a.png", "http://[::1]/a.png"])
def test_an_image_url_at_a_private_address_is_refused(client, request_headers: dict[str, str], image_url: str) -> None:
    response = client.post("/api/v2/generate/chat/async", json=_image_body(image_url), headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "SourceImageUrlRefused"


def test_an_image_url_whose_name_resolves_to_a_private_address_is_refused(
    client,
    request_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    from horde import outbound

    def private_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.7.7", port))]

    monkeypatch.setattr(outbound, "socket", SimpleNamespace(getaddrinfo=private_getaddrinfo, SOCK_STREAM=socket.SOCK_STREAM))

    response = client.post("/api/v2/generate/chat/async", json=_image_body("https://images.example/a.png"), headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "SourceImageUrlRefused"


def test_an_image_over_the_resolution_cap_is_refused_and_nothing_is_queued(app, client, request_headers: dict[str, str]) -> None:
    response = client.post("/api/v2/generate/chat/async", json=_image_body(_png_data_url((3100, 3100))), headers=request_headers)

    assert response.status_code == 400
    assert response.get_json()["rc"] == "SourceImageResolutionExceeded"


def test_images_are_deleted_with_the_expired_request_row(app, client, request_headers: dict[str, str]) -> None:
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.database import threads
    from horde.flask import db

    request_id = _submit(client, request_headers, _image_body(_png_data_url(), _png_data_url(colour=(1, 2, 3))))
    image_keys = _image_keys(_stored_request(app, request_id).messages)
    assert len(image_keys) == 2
    assert all(_stored_object(image_key) is not None for image_key in image_keys)
    with app.app_context():
        db.session.get(TextWaitingPrompt, uuid.UUID(request_id)).expiry = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()

    threads.check_waiting_prompts()

    assert all(_stored_object(image_key) is None for image_key in image_keys)


def test_a_failed_activation_deletes_the_images_it_stored(
    app,
    client,
    request_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt

    stored_keys: list[str] = []

    def failing_activation(waiting_prompt: TextWaitingPrompt, *args: Any, **kwargs: Any) -> None:
        stored_keys.extend(_image_keys(waiting_prompt.messages))
        raise RuntimeError("activation failed")

    monkeypatch.setattr(TextWaitingPrompt, "activate", failing_activation)

    with pytest.raises(RuntimeError, match="activation failed"):
        client.post("/api/v2/generate/chat/async", json=_image_body(_png_data_url()), headers=request_headers)

    assert len(stored_keys) == 1
    assert _stored_object(stored_keys[0]) is None


def test_a_storage_failure_refuses_the_request_and_leaves_no_row(
    app,
    client,
    make_api_user,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from botocore.exceptions import EndpointConnectionError

    from horde.apis.v2 import kobold
    from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
    from horde.flask import db

    requester = make_api_user(trusted=True, kudos=1000)
    headers = {"apikey": requester.api_key, "Client-Agent": "aihorde_ci_client:1:(test)ci"}

    def unreachable_store(jpeg_bytes: bytes, key: str) -> None:
        raise EndpointConnectionError(endpoint_url="http://store.invalid")

    monkeypatch.setattr(kobold, "upload_chat_image", unreachable_store)

    response = client.post("/api/v2/generate/chat/async", json=_image_body(_png_data_url()), headers=headers)

    assert response.status_code == 503
    assert response.get_json()["rc"] == "ChatImageStorageUnavailable"
    with app.app_context():
        assert db.session.query(TextWaitingPrompt).filter(TextWaitingPrompt.user_id == requester.id).count() == 0


@pytest.fixture
def retry_set(app) -> Iterator[Any]:
    """The chat image delete retry set, emptied before and after the test."""
    from horde.classes.kobold.waiting_prompt import CHAT_IMAGE_DELETE_RETRY_KEY
    from horde.horde_redis import horde_redis

    horde_redis.horde_r.delete(CHAT_IMAGE_DELETE_RETRY_KEY)

    def members() -> set[str]:
        stored_keys = horde_redis.horde_r.smembers(CHAT_IMAGE_DELETE_RETRY_KEY)
        return {key.decode() if isinstance(key, bytes) else key for key in stored_keys}

    yield SimpleNamespace(members=members)
    horde_redis.horde_r.delete(CHAT_IMAGE_DELETE_RETRY_KEY)


def test_a_failed_delete_is_queued_and_retried_on_the_next_tick(app, retry_set: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import EndpointConnectionError

    from horde.classes.kobold import waiting_prompt as text_waiting_prompt

    image_key = f"{uuid.uuid4()}_chat_0.jpg"
    _put_object(image_key, b"left behind")
    original_delete = text_waiting_prompt.delete_chat_images

    def unreachable_store(keys: Any) -> set[str]:
        raise EndpointConnectionError(endpoint_url="http://store.invalid")

    monkeypatch.setattr(text_waiting_prompt, "delete_chat_images", unreachable_store)
    text_waiting_prompt.delete_stored_chat_images([image_key])

    assert retry_set.members() == {image_key}
    assert text_waiting_prompt.retry_chat_image_deletes() == []
    assert retry_set.members() == {image_key}

    monkeypatch.setattr(text_waiting_prompt, "delete_chat_images", original_delete)
    assert text_waiting_prompt.retry_chat_image_deletes() == [image_key]

    assert retry_set.members() == set()
    assert _stored_object(image_key) is None


def test_a_key_the_store_reports_as_not_deleted_stays_queued(app, retry_set: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from horde.classes.kobold import waiting_prompt as text_waiting_prompt

    deleted_key, refused_key = f"{uuid.uuid4()}_chat_0.jpg", f"{uuid.uuid4()}_chat_1.jpg"
    monkeypatch.setattr(text_waiting_prompt, "delete_chat_images", lambda keys: {refused_key})

    text_waiting_prompt.delete_stored_chat_images([deleted_key, refused_key])

    assert retry_set.members() == {refused_key}


def test_the_cleanup_tick_retries_queued_deletes(app, retry_set: Any) -> None:
    from horde.classes.kobold.waiting_prompt import queue_chat_image_delete_retry
    from horde.database import threads

    image_key = f"{uuid.uuid4()}_chat_0.jpg"
    _put_object(image_key, b"queued")
    queue_chat_image_delete_retry([image_key])

    threads.check_waiting_prompts()

    assert retry_set.members() == set()
    assert _stored_object(image_key) is None


def test_the_retry_set_does_not_grow_past_its_bound(app, retry_set: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from horde.classes.kobold import waiting_prompt as text_waiting_prompt

    monkeypatch.setattr(text_waiting_prompt, "MAX_CHAT_IMAGE_DELETE_RETRY_KEYS", 2)

    text_waiting_prompt.queue_chat_image_delete_retry(["a_chat_0.jpg", "b_chat_0.jpg", "c_chat_0.jpg"])

    assert len(retry_set.members()) == 2
