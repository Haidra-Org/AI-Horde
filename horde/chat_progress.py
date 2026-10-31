# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content deltas a chat worker posts while it generates, kept in a retained Redis stream per request.

A chat request whose client streams carries the stream hint. When a worker pops it,
[`write_stream_lease`][horde.chat_progress.write_stream_lease] writes ``horde:chat:lease:<generation id>``, a hash
holding the request, the worker, the model, the worker's owner, a digest of the owner's API key and the byte cap.
The progress route authenticates a post against that lease alone, so a post does not read Postgres. The lease also
holds the job lease's start, current ``job_ttl`` and cap, so only a post that extends the job lease writes Postgres.

[`append_progress`][horde.chat_progress.append_progress] appends one delta to ``horde:chat:stream:<request id>`` with
the fields ``gen`` (the generation id), ``seq``, ``content``, ``reasoning_content`` when the delta carries
reasoning, ``worker`` and ``model``, the last two copied from the lease, in one Lua script that also enforces the
sequence and the caps. The worker and model let the front compute the reply's ``system_fingerprint`` from the first
delta; an entry appended under a lease without a model leaves it out. The lease hash holds the generation's last
accepted ``seq``, the bytes accepted so far and the last content and reasoning, so the check does not need a second
key or a read of the stream. When a generation with a lease faults,
[`end_progress_stream`][horde.chat_progress.end_progress_stream] appends ``gen`` and ``ended`` (``1``) and deletes the
lease, so a reader learns at once that the attempt it follows is gone. When the request is cancelled,
[`cancel_progress_streams`][horde.chat_progress.cancel_progress_streams] sets ``cancelled`` on each lease it still
has and appends the same marker for each, and keeps the leases, so the acknowledgement of the worker's next post
tells the worker to stop. Before that, [`read_posted_progress`][horde.chat_progress.read_posted_progress] reads the
content each pending generation posted, which the cancel settles the generation on.

Unlike [`horde.chat_signal`][horde.chat_signal], whose messages never carry text, the stream holds generated text. It
lives on the chat signal database, which the front reads, and expires on its own. Writing the lease, deleting it,
flagging it cancelled and the ended marker are best effort: a Redis failure is logged and never fails the pop, submit,
fault or cancel that wrote them.
"""

from __future__ import annotations

import enum
import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files

import redis
from redis.commands.core import Script

from horde.logger import logger
from horde.redis_ctrl import get_chat_signal_db

CHAT_PROGRESS_LEASE_PREFIX = "horde:chat:lease:"
"""Prefix of a streamed generation's lease key; the generation id follows it."""

CHAT_PROGRESS_STREAM_PREFIX = "horde:chat:stream:"
"""Prefix of a chat request's delta stream key; the request id follows it."""

CHAT_PROGRESS_LEASE_GRACE_SECONDS = 90
"""How long a lease outlives the job's TTL. The stale-generation thread faults an expired job on a 60 second tick,
so a generation stays pending for up to a tick past its TTL; the lease covers that window and the pass itself, and a
worker posting in it is still authenticated and its fault still ends the stream."""

CHAT_PROGRESS_STREAM_TTL_SECONDS = 900
"""How long a stream is kept after its last append: the front's 600 second stream budget plus 300 seconds, so a
reader that stalled or reconciles at the terminal outcome still finds every delta."""

CHAT_PROGRESS_BYTES_PER_TOKEN = 16
"""The UTF-8 bytes a generation may post per token of its ``max_length``. A token decodes to a few characters, at
most four bytes each, so honest output stays well inside it; the cap bounds what a worker can store, since the lease
is the only record of the request the route reads."""

MAX_CHAT_PROGRESS_ENTRIES = 1000
"""The most deltas one generation may post. At the expected one post per second it covers past the front's 600
second stream budget and any text job's TTL; a faster poster is cut off rather than served."""

