# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Move captured prompt text from the event row to object storage, exactly once, and delete it with retention.

A dict-backed store stands in for the evidence bucket, except in the ``object_storage`` test, which runs the same
upload and deletion against S3-compatible storage.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import update

from horde import r2
from horde.classes.base.prompt_moderation import EvidenceTextState, PromptModerationEvent, PromptModerationReason
from horde.database import prompt_moderation as evidence_db
from horde.database.prompt_moderation import (
    PromptEvidence,
    RetentionPolicy,
    apply_evidence_retention,
    encode_evidence_text,
    record_prompt_evidence,
    upload_pending_text,
)

pytestmark = pytest.mark.unit

FAKE_BUCKET = "evidence-test"
"""The bucket name the fake store expects on every request."""


class FakeEvidenceStore:
    """Hold evidence objects in a dict and answer the S3 calls ``horde.r2`` makes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        """Stored object bodies by key."""
        self.refused_puts: set[str] = set()
        """Keys whose PUT fails with a client error."""
        self.refused_deletes: set[str] = set()
        """Keys a bulk delete reports as failed."""
        self.unreachable = False
        """Whether every request fails as if the store could not be reached."""
        self.delete_requests: list[list[str]] = []
        """The keys of each bulk delete request, in order."""

    def _check_reachable(self, operation: str) -> None:
        if self.unreachable:
            raise EndpointConnectionError(endpoint_url=f"https://store.invalid/{operation}")

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict[str, Any]:  # noqa: N803
        self._check_reachable("put")
        assert Bucket == FAKE_BUCKET
        assert ContentType == "application/json"
        if Key in self.refused_puts:
            raise ClientError({"Error": {"Code": "InternalError"}}, "PutObject")
        self.objects[Key] = Body
        return {}

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self._check_reachable("delete")
        assert Bucket == FAKE_BUCKET
        assert Delete["Quiet"] is True
        keys = [entry["Key"] for entry in Delete["Objects"]]
        assert len(keys) <= r2.EVIDENCE_DELETE_BATCH_SIZE
        self.delete_requests.append(keys)
        errors = []
        for key in keys:
            if key in self.refused_deletes:
                errors.append({"Key": key, "Code": "InternalError", "Message": "refused"})
            elif key not in self.objects:
                errors.append({"Key": key, "Code": "NoSuchKey", "Message": "missing"})
            else:
                del self.objects[key]
        return {"Errors": errors} if errors else {}

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, Any], ExpiresIn: int) -> str:  # noqa: N803
        return f"https://store.invalid/{Params['Key']}?method={ClientMethod}&expires={ExpiresIn}"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeEvidenceStore:
    fake = FakeEvidenceStore()
    monkeypatch.setattr(r2, "evidence_client", fake)
    monkeypatch.setattr(r2, "r2_evidence_bucket", FAKE_BUCKET)
    return fake


@pytest.fixture
def no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r2, "evidence_client", None)


def _record(**stages: str | None) -> int:
    fields: dict[str, Any] = {"submitted_prompt": "submitted", "moderation_prompt": "moderated", "effective_prompt": None, **stages}
    event_id = record_prompt_evidence(PromptEvidence(user_id=1, reason=PromptModerationReason.FILTER_REJECTION, **fields))
    assert event_id is not None
    return event_id


def _event(db_session, event_id: int) -> PromptModerationEvent:
    db_session.expire_all()
    event = db_session.get(PromptModerationEvent, event_id)
    assert event is not None
    return event


def _age(db_session, event_id: int, days: int) -> None:
    db_session.execute(
        update(PromptModerationEvent).where(PromptModerationEvent.id == event_id).values(created=datetime.utcnow() - timedelta(days=days)),
    )
    db_session.commit()


def _claimed(event_id: int) -> list[evidence_db.PendingText]:
    """Return the pending text of one event as the upload job claims it."""
    return [item for item in evidence_db.claim_pending_text(100) if item.event_id == event_id]


def _text_policy() -> RetentionPolicy:
    """Return a policy whose text window has passed for events aged 15 days and whose other limits have not."""
    return RetentionPolicy(text=timedelta(days=10), ipaddr=None, ceiling=timedelta(days=30))


class TestCanonicalTextObject:
    def test_object_is_compact_sorted_json_with_unescaped_unicode(self) -> None:
        encoded = encode_evidence_text("ein Bär", None, "x")
        assert encoded is not None
        body, digest, chars = encoded
        assert body == '{"effective_prompt":"x","moderation_prompt":null,"submitted_prompt":"ein Bär"}'.encode()
        assert digest == hashlib.sha256(body).hexdigest()
        assert chars == len("ein Bär") + 1

    def test_no_stage_has_no_object(self) -> None:
        assert encode_evidence_text(None, None, None) is None


class TestCapture:
    def test_text_waits_in_the_row_with_its_digest_and_length(self, db_session) -> None:
        event = _event(db_session, _record())

        assert event.text_state == EvidenceTextState.PENDING
        assert (event.submitted_prompt, event.moderation_prompt) == ("submitted", "moderated")
        encoded = encode_evidence_text("submitted", "moderated", None)
        assert encoded is not None
        assert (event.text_sha256, event.text_chars) == (encoded[1], encoded[2])

    def test_event_without_a_stage_holds_no_text(self, db_session) -> None:
        event = _event(db_session, _record(submitted_prompt=None, moderation_prompt=None))

        assert event.text_state == EvidenceTextState.NONE
        assert (event.text_sha256, event.text_chars) == (None, None)

    def test_capture_counts_each_new_event_by_reason_and_text_state(self, db_session, monkeypatch) -> None:
        captured = Mock()
        monkeypatch.setattr(evidence_db, "moderation_evidence_captured", captured)

        _record()

        captured.add.assert_called_once_with(1, {"horde.reason": "filter_rejection", "horde.text_state": "pending"})


class TestUpload:
    def test_upload_stores_the_canonical_object_and_empties_the_row(self, db_session, store) -> None:
        event_id = _record()

        result = upload_pending_text()

        assert (result.claimed, result.stored, result.failed) == (1, 1, 0)
        event = _event(db_session, event_id)
        assert event.text_state == EvidenceTextState.STORED
        assert (event.submitted_prompt, event.moderation_prompt, event.effective_prompt) == (None, None, None)
        body = store.objects[f"evidence/{event_id}.json"]
        assert hashlib.sha256(body).hexdigest() == event.text_sha256
        assert json.loads(body) == {"submitted_prompt": "submitted", "moderation_prompt": "moderated", "effective_prompt": None}
        assert upload_pending_text().claimed == 0

    def test_refused_upload_ends_the_cycle_and_leaves_later_texts_in_the_row(self, db_session, store) -> None:
        """Uploads run in ID order; the first refusal stops the cycle, so the events after it are not attempted."""
        accepted = _record()
        refused = _record()
        unattempted = _record()
        store.refused_puts.add(r2.evidence_object_key(refused))

        result = upload_pending_text()

        assert (result.claimed, result.stored, result.failed) == (3, 1, 1)
        assert _event(db_session, accepted).text_state == EvidenceTextState.STORED
        assert _event(db_session, refused).text_state == EvidenceTextState.PENDING
        assert _event(db_session, refused).submitted_prompt == "submitted"
        assert _event(db_session, unattempted).text_state == EvidenceTextState.PENDING
        assert set(store.objects) == {r2.evidence_object_key(accepted)}

    def test_unreachable_store_leaves_every_text_in_the_row(self, db_session, store) -> None:
        event_id = _record()
        store.unreachable = True

        result = upload_pending_text()

        assert (result.claimed, result.stored, result.failed) == (1, 0, 1)
        assert _event(db_session, event_id).text_state == EvidenceTextState.PENDING

    def test_pending_event_without_a_stage_moves_to_none_without_an_upload(self, db_session, store) -> None:
        """An event captured before the column existed defaults to pending, even if retention already removed its text."""
        event_id = _record()
        db_session.execute(
            update(PromptModerationEvent).where(PromptModerationEvent.id == event_id).values(submitted_prompt=None, moderation_prompt=None),
        )
        db_session.commit()

        assert upload_pending_text().claimed == 0

        assert _event(db_session, event_id).text_state == EvidenceTextState.NONE
        assert store.objects == {}

    def test_object_of_an_event_removed_during_its_upload_is_deleted(self, db_session, store, monkeypatch) -> None:
        """Retention removing the text while the PUT is in flight leaves the event removed and no object behind."""
        event_id = _record()
        put = r2.put_evidence_text

        def put_then_remove(uploaded_id: int, body: bytes) -> bool:
            stored = put(uploaded_id, body)
            db_session.execute(
                update(PromptModerationEvent)
                .where(PromptModerationEvent.id == uploaded_id)
                .values(submitted_prompt=None, moderation_prompt=None, text_state=EvidenceTextState.NONE.value, text_redacted=True),
            )
            db_session.commit()
            return stored

        monkeypatch.setattr(r2, "put_evidence_text", put_then_remove)

        result = upload_pending_text()

        assert result.stored == 0
        assert _event(db_session, event_id).text_state == EvidenceTextState.NONE
        assert store.objects == {}

    def test_object_recorded_by_another_run_is_kept(self, db_session, store, monkeypatch) -> None:
        """A second run that loses the conditional update leaves the object the winning run recorded."""
        event_id = _record()
        put = r2.put_evidence_text

        def put_while_another_run_records(uploaded_id: int, body: bytes) -> bool:
            stored = put(uploaded_id, body)
            db_session.execute(
                update(PromptModerationEvent)
                .where(PromptModerationEvent.id == uploaded_id)
                .values(submitted_prompt=None, moderation_prompt=None, text_state=EvidenceTextState.STORED.value),
            )
            db_session.commit()
            return stored

        monkeypatch.setattr(r2, "put_evidence_text", put_while_another_run_records)

        result = upload_pending_text()

        assert result.stored == 0
        assert _event(db_session, event_id).text_state == EvidenceTextState.STORED
        assert set(store.objects) == {r2.evidence_object_key(event_id)}
        assert store.delete_requests == []

    def test_upload_records_the_digest_and_length_of_an_event_captured_without_them(self, db_session, store) -> None:
        """An event captured before the digest and length columns existed gets both from its uploaded object."""
        event_id = _record()
        db_session.execute(
            update(PromptModerationEvent).where(PromptModerationEvent.id == event_id).values(text_sha256=None, text_chars=None),
        )
        db_session.commit()

        assert upload_pending_text().stored == 1

        event = _event(db_session, event_id)
        encoded = encode_evidence_text("submitted", "moderated", None)
        assert encoded is not None
        assert (event.text_sha256, event.text_chars) == (encoded[1], encoded[2])
        assert hashlib.sha256(store.objects[r2.evidence_object_key(event_id)]).hexdigest() == event.text_sha256

    def test_marking_twice_records_one_upload(self, db_session, store) -> None:
        event_id = _record()
        claimed = _claimed(event_id)

        assert evidence_db.mark_text_stored(claimed) == [event_id]
        assert evidence_db.mark_text_stored(claimed) == []


class TestUploadJob:
    @pytest.fixture
    def gauges(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
        from horde import metrics

        instruments = {
            name: Mock()
            for name in (
                "moderation_evidence_pending_rows",
                "moderation_evidence_oldest_pending_seconds",
                "moderation_evidence_uploads",
                "moderation_evidence_upload_failures",
                "moderation_rejections_per_minute",
                "moderation_rejecting_subjects",
                "moderation_observation_timestamp",
            )
        }
        for name, instrument in instruments.items():
            monkeypatch.setattr(metrics, name, instrument)
        return instruments

    def test_tick_uploads_the_backlog_after_recording_it(self, db_session, store, gauges) -> None:
        from horde.database import threads

        event_ids = [_record() for _ in range(3)]

        threads.upload_moderation_evidence_text()

        gauges["moderation_evidence_pending_rows"].set.assert_called_once_with(3)
        gauges["moderation_rejections_per_minute"].set.assert_called_once_with(3 / 5)
        gauges["moderation_evidence_uploads"].add.assert_any_call(3)
        gauges["moderation_observation_timestamp"].set.assert_called_once()
        assert {_event(db_session, event_id).text_state for event_id in event_ids} == {EvidenceTextState.STORED}

    def test_tick_drains_more_than_one_batch(self, db_session, store, gauges, monkeypatch) -> None:
        from horde.database import threads

        monkeypatch.setattr(evidence_db, "EVIDENCE_UPLOAD_BATCH_SIZE", 2)
        event_ids = [_record() for _ in range(5)]

        threads.upload_moderation_evidence_text()

        assert {_event(db_session, event_id).text_state for event_id in event_ids} == {EvidenceTextState.STORED}

    def test_tick_without_a_store_records_the_backlog_and_uploads_nothing(self, db_session, no_store, gauges) -> None:
        from horde.database import threads

        event_id = _record()

        threads.upload_moderation_evidence_text()

        gauges["moderation_evidence_pending_rows"].set.assert_called_once_with(1)
        assert _event(db_session, event_id).text_state == EvidenceTextState.PENDING

    def test_tick_uploads_nothing_while_another_process_holds_the_lock(self, app, db_session, store, gauges) -> None:
        from horde.database import threads
        from horde.flask import db

        event_id = _record()
        with db.engine.connect() as holder:
            assert evidence_db.try_evidence_upload_lock(holder)
            try:
                threads.upload_moderation_evidence_text()
            finally:
                evidence_db.release_evidence_upload_lock(holder)

        assert _event(db_session, event_id).text_state == EvidenceTextState.PENDING
        threads.upload_moderation_evidence_text()
        assert _event(db_session, event_id).text_state == EvidenceTextState.STORED


class TestRetentionDeletesStoredText:
    def test_text_removal_deletes_the_object_and_records_no_text(self, db_session, store) -> None:
        event_id = _record()
        upload_pending_text()
        _age(db_session, event_id, days=15)

        assert apply_evidence_retention(_text_policy()).text_redacted == 1

        event = _event(db_session, event_id)
        assert event.text_redacted
        assert event.text_state == EvidenceTextState.NONE
        assert (event.text_sha256, event.text_chars) == (None, None)
        assert store.objects == {}

    def test_one_delete_request_covers_every_selected_event(self, db_session, store) -> None:
        """Pending events are deleted too, since an upload in flight may have stored an object for one."""
        stored_id = _record()
        upload_pending_text()
        pending_id = _record()
        for event_id in (stored_id, pending_id):
            _age(db_session, event_id, days=15)

        assert apply_evidence_retention(_text_policy()).text_redacted == 2

        assert [sorted(keys) for keys in store.delete_requests] == [sorted(r2.evidence_object_key(i) for i in (stored_id, pending_id))]

    def test_unreachable_store_flags_only_events_without_a_stored_object(self, db_session, store) -> None:
        """A stored event waits for its object's deletion; a pending event in the same batch is flagged."""
        stored_id = _record()
        upload_pending_text()
        pending_id = _record()
        for event_id in (stored_id, pending_id):
            _age(db_session, event_id, days=15)
        store.unreachable = True

        assert apply_evidence_retention(_text_policy()).text_redacted == 1

        assert _event(db_session, stored_id).text_state == EvidenceTextState.STORED
        assert not _event(db_session, stored_id).text_redacted
        assert _event(db_session, pending_id).text_redacted
        assert _event(db_session, pending_id).text_state == EvidenceTextState.NONE
        store.unreachable = False
        assert apply_evidence_retention(_text_policy()).text_redacted == 1
        assert _event(db_session, stored_id).text_redacted

    def test_only_events_whose_object_was_deleted_are_flagged(self, db_session, store) -> None:
        refused, deleted = _record(), _record()
        upload_pending_text()
        for event_id in (refused, deleted):
            _age(db_session, event_id, days=15)
        store.refused_deletes.add(r2.evidence_object_key(refused))

        assert apply_evidence_retention(_text_policy()).text_redacted == 1

        assert not _event(db_session, refused).text_redacted
        assert _event(db_session, deleted).text_redacted

    def test_anonymization_waits_for_the_object_deletion(self, db_session, store) -> None:
        """Only an event with a stored object waits; an unreachable store never holds back the others' anonymization."""
        event_id = _record()
        upload_pending_text()
        pending_id = _record()
        for aged_id in (event_id, pending_id):
            _age(db_session, aged_id, days=31)
        store.unreachable = True
        policy = RetentionPolicy(text=None, ipaddr=None, ceiling=timedelta(days=30))

        assert apply_evidence_retention(policy).anonymized == 1
        assert _event(db_session, event_id).user_id == 1
        assert _event(db_session, pending_id).user_id is None

        store.unreachable = False
        assert apply_evidence_retention(policy).anonymized == 1
        event = _event(db_session, event_id)
        assert (event.user_id, event.text_state) == (None, EvidenceTextState.NONE)
        assert store.objects == {}

    def test_without_a_store_only_events_holding_text_in_the_row_are_flagged(self, db_session, store, monkeypatch) -> None:
        stored_id = _record()
        upload_pending_text()
        pending_id = _record()
        for event_id in (stored_id, pending_id):
            _age(db_session, event_id, days=15)
        monkeypatch.setattr(r2, "evidence_client", None)
        monkeypatch.setattr(evidence_db, "_last_no_store_warning", None)
        logger = Mock()
        monkeypatch.setattr(evidence_db, "logger", logger)

        assert apply_evidence_retention(_text_policy()).text_redacted == 1
        assert apply_evidence_retention(_text_policy()).text_redacted == 0

        assert not _event(db_session, stored_id).text_redacted
        assert _event(db_session, pending_id).text_state == EvidenceTextState.NONE
        # The second pass meets the same waiting row inside the warning interval, so the warning is not repeated.
        logger.warning.assert_called_once_with(
            "{} stored evidence rows kept past their window: no evidence store is configured",
            1,
        )


