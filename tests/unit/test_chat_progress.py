# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The stream lease a streamed chat generation holds, its delta stream and the append script's sequence and caps."""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest

from horde import chat_progress
from horde.chat_progress import (
    CHAT_PROGRESS_BYTES_PER_TOKEN,
    CHAT_PROGRESS_LEASE_GRACE_SECONDS,
    CHAT_PROGRESS_STREAM_TTL_SECONDS,
    ProgressAppend,
    ProgressOutcome,
    StreamLease,
    api_key_digest,
    append_progress,
    chat_lease_key,
    chat_stream_key,
    end_progress_stream,
    read_stream_lease,
    release_stream_lease,
    write_stream_lease,
)

GENERATION_ID = "5b0a54d6-1c55-4b39-8b1e-0e6c4a1c9f10"
REQUEST_ID = "c6f4d1b2-7f0e-4a35-9f3e-2b8f6c1d0a77"
STORED_API_KEY = "salted-hash-of-the-owner-key"
JOB_TTL = 150
MODEL = "koboldcpp/Llama-3-8B"
MAX_LENGTH = 4
"""Four tokens, so the byte cap is 64 bytes and a test reaches it with short deltas."""


@pytest.fixture
def progress_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[fakeredis.FakeStrictRedis]:
    """Route the progress module to an in-memory Redis, with the append script registered on it afresh."""
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(chat_progress, "_client", fake)
    monkeypatch.setattr(chat_progress, "_append_script", None)
    yield fake


@pytest.fixture
def lua_redis(progress_redis: fakeredis.FakeStrictRedis) -> fakeredis.FakeStrictRedis:
    """The in-memory Redis, for tests that run the append script; fakeredis runs Lua only with ``lupa``."""
    pytest.importorskip("lupa", reason="fakeredis runs Lua scripts only with lupa installed (fakeredis[lua]).")
    return progress_redis


def _write_lease(*, fake: bool = False, max_length: int = MAX_LENGTH) -> None:
    write_stream_lease(
        GENERATION_ID,
        request_id=REQUEST_ID,
        worker_id="worker-1",
        model=MODEL,
        owner_id=7,
        owner_api_key=STORED_API_KEY,
        max_length=max_length,
        job_ttl=JOB_TTL,
        fake=fake,
    )


def _lease() -> StreamLease:
    lease = read_stream_lease(GENERATION_ID)
    assert lease is not None
    return lease


def _stream_entries(redis_client: fakeredis.FakeStrictRedis) -> list[dict[str, str]]:
    return [fields for _, fields in redis_client.xrange(chat_stream_key(REQUEST_ID))]