CHAT_PROGRESS_STREAM_MAX_ENTRIES = 4 * MAX_CHAT_PROGRESS_ENTRIES
"""The approximate length a request's stream is trimmed to. A request is attempted at most three times before its
faults fault it, so four generations' worth of deltas plus their ended markers fit."""

CHAT_PROGRESS_NOT_STREAMING_RC = "ChatProgressNotStreaming"
"""Return code for a post for a stored, unfaulted generation without a lease: not streamed, finished or past its
lease. The worker stops posting and still submits."""

CHAT_PROGRESS_OUT_OF_SEQUENCE_RC = "ChatProgressOutOfSequence"
"""Return code for a post whose ``seq`` is not the next one, or that changes the content of the last one; the
refusal carries the ``seq`` expected next."""

CHAT_PROGRESS_CAP_REACHED_RC = "ChatProgressCapReached"
"""Return code for a post that would pass the generation's entry or byte cap. The worker stops posting and still
submits."""

CHAT_PROGRESS_UNAVAILABLE_RC = "ChatProgressUnavailable"
"""Return code for a post Redis could not take. The worker may retry the same ``seq`` or stop posting."""

CHAT_JOB_ABORTED_RC = "ChatJobAborted"
"""Return code for a post for a generation without a lease that is faulted, as by a lapsed lease or a cancel, or not
stored, as after its request expired. The worker stops generating and does not submit."""

LEASE_REQUEST_FIELD = "request"
"""Lease field holding the request id."""

LEASE_WORKER_FIELD = "worker"
"""Lease field holding the worker id."""

LEASE_MODEL_FIELD = "model"
"""Lease field holding the model the generation was assigned for."""

LEASE_OWNER_FIELD = "owner"
"""Lease field holding the worker owner's user id."""

LEASE_KEY_DIGEST_FIELD = "key"
"""Lease field holding [`api_key_digest`][horde.chat_progress.api_key_digest] of the owner's stored API key."""

LEASE_MAX_BYTES_FIELD = "max_bytes"
"""Lease field holding the bytes the generation may post in all."""

LEASE_FAKE_FIELD = "fake"
"""Lease field that is ``1`` for a fake generation, whose posts are acknowledged and not appended so the worker
cannot tell it apart from a real one."""

LEASE_STARTED_FIELD = "started"
"""Lease field holding the generation's ``start_time`` as Unix seconds, the point its job lease is measured from."""

LEASE_JOB_TTL_FIELD = "job_ttl"
"""Lease field holding the generation's current ``job_ttl``, so a progress post that does not extend the job lease
does not read Postgres."""

LEASE_CAP_FIELD = "lease_cap"
"""Lease field holding the longest ``job_ttl`` progress posts may extend the job lease to."""

LEASE_TRUSTED_FIELD = "trusted"
"""Lease field that is ``1`` when the worker's owner was trusted at pop, which set the lease cap."""

LEASE_CANCELLED_FIELD = "cancelled"
"""Lease field that is ``1`` once the request was cancelled; absent before. The append script copies it into the
acknowledgement so the worker stops generating."""

LEASE_SEQ_FIELD = "seq"
"""Lease field the append script keeps the last accepted ``seq`` in; absent before the first post."""

LEASE_BYTES_FIELD = "bytes"
"""Lease field the append script keeps the bytes accepted so far in."""

LEASE_LAST_CONTENT_FIELD = "last"
"""Lease field the append script keeps the last accepted content in, to tell a retry from a change. One delta is
small next to the byte cap, so the copy costs less than reading the stream back."""

LEASE_LAST_REASONING_FIELD = "last_reasoning"
"""Lease field the append script keeps the last accepted reasoning content in, empty when it carried none, so a retry
is told from a change as for ``last``."""

STREAM_GENERATION_FIELD = "gen"
"""Stream entry field holding the generation id, the attempt the entry belongs to."""

STREAM_SEQ_FIELD = "seq"
"""Stream entry field holding the delta's sequence number, from 1 per generation."""