class TestEvidenceStoreCalls:
    def test_bulk_delete_is_chunked_and_treats_missing_keys_as_deleted(self, store) -> None:
        store.objects[r2.evidence_object_key(1)] = b"{}"
        store.refused_deletes.add(r2.evidence_object_key(2))

        failed = r2.delete_evidence_texts(range(1, r2.EVIDENCE_DELETE_BATCH_SIZE + 2))

        assert failed == {2}
        assert [len(keys) for keys in store.delete_requests] == [r2.EVIDENCE_DELETE_BATCH_SIZE, 1]
        assert store.objects == {}

    def test_unreachable_delete_raises(self, store) -> None:
        store.unreachable = True

        with pytest.raises(EndpointConnectionError):
            r2.delete_evidence_texts([1])

    def test_refused_put_reports_failure(self, store) -> None:
        store.refused_puts.add(r2.evidence_object_key(7))

        assert r2.put_evidence_text(7, b"{}") is False
        assert r2.put_evidence_text(8, b"{}") is True

    def test_without_a_store_nothing_is_stored_or_deleted(self, no_store) -> None:
        assert r2.put_evidence_text(7, b"{}") is False
        assert r2.delete_evidence_texts([7, 8]) == {7, 8}
        assert r2.evidence_text_url(7) is None