def test_a_lease_holds_the_request_worker_model_owner_and_byte_cap_until_past_the_job_ttl(
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease()

    lease_key = chat_lease_key(GENERATION_ID)
    assert lease_key == f"horde:chat:lease:{GENERATION_ID}"
    assert progress_redis.hgetall(lease_key) == {
        "request": REQUEST_ID,
        "worker": "worker-1",
        "model": MODEL,
        "owner": "7",
        "key": api_key_digest(STORED_API_KEY),
        "max_bytes": str(MAX_LENGTH * CHAT_PROGRESS_BYTES_PER_TOKEN),
        "fake": "0",
    }
    assert JOB_TTL < progress_redis.ttl(lease_key) <= JOB_TTL + CHAT_PROGRESS_LEASE_GRACE_SECONDS


def test_a_lease_holds_a_digest_of_the_stored_key_and_not_the_stored_key(
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease()

    lease = _lease()

    assert lease.key_digest == api_key_digest(STORED_API_KEY)
    assert STORED_API_KEY not in progress_redis.hvals(chat_lease_key(GENERATION_ID))


def test_a_lease_reads_back_as_the_route_uses_it(progress_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()

    assert read_stream_lease(GENERATION_ID) == StreamLease(
        generation_id=GENERATION_ID,
        request_id=REQUEST_ID,
        worker_id="worker-1",
        owner_id=7,
        key_digest=api_key_digest(STORED_API_KEY),
    )


def test_a_generation_without_a_lease_reads_as_none(progress_redis: fakeredis.FakeStrictRedis) -> None:
    assert read_stream_lease(GENERATION_ID) is None


def test_a_lease_write_redis_refuses_is_logged_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    monkeypatch.setattr(chat_progress, "_client", fakeredis.FakeStrictRedis(server=server, decode_responses=True))

    _write_lease()


def test_a_fault_appends_the_ended_marker_and_deletes_the_lease(progress_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()

    end_progress_stream(GENERATION_ID, REQUEST_ID)

    assert _stream_entries(progress_redis) == [{"gen": GENERATION_ID, "ended": "1"}]
    assert 0 < progress_redis.ttl(chat_stream_key(REQUEST_ID)) <= CHAT_PROGRESS_STREAM_TTL_SECONDS
    assert not progress_redis.exists(chat_lease_key(GENERATION_ID))


def test_a_fault_of_a_generation_without_a_lease_creates_no_stream(progress_redis: fakeredis.FakeStrictRedis) -> None:
    end_progress_stream(GENERATION_ID, REQUEST_ID)

    assert not progress_redis.exists(chat_stream_key(REQUEST_ID))


def test_a_fault_of_a_fake_generation_deletes_its_lease_without_a_marker(
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease(fake=True)

    end_progress_stream(GENERATION_ID, REQUEST_ID)

    assert not progress_redis.exists(chat_stream_key(REQUEST_ID))
    assert not progress_redis.exists(chat_lease_key(GENERATION_ID))


def test_a_finished_generation_releases_its_lease_and_keeps_its_stream(
    progress_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease()
    progress_redis.xadd(chat_stream_key(REQUEST_ID), {"gen": GENERATION_ID, "seq": 1, "content": "Hel"})

    release_stream_lease(GENERATION_ID)

    assert not progress_redis.exists(chat_lease_key(GENERATION_ID))
    assert progress_redis.exists(chat_stream_key(REQUEST_ID))


def test_deltas_in_sequence_are_appended_with_the_generation_worker_and_model_and_the_stream_expires(
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease()

    first = append_progress(_lease(), 1, "Hel")
    second = append_progress(_lease(), 2, "lo")

    assert (first, second) == (
        ProgressAppend(ProgressOutcome.ACCEPTED, 1),
        ProgressAppend(ProgressOutcome.ACCEPTED, 2),
    )
    assert _stream_entries(lua_redis) == [
        {"gen": GENERATION_ID, "seq": "1", "content": "Hel", "worker": "worker-1", "model": MODEL},
        {"gen": GENERATION_ID, "seq": "2", "content": "lo", "worker": "worker-1", "model": MODEL},
    ]
    assert 0 < lua_redis.ttl(chat_stream_key(REQUEST_ID)) <= CHAT_PROGRESS_STREAM_TTL_SECONDS


def test_a_delta_under_a_lease_without_a_model_is_appended_without_one(lua_redis: fakeredis.FakeStrictRedis) -> None:
    """A lease written by a Horde that did not store the model still takes posts; its entries leave the model out."""
    _write_lease()
    lua_redis.hdel(chat_lease_key(GENERATION_ID), "model")

    appended = append_progress(_lease(), 1, "Hel")

    assert appended == ProgressAppend(ProgressOutcome.ACCEPTED, 1)
    assert _stream_entries(lua_redis) == [{"gen": GENERATION_ID, "seq": "1", "content": "Hel", "worker": "worker-1"}]


def test_an_identical_retry_of_the_last_delta_is_acknowledged_without_a_second_entry(
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease()
    append_progress(_lease(), 1, "Hel")

    retry = append_progress(_lease(), 1, "Hel")

    assert retry == ProgressAppend(ProgressOutcome.DUPLICATE, 1)
    assert len(_stream_entries(lua_redis)) == 1


def test_a_changed_repeat_of_the_last_delta_is_refused_with_the_next_seq(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()
    append_progress(_lease(), 1, "Hel")

    changed = append_progress(_lease(), 1, "Help")

    assert changed == ProgressAppend(ProgressOutcome.GAP, 2)
    assert _stream_entries(lua_redis) == [{"gen": GENERATION_ID, "seq": "1", "content": "Hel", "worker": "worker-1", "model": MODEL}]


def test_a_gap_is_refused_with_the_seq_expected_next(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()
    append_progress(_lease(), 1, "Hel")

    skipped = append_progress(_lease(), 3, "lo")

    assert skipped == ProgressAppend(ProgressOutcome.GAP, 2)
    assert len(_stream_entries(lua_redis)) == 1


def test_a_first_delta_other_than_seq_one_is_refused(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()

    assert append_progress(_lease(), 2, "lo") == ProgressAppend(ProgressOutcome.GAP, 1)
    assert not lua_redis.exists(chat_stream_key(REQUEST_ID))


def test_a_delta_past_the_entry_cap_is_refused(lua_redis: fakeredis.FakeStrictRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_progress, "MAX_CHAT_PROGRESS_ENTRIES", 2)
    _write_lease()
    append_progress(_lease(), 1, "a")
    append_progress(_lease(), 2, "b")

    assert append_progress(_lease(), 3, "c") == ProgressAppend(ProgressOutcome.OVER_CAP, 3)
    assert len(_stream_entries(lua_redis)) == 2


def test_a_delta_past_the_byte_cap_is_refused(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease(max_length=1)
    append_progress(_lease(), 1, "x" * CHAT_PROGRESS_BYTES_PER_TOKEN)

    assert append_progress(_lease(), 2, "y") == ProgressAppend(ProgressOutcome.OVER_CAP, 2)
    assert len(_stream_entries(lua_redis)) == 1


def test_the_byte_cap_counts_utf8_bytes_not_characters(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease(max_length=1)

    # Six four-byte characters are 24 bytes, past the 16 bytes one token allows.
    assert append_progress(_lease(), 1, "\U0001f600" * 6) == ProgressAppend(ProgressOutcome.OVER_CAP, 1)


def test_a_fake_generation_is_acknowledged_without_appending(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease(fake=True)

    assert append_progress(_lease(), 1, "Hel") == ProgressAppend(ProgressOutcome.ACCEPTED, 1)
    assert append_progress(_lease(), 3, "lo") == ProgressAppend(ProgressOutcome.GAP, 2)
    assert not lua_redis.exists(chat_stream_key(REQUEST_ID))


def test_a_lease_deleted_after_it_was_read_refuses_the_append(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()
    lease = _lease()
    release_stream_lease(GENERATION_ID)

    assert append_progress(lease, 1, "Hel") == ProgressAppend(ProgressOutcome.NO_LEASE, 0)
    assert not lua_redis.exists(chat_stream_key(REQUEST_ID))


def test_a_delta_with_reasoning_is_appended_with_the_reasoning_beside_the_content(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease()

    first = append_progress(_lease(), 1, "", "Think")
    second = append_progress(_lease(), 2, "Hi")

    assert (first, second) == (
        ProgressAppend(ProgressOutcome.ACCEPTED, 1),
        ProgressAppend(ProgressOutcome.ACCEPTED, 2),
    )
    assert _stream_entries(lua_redis) == [
        {"gen": GENERATION_ID, "seq": "1", "content": "", "reasoning_content": "Think", "worker": "worker-1", "model": MODEL},
        {"gen": GENERATION_ID, "seq": "2", "content": "Hi", "worker": "worker-1", "model": MODEL},
    ]


def test_an_identical_retry_of_a_reasoning_delta_is_acknowledged_and_a_changed_one_refused(
    lua_redis: fakeredis.FakeStrictRedis,
) -> None:
    _write_lease()
    append_progress(_lease(), 1, "", "Think")

    retry = append_progress(_lease(), 1, "", "Think")
    changed = append_progress(_lease(), 1, "", "Thought")

    assert retry == ProgressAppend(ProgressOutcome.DUPLICATE, 1)
    assert changed == ProgressAppend(ProgressOutcome.GAP, 2)
    assert len(_stream_entries(lua_redis)) == 1


def test_reasoning_counts_toward_the_byte_cap_with_the_content(lua_redis: fakeredis.FakeStrictRedis) -> None:
    _write_lease(max_length=1)

    half_cap = CHAT_PROGRESS_BYTES_PER_TOKEN // 2
    assert append_progress(_lease(), 1, "x" * half_cap, "y" * (half_cap + 1)) == ProgressAppend(ProgressOutcome.OVER_CAP, 1)
    assert not lua_redis.exists(chat_stream_key(REQUEST_ID))