STREAM_CONTENT_FIELD = "content"
"""Stream entry field holding the delta's text; empty when the delta carries only reasoning."""

STREAM_REASONING_CONTENT_FIELD = "reasoning_content"
"""Stream entry field holding the delta's reasoning, never part of ``content``; absent when the delta carries none."""

STREAM_WORKER_FIELD = "worker"
"""Stream entry field holding the worker id, copied from the lease."""

STREAM_MODEL_FIELD = "model"
"""Stream entry field holding the model, copied from the lease."""

STREAM_ENDED_FIELD = "ended"
"""Stream entry field, ``1``, marking that the generation faulted and does not post more deltas."""


class ProgressOutcome(enum.StrEnum):
    """What became of one progress post."""

    ACCEPTED = "accepted"
    """The delta was appended."""

    DUPLICATE = "duplicate"
    """An identical repeat of the last accepted delta, acknowledged without appending it again."""

    GAP = "gap"
    """A sequence number other than the next one, or changed content for the last one; the delta was not appended."""

    OVER_CAP = "over_cap"
    """The delta would pass the generation's entry or byte cap; the delta was not appended."""

    NO_LEASE = "no_lease"
    """The generation has no lease and is stored and not faulted: it is not streamed, finished or past its lease."""

    FORBIDDEN = "forbidden"
    """The API key is not the key of the generation's worker owner."""

    ABORTED = "aborted"
    """The generation has no lease and is faulted or not stored; the worker stops generating and does not submit."""

    UNAVAILABLE = "unavailable"
    """Redis could not be reached."""


@dataclass(frozen=True)
class StreamLease:
    """The lease of a generation whose deltas are streamed, as the progress route reads it."""

    generation_id: str
    """The generation the lease authorizes posts for."""

    request_id: str
    """The chat request the generation belongs to; its id is the stream key's suffix."""

    worker_id: str
    """The worker the generation was assigned to."""

    owner_id: int
    """The user id of the worker's owner."""

    key_digest: str
    """[`api_key_digest`][horde.chat_progress.api_key_digest] of the owner's stored API key."""

    started_at: float | None = None
    """The generation's ``start_time`` as Unix seconds; None on a lease written without the job lease fields."""

    job_ttl: int | None = None
    """The generation's current ``job_ttl``; None on a lease written without the job lease fields."""

    lease_cap: int | None = None
    """The longest ``job_ttl`` progress may extend the job lease to; None on a lease written without it."""

    trusted: bool = False
    """Whether the worker's owner was trusted at pop."""


@dataclass(frozen=True)
class ProgressAppend:
    """The result of [`append_progress`][horde.chat_progress.append_progress]."""

    outcome: ProgressOutcome
    """What became of the post."""

    seq: int
    """The accepted ``seq`` for an accepted or duplicate post; the ``seq`` the generation expects next otherwise."""

    cancelled: bool = False
    """Whether the request was cancelled, for an accepted or duplicate post; False for a refused one."""


@dataclass(frozen=True)
class PostedProgress:
    """Represents the content a generation posted before its request was cancelled, as the server measured it."""

    content: str
    """The posted content joined in ``seq`` order; empty when the stream no longer holds the generation's entries."""

    chars: int
    """The characters the generation is settled on: ``len(content)`` when the stream holds the entries, else the
    lease's accepted bytes."""


EMPTY_POSTED_PROGRESS = PostedProgress(content="", chars=0)
"""The posted progress of a generation that posted nothing, or whose progress could not be read."""