@pytest.fixture
def _real_store(object_store_ready: None) -> Iterator[None]:
    """Use the S3-compatible evidence bucket the test runtime provisions."""
    if r2.evidence_client is None:
        pytest.fail(f"The object-storage runtime set no {r2.EVIDENCE_ACCOUNT_ENV} and {r2.EVIDENCE_BUCKET_ENV}")
    yield


@pytest.mark.object_storage
@pytest.mark.usefixtures("_real_store")
def test_upload_signed_read_and_bulk_delete_against_object_storage(db_session) -> None:
    import requests

    event_id = _record(submitted_prompt="ein Bär")
    assert upload_pending_text().stored == 1
    event = _event(db_session, event_id)

    url = r2.evidence_text_url(event_id)
    assert url is not None
    response = requests.get(url, timeout=10)
    assert response.status_code == 200, response.text
    assert hashlib.sha256(response.content).hexdigest() == event.text_sha256
    assert response.headers["Cache-Control"] == "private, no-store"
    assert json.loads(response.content.decode("utf-8"))["submitted_prompt"] == "ein Bär"

    assert r2.delete_evidence_texts([event_id, event_id + 1]) == set()
    assert requests.get(url, timeout=10).status_code == 404


def test_listing_reports_where_the_text_is_held(db_session, store) -> None:
    pending_id = _record()
    stored_id = _record()
    evidence_db.mark_text_stored(_claimed(stored_id))

    events = {event["id"]: event for event in evidence_db.get_prompt_events(limit=10)["events"]}

    assert events[pending_id]["text_state"] == "pending"
    assert events[pending_id]["submitted_prompt"] == "submitted"
    assert events[stored_id]["text_state"] == "stored"
    assert events[stored_id]["submitted_prompt"] is None
    assert events[stored_id]["text_chars"] == len("submitted") + len("moderated")
    assert len(events[stored_id]["text_sha256"]) == 64


def test_upload_lock_is_a_session_lock_released_by_its_holder(app, db_session) -> None:
    from horde.flask import db

    with db.engine.connect() as first, db.engine.connect() as second:
        assert evidence_db.try_evidence_upload_lock(first)
        assert not evidence_db.try_evidence_upload_lock(second)
        evidence_db.release_evidence_upload_lock(first)
        assert evidence_db.try_evidence_upload_lock(second)
        evidence_db.release_evidence_upload_lock(second)