APPEND_PROGRESS_SCRIPT: str = files(__package__).joinpath("chat_progress.lua").read_text(encoding="utf-8")
"""The Lua source of the progress append, read from ``chat_progress.lua`` beside this module so that it is
written and reviewed as Lua. It appends one delta under the generation's lease. ``KEYS``: the lease, the stream.
``ARGV``: the generation id,
``seq``, the content, [`MAX_CHAT_PROGRESS_ENTRIES`][horde.chat_progress.MAX_CHAT_PROGRESS_ENTRIES],
[`CHAT_PROGRESS_STREAM_TTL_SECONDS`][horde.chat_progress.CHAT_PROGRESS_STREAM_TTL_SECONDS] and
[`CHAT_PROGRESS_STREAM_MAX_ENTRIES`][horde.chat_progress.CHAT_PROGRESS_STREAM_MAX_ENTRIES] and the reasoning content,
empty when the delta carries none. Replies with the outcome
and a ``seq``, and the ``cancelled`` flag for an accepted or duplicate post, as
[`ProgressAppend`][horde.chat_progress.ProgressAppend] holds them. The sequence state lives in the
lease hash, so the script reads one key besides the stream it writes and expires with the lease."""

_client_lock = threading.Lock()
_client: redis.Redis | None = None
_append_script: Script | None = None


def _get_client() -> redis.Redis:
    """Return the shared client, creating it once; a redis client reconnects on its own."""
    global _client
    with _client_lock:
        if _client is None:
            _client = get_chat_signal_db()
        return _client


def _get_append_script() -> Script:
    """Return the append script registered on the shared client, registering it once.

    The script is sent by digest and loaded again by redis-py when the server lost it, so registering it once per
    process is enough.
    """
    global _append_script
    client = _get_client()
    with _client_lock:
        if _append_script is None or _append_script.registered_client is not client:
            _append_script = client.register_script(APPEND_PROGRESS_SCRIPT)
        return _append_script


def chat_lease_key(generation_id: str) -> str:
    """Return the lease key of a generation.

    Args:
        generation_id: The generation's id.

    Returns:
        ``horde:chat:lease:<generation id>``.
    """
    return f"{CHAT_PROGRESS_LEASE_PREFIX}{generation_id}"


def chat_stream_key(request_id: str) -> str:
    """Return the delta stream key of a chat request.

    Args:
        request_id: The chat request's id.

    Returns:
        ``horde:chat:stream:<request id>``.
    """
    return f"{CHAT_PROGRESS_STREAM_PREFIX}{request_id}"


def api_key_digest(stored_api_key: str) -> str:
    """Return the digest a lease holds of an API key, from the salted hash the user row stores.

    The lease holds a digest of the stored hash, not the hash itself, so a reader of the chat signal database does not
    learn anything it could match against the users table.

    Args:
        stored_api_key: The salted hash ``horde.utils.hash_api_key`` returns, as the user row stores it.

    Returns:
        64 lowercase hexadecimal characters.
    """
    return hashlib.sha256(stored_api_key.encode("utf-8")).hexdigest()


def write_stream_lease(
    generation_id: str,
    *,
    request_id: str,
    worker_id: str,
    model: str,
    owner_id: int,
    owner_api_key: str,
    max_length: int,
    job_ttl: int,
    started_at: float,
    lease_cap: int,
    trusted: bool,
    fake: bool,
) -> None:
    """Write the lease that lets a generation's worker post deltas, logging rather than raising on a Redis failure.

    Args:
        generation_id: The generation assigned to the worker.
        request_id: The chat request it belongs to.
        worker_id: The worker it was assigned to.
        model: The model it was assigned for.
        owner_id: The user id of the worker's owner.
        owner_api_key: The owner's stored API key hash.
        max_length: The tokens the job may generate, which sizes the byte cap.
        job_ttl: The job's TTL in seconds.
        started_at: The generation's ``start_time`` as Unix seconds.
        lease_cap: The longest ``job_ttl`` progress posts may extend the job lease to.
        trusted: Whether the worker's owner is trusted, which set ``lease_cap``.
        fake: Whether the generation is fake.

    Side Effects:
        Replaces the lease hash and sets its TTL to the job's TTL plus
        [`CHAT_PROGRESS_LEASE_GRACE_SECONDS`][horde.chat_progress.CHAT_PROGRESS_LEASE_GRACE_SECONDS].
    """
    lease_key = chat_lease_key(generation_id)
    lease_fields = {
        LEASE_REQUEST_FIELD: request_id,
        LEASE_WORKER_FIELD: worker_id,
        LEASE_MODEL_FIELD: model,
        LEASE_OWNER_FIELD: owner_id,
        LEASE_KEY_DIGEST_FIELD: api_key_digest(owner_api_key),
        LEASE_MAX_BYTES_FIELD: max_length * CHAT_PROGRESS_BYTES_PER_TOKEN,
        LEASE_FAKE_FIELD: int(fake),
        LEASE_STARTED_FIELD: started_at,
        LEASE_JOB_TTL_FIELD: job_ttl,
        LEASE_CAP_FIELD: lease_cap,
        LEASE_TRUSTED_FIELD: int(trusted),
    }
    try:
        pipeline = _get_client().pipeline(transaction=True)
        pipeline.delete(lease_key)
        pipeline.hset(lease_key, mapping=lease_fields)
        pipeline.expire(lease_key, job_ttl + CHAT_PROGRESS_LEASE_GRACE_SECONDS)
        pipeline.execute()
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to write the stream lease of chat generation {generation_id}: {redis_error}")


def read_stream_lease(generation_id: str) -> StreamLease | None:
    """Return a generation's lease, or None when it has none.

    Args:
        generation_id: The generation a post specifies.

    Returns:
        The lease, or None when the generation is not streamed, finished, faulted, expired or unknown.

    Raises:
        redis.RedisError: When Redis cannot be reached; the route refuses the post rather than guess.
    """
    lease_fields = _get_client().hgetall(chat_lease_key(generation_id))
    if not lease_fields:
        return None
    return StreamLease(
        generation_id=generation_id,
        request_id=lease_fields[LEASE_REQUEST_FIELD],
        worker_id=lease_fields[LEASE_WORKER_FIELD],
        owner_id=int(lease_fields[LEASE_OWNER_FIELD]),
        key_digest=lease_fields[LEASE_KEY_DIGEST_FIELD],
        started_at=float(lease_fields[LEASE_STARTED_FIELD]) if LEASE_STARTED_FIELD in lease_fields else None,
        job_ttl=int(lease_fields[LEASE_JOB_TTL_FIELD]) if LEASE_JOB_TTL_FIELD in lease_fields else None,
        lease_cap=int(lease_fields[LEASE_CAP_FIELD]) if LEASE_CAP_FIELD in lease_fields else None,
        trusted=lease_fields.get(LEASE_TRUSTED_FIELD) == "1",
    )


def extend_stream_lease(generation_id: str, job_ttl: int, expire_seconds: int) -> None:
    """Record an extended job lease on a generation's stream lease, logging rather than raising on a Redis failure.

    The lease is changed only while it exists, since a lease deleted by the generation's submit or fault must not be
    recreated without its other fields.

    Args:
        generation_id: The generation whose job lease was extended.
        job_ttl: The extended ``job_ttl``.
        expire_seconds: Seconds from now until the extended job lease ends. The stream lease outlives it by
            [`CHAT_PROGRESS_LEASE_GRACE_SECONDS`][horde.chat_progress.CHAT_PROGRESS_LEASE_GRACE_SECONDS].
    """
    lease_key = chat_lease_key(generation_id)

    def _extend(pipeline: redis.client.Pipeline) -> None:
        if not pipeline.exists(lease_key):
            return
        pipeline.multi()
        pipeline.hset(lease_key, LEASE_JOB_TTL_FIELD, job_ttl)
        pipeline.expire(lease_key, expire_seconds + CHAT_PROGRESS_LEASE_GRACE_SECONDS)

    try:
        _get_client().transaction(_extend, lease_key)
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to extend the stream lease of chat generation {generation_id}: {redis_error}")


def append_progress(lease: StreamLease, seq: int, content: str, reasoning_content: str = "") -> ProgressAppend:
    """Append one delta of a leased generation to its request's stream, enforcing the sequence and the caps.

    The reasoning counts toward the byte cap with the content, and a retry repeats both.

    Args:
        lease: The generation's lease, read by [`read_stream_lease`][horde.chat_progress.read_stream_lease].
        seq: The delta's sequence number, from 1.
        content: The delta's text; empty when the delta carries only reasoning.
        reasoning_content: The delta's reasoning; empty when it carries none.

    Returns:
        The outcome and its ``seq``.

    Raises:
        redis.RedisError: When Redis cannot be reached.
    """
    reply = _get_append_script()(
        keys=[
            chat_lease_key(lease.generation_id),
            chat_stream_key(lease.request_id),
        ],
        args=[
            lease.generation_id,
            seq,
            content,
            MAX_CHAT_PROGRESS_ENTRIES,
            CHAT_PROGRESS_STREAM_TTL_SECONDS,
            CHAT_PROGRESS_STREAM_MAX_ENTRIES,
            reasoning_content,
        ],
    )
    outcome, reply_seq, *reply_flags = reply
    return ProgressAppend(
        outcome=ProgressOutcome(outcome),
        seq=int(reply_seq),
        cancelled=bool(reply_flags) and int(reply_flags[0]) == 1,
    )


def end_progress_stream(generation_id: str, request_id: str) -> None:
    """Mark a faulted generation's attempt as ended on its stream and delete its lease, when it holds one.

    A generation without a lease was not streamed or already ended, and is left alone. A fake generation's lease is
    deleted without a marker, since its posts were never appended. Logs rather than raises on a Redis failure; the
    lease then expires on its own and a reader learns of the fault from the next attempt or the request's outcome.

    Args:
        generation_id: The faulted generation.
        request_id: The chat request it belongs to.

    Side Effects:
        Appends ``gen`` and ``ended`` to the stream, refreshes its TTL and deletes the lease.
    """
    lease_key = chat_lease_key(generation_id)
    try:
        client = _get_client()
        fake = client.hget(lease_key, LEASE_FAKE_FIELD)
        if fake is None:
            return
        pipeline = client.pipeline(transaction=True)
        if fake != "1":
            stream_key = chat_stream_key(request_id)
            pipeline.xadd(
                stream_key,
                {
                    STREAM_GENERATION_FIELD: generation_id,
                    STREAM_ENDED_FIELD: 1,
                },
                maxlen=CHAT_PROGRESS_STREAM_MAX_ENTRIES,
                approximate=True,
            )
            pipeline.expire(stream_key, CHAT_PROGRESS_STREAM_TTL_SECONDS)
        pipeline.delete(lease_key)
        pipeline.execute()
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to end the delta stream of chat generation {generation_id}: {redis_error}")


def cancel_progress_streams(generation_ids: Sequence[str], request_id: str) -> None:
    """Flag the leases of a cancelled request's in-flight generations and mark each attempt as ended on its stream.

    One call covers every generation of the request, so the flags and markers land in one transaction. A generation
    without a lease finished, faulted or was not streamed, and is left as it is; a lease is flagged only while it
    exists, since setting a field on a deleted lease would recreate it without its fields or TTL. Each flagged lease
    is kept until it is released or expires, so the worker's next post is acknowledged with ``cancelled``. A fake
    generation's lease is flagged without a marker, since its posts were never appended. Logs rather than raises on a
    Redis failure, since a cancel succeeds without Redis; the worker then generates and submits as it would without
    the cancel.

    Args:
        generation_ids: The request's in-flight generations.
        request_id: The cancelled chat request.

    Side Effects:
        Sets ``cancelled`` on each existing lease, and appends ``gen`` and ``ended`` to the stream and refreshes its
        TTL for each that is not fake.
    """
    if not generation_ids:
        return
    lease_keys = [chat_lease_key(generation_id) for generation_id in generation_ids]
    stream_key = chat_stream_key(request_id)

    def flag_live_leases(pipeline: redis.client.Pipeline) -> None:
        # The leases are watched, so a submit or fault that deletes one between this read and the write retries the
        # transaction, and a deleted lease is not recreated.
        fake_flags = [pipeline.hget(lease_key, LEASE_FAKE_FIELD) for lease_key in lease_keys]
        pipeline.multi()
        for generation_id, lease_key, fake in zip(generation_ids, lease_keys, fake_flags, strict=True):
            if fake is None:
                continue
            pipeline.hset(lease_key, LEASE_CANCELLED_FIELD, 1)
            if fake != "1":
                pipeline.xadd(
                    stream_key,
                    {
                        STREAM_GENERATION_FIELD: generation_id,
                        STREAM_ENDED_FIELD: 1,
                    },
                    maxlen=CHAT_PROGRESS_STREAM_MAX_ENTRIES,
                    approximate=True,
                )
                pipeline.expire(stream_key, CHAT_PROGRESS_STREAM_TTL_SECONDS)

    try:
        _get_client().transaction(flag_live_leases, *lease_keys)
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to cancel the delta stream of chat request {request_id}: {redis_error}")


def read_posted_progress(generation_id: str, request_id: str) -> PostedProgress:
    """Return the content a generation posted, read from the request's stream.

    Reasoning is left out, since a generation is paid on its content alone. When the stream holds no entry for the
    generation and its lease still exists, the lease's accepted bytes stand in for the characters. The bytes count
    multibyte characters more than once and include any reasoning, so the fallback leans toward the worker; the stream
    outlives the lease by design, so the fallback applies only when the stream was trimmed or lost.

    Args:
        generation_id: The pending generation of the cancelled request.
        request_id: The chat request whose stream holds the generation's entries.

    Returns:
        The posted content and its character count, or
        [`EMPTY_POSTED_PROGRESS`][horde.chat_progress.EMPTY_POSTED_PROGRESS] when the generation posted nothing or
        Redis could not be reached, which a cancel survives by settling on the formula's floor.
    """
    pipeline = _get_client().pipeline(transaction=False)
    pipeline.xrange(chat_stream_key(request_id))
    pipeline.hget(chat_lease_key(generation_id), LEASE_BYTES_FIELD)
    try:
        stream_entries, lease_bytes = pipeline.execute()
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to read the posted progress of chat generation {generation_id}: {redis_error}")
        return EMPTY_POSTED_PROGRESS
    # Ended markers carry no seq and no content; only delta entries are paid.
    generation_deltas = sorted(
        (
            (int(entry_fields[STREAM_SEQ_FIELD]), entry_fields.get(STREAM_CONTENT_FIELD, ""))
            for _entry_id, entry_fields in stream_entries
            if entry_fields.get(STREAM_GENERATION_FIELD) == generation_id and STREAM_SEQ_FIELD in entry_fields
        ),
    )
    if generation_deltas:
        content = "".join(delta_content for _seq, delta_content in generation_deltas)
        return PostedProgress(content=content, chars=len(content))
    if lease_bytes is None:
        return EMPTY_POSTED_PROGRESS
    return PostedProgress(content="", chars=int(lease_bytes))


def release_stream_lease(generation_id: str) -> None:
    """Delete a finished generation's lease, logging rather than raising on a Redis failure.

    The stream itself stays until it expires, since a reader reconciles the deltas it relayed against the final reply.

    Args:
        generation_id: The generation whose reply was stored.
    """
    try:
        _get_client().delete(chat_lease_key(generation_id))
    except redis.RedisError as redis_error:
        logger.warning(f"Unable to release the stream lease of chat generation {generation_id}: {redis_error}")
