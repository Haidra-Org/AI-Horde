# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Check chat responses against the contract a scenario expects, without any network or Locust state.

A chat scenario states one expected outcome: a [`CompletionExpectation`][tests.stress.locustsuite.chat_checks.CompletionExpectation]
for a reply or an [`ErrorExpectation`][tests.stress.locustsuite.chat_checks.ErrorExpectation] for a refusal. The functions
here compare a response with it and return the violations as sentences, an empty list meaning the response honours the
contract. The Locust users turn a non-empty list into a failure whose message starts with
[`CONTRACT_VIOLATION_PREFIX`][tests.stress.locustsuite.chat_checks.CONTRACT_VIOLATION_PREFIX], which
``check_smoke_results.py`` gates on whatever HTTP status the response had.

- [`StreamTranscript`][tests.stress.locustsuite.chat_checks.StreamTranscript] collects a server-sent event stream line by
  line, with the times the first chunk and the first content arrived.
- [`find_completion_violations`][tests.stress.locustsuite.chat_checks.find_completion_violations] and
  [`find_stream_violations`][tests.stress.locustsuite.chat_checks.find_stream_violations] check a delivered reply.
- [`find_error_violations`][tests.stress.locustsuite.chat_checks.find_error_violations] checks a refusal, before or after
  the stream's headers.
- [`classify_benign_refusal`][tests.stress.locustsuite.chat_checks.classify_benign_refusal] names the operating
  conditions (rate limits, a full front, too little kudos) that are never a contract failure.
- [`find_abandoned_stream_violations`][tests.stress.locustsuite.chat_checks.find_abandoned_stream_violations] checks
  the part of a stream a client read before it closed the connection.
- [`classify_environment_condition`][tests.stress.locustsuite.chat_checks.classify_environment_condition] names the
  conditions of the deployment (admission off, maintenance, an unreachable Horde, a proxy error page) that are no
  contract failure but do mean the run was not clean; they fail under
  [`ENVIRONMENT_CONDITION_PREFIX`][tests.stress.locustsuite.chat_checks.ENVIRONMENT_CONDITION_PREFIX].

The rules follow ``docs/reference/chat_front.md`` (response and stream shape) and ``docs/how-to/send_chat_completions.md``
(errors, retry headers).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import jsonschema

__all__ = [
    "BENIGN_REFUSALS",
    "COMMENT_PREFIX",
    "COMPLETION_ID_PREFIX",
    "CONTRACT_VIOLATION_PREFIX",
    "DATA_FIELD_PREFIX",
    "DONE_MARKER",
    "ENVIRONMENT_CONDITION_PREFIX",
    "ERROR_TYPE_BY_STATUS",
    "QUEUE_UNAVAILABLE_REFUSAL",
    "BenignRefusal",
    "CompletionExpectation",
    "ContentRule",
    "EnvironmentCondition",
    "ErrorExpectation",
    "FrontVerdict",
    "StreamTranscript",
    "classify_benign_refusal",
    "classify_environment_condition",
    "describe_completion",
    "describe_stream",
    "error_code_of",
    "is_keepalive_comment",
    "find_abandoned_stream_violations",
    "find_completion_violations",
    "find_cors_violations",
    "find_error_violations",
    "find_stream_violations",
    "judge_front_response",
    "queue_unavailable_reason",
    "reassemble_stream_message",
]

CONTRACT_VIOLATION_PREFIX: str = "Contract violation"
"""The start of every failure message a chat contract check raises; ``check_smoke_results.py`` gates on it.

LOG CONTRACT: ``tests/stress/check_smoke_results.py`` matches this prefix in Locust's failure CSV. Changing it means
changing the checker's ``CONTRACT_VIOLATION_PREFIX`` in the same commit."""

ENVIRONMENT_CONDITION_PREFIX: str = "Environment condition"
"""The start of every failure message for a deployment condition that is no contract failure but leaves the run unclean.

LOG CONTRACT: ``tests/stress/check_smoke_results.py`` matches this prefix in Locust's failure CSV, reports it in its own
section and gates on it. Changing it means changing the checker's ``ENVIRONMENT_CONDITION_PREFIX`` in the same commit."""

COMPLETION_ID_PREFIX: str = "chatcmpl-"
"""The prefix the front puts before the Horde request id in a completion's ``id``."""

SYSTEM_FINGERPRINT_PREFIX: str = "fp_"
"""The prefix of a non-null ``system_fingerprint``."""

COMPLETION_OBJECT: str = "chat.completion"
"""The ``object`` of a non-streamed completion."""

CHUNK_OBJECT: str = "chat.completion.chunk"
"""The ``object`` of a streamed chunk."""

DONE_MARKER: str = "[DONE]"
"""The data of the last event of every stream."""

DATA_FIELD_PREFIX: str = "data:"
"""The prefix of a server-sent event data line."""

COMMENT_PREFIX: str = ":"
"""The prefix of a server-sent event comment line; the front's keepalives are comments."""

KEEPALIVE_COMMENT_WORD: str = "horde"
"""The first word of the front's keepalive comment, ``: horde queue_position=7 wait_time=42``."""

OUTPUT_ESTIMATE_CHARGE_BASIS: str = "output_estimate"
"""The only ``charge_basis`` a chat generation is charged under."""

SHOULD_RETRY_HEADER: str = "x-should-retry"
"""The header OpenAI SDKs read before retrying; ``false`` once the Horde may hold the request."""

RETRY_AFTER_HEADER: str = "retry-after"
"""The header naming how long to wait before a retry."""

ALLOW_ORIGIN_HEADER: str = "access-control-allow-origin"
"""The CORS header every front response carries for a browser client."""

ANY_ORIGIN: str = "*"
"""The ``Access-Control-Allow-Origin`` value the front sends."""

ERROR_TYPE_BY_STATUS: Mapping[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    408: "timeout_error",
    429: "rate_limit_error",
}
"""The ``error.type`` the front sends for each HTTP status, as ``docs/reference/chat_front.md`` documents it; any other
status is ``api_error``. Kept here rather than imported from ``horde.oai`` so the suite checks the documented contract,
not the implementation against itself."""

OTHER_STATUS_ERROR_TYPE: str = "api_error"
"""The ``error.type`` of every status the table above does not list."""

EXCERPT_CHARACTERS: int = 160
"""How much of an unexpected value a violation quotes."""

QUEUE_UNAVAILABLE_CODE: str = "queue_unavailable"
"""The ``error.code`` of a ``fail_fast`` request the front refused at its first status read."""

AVAILABILITY_MODE_PARAM: str = "horde.availability_mode"
"""The ``error.param`` of a ``queue_unavailable`` refusal."""

QUEUE_UNAVAILABLE_REASON_BY_FRAGMENT: Mapping[str, str] = {
    "zero worker threads eligible": "no_eligible_threads",
    "estimated wait exceeds": "wait_exceeds_budget",
}
"""The refusal reason a ``queue_unavailable`` message states, by a fragment of its wording. The envelope carries no
reason field, so the message is the only place the two causes differ."""


class ContentRule(StrEnum):
    """What a delivered reply's ``message.content`` has to be."""

    TEXT = "text"
    """A non-empty string."""
    NULL = "null"
    """Null: the reply only calls tools."""
    JSON_OBJECT = "json_object"
    """A string holding one JSON object."""
    JSON_SCHEMA = "json_schema"
    """A string holding one JSON document valid against the expectation's schema."""
    ANY = "any"
    """Anything, as a ``best_effort`` reply from a worker that may deviate."""


@dataclass(frozen=True)
class CompletionExpectation:
    """Represents the reply a scenario expects the front to deliver."""

    model: str
    """The model the reply has to name: the one requested, since a request accepts only that model."""
    finish_reasons: frozenset[str]
    """The ``finish_reason`` values the reply may carry."""
    content_rule: ContentRule = ContentRule.TEXT
    """What ``message.content`` has to be."""
    json_schema: Mapping[str, Any] | None = None
    """The schema the content has to satisfy under ``ContentRule.JSON_SCHEMA``."""
    tool_call_names: frozenset[str] = frozenset()
    """The function names a call may name; empty means the reply carries no tool calls."""
    min_tool_calls: int = 0
    """The fewest tool calls the reply carries."""
    max_tool_calls: int = 0
    """The most tool calls the reply carries."""
    nonce: str | None = None
    """The request's nonce, which the simulated worker echoes into every text, JSON document and call argument; a reply
    without it was generated for another request. None skips the check."""
    content_fragment: str | None = None
    """Text the content has to contain, such as the vision worker's marker."""


@dataclass(frozen=True)
class ErrorExpectation:
    """Represents the refusal a scenario expects, before the stream's headers or as an in-stream error event."""

    status: int
    """The HTTP status of a refusal sent before any header; a stream refused after its headers has status 200."""
    codes: frozenset[str]
    """The ``error.code`` values the envelope may carry."""
    after_headers: bool = False
    """True when the error follows the request's first status read, so a stream reports it as an event after 200
    headers; a refusal at that read precedes them."""
    param: str | None = None
    """The ``error.param`` the envelope has to carry; None requires a null ``param``."""
    rc: str | None = None
    """The ``error.metadata.rc`` the envelope has to carry; None does not check it."""
    message_fragments: tuple[str, ...] = ()
    """Text the ``error.message`` has to contain, such as a missing declaration's name."""
    should_not_retry: bool | None = None
    """True requires ``x-should-retry: false``, False requires the header absent, None does not check it."""
    retry_after: bool | None = None
    """True requires a ``Retry-After`` header, False requires none, None does not check it."""


QUEUE_UNAVAILABLE_REFUSAL: ErrorExpectation = ErrorExpectation(
    status=503,
    codes=frozenset({QUEUE_UNAVAILABLE_CODE}),
    param=AVAILABILITY_MODE_PARAM,
    should_not_retry=True,
    retry_after=True,
)
"""The refusal a ``fail_fast`` request may get instead of its scenario's outcome. The front decides at the first status
read, before a stream's headers, so a streamed request gets it as a plain HTTP error too; the Horde created the request
before the read, so the refusal says not to retry."""


class BenignRefusal(StrEnum):
    """An operating condition the front or Horde reports that a contract check never counts against the service."""

    RATE_LIMITED = "rate_limited"
    """429: the Horde's per-key or per-IP rate limit, passed through."""
    FRONT_AT_CAPACITY = "front_at_capacity"
    """503 ``FrontAtCapacity``: the front or this client address holds its maximum of open requests."""
    KUDOS_UPFRONT = "kudos_upfront"
    """403 ``KudosUpfront``: the key has too little kudos for a request this large."""
    SHARED_KEY_INSUFFICIENT = "shared_key_insufficient_kudos"
    """``SharedKeyInsufficientKudos``: a shared key ran out."""
    QUEUE_UNAVAILABLE = "queue_unavailable"
    """503 ``queue_unavailable`` to a request that asked ``horde.availability_mode: fail_fast``; for any other request
    it is a contract violation."""


BENIGN_REFUSALS: Mapping[str, BenignRefusal] = {
    "FrontAtCapacity": BenignRefusal.FRONT_AT_CAPACITY,
    "KudosUpfront": BenignRefusal.KUDOS_UPFRONT,
    "SharedKeyInsufficientKudos": BenignRefusal.SHARED_KEY_INSUFFICIENT,
}
"""Error codes that are an operating condition whatever the scenario expected, by code."""


class EnvironmentCondition(StrEnum):
    """A condition of the deployment that is no contract failure but leaves the run unclean."""

    ADMISSION_DISABLED = "admission_disabled"
    """503 ``ChatAdmissionDisabled``: the Horde accepts no new chat requests."""
    MAINTENANCE = "maintenance"
    """``MaintenanceMode``: the Horde is in maintenance."""
    HORDE_UNAVAILABLE = "horde_unavailable"
    """502 ``HordeUnavailable``: the front reached no Horde root."""
    REQUEST_TIMEOUT = "request_timeout"
    """408 ``ChatRequestTimeout``: the request still waited at the front's deadline."""
    IP_TIMEOUT = "ip_timeout"
    """403 ``TimeoutIP``: the load generator's address is in a timeout."""
    PROXY_ERROR = "proxy_error"
    """A 5xx without an error envelope, such as a proxy's error page in front of the service."""


ENVIRONMENT_CONDITIONS: Mapping[str, EnvironmentCondition] = {
    "ChatAdmissionDisabled": EnvironmentCondition.ADMISSION_DISABLED,
    "MaintenanceMode": EnvironmentCondition.MAINTENANCE,
    "HordeUnavailable": EnvironmentCondition.HORDE_UNAVAILABLE,
    "ChatRequestTimeout": EnvironmentCondition.REQUEST_TIMEOUT,
    "TimeoutIP": EnvironmentCondition.IP_TIMEOUT,
}
"""Error codes that report a deployment condition, by code."""


def classify_environment_condition(status_code: int, error_code: str | None, *, has_envelope: bool) -> EnvironmentCondition | None:
    """Return the deployment condition a response reports, or None when it reports none.

    Args:
        status_code: The HTTP status (200 for an error event after a stream's headers).
        error_code: The envelope's ``error.code`` or the Horde ``rc``, when there is one.
        has_envelope: Whether the body was an error envelope or a Horde error body.
    """
    if error_code is not None and error_code in ENVIRONMENT_CONDITIONS:
        return ENVIRONMENT_CONDITIONS[error_code]
    if status_code >= 500 and not has_envelope:
        return EnvironmentCondition.PROXY_ERROR
    return None


def error_code_of(body: Any) -> str | None:
    """Return a body's ``error.code`` when it is an OpenAI envelope, else its Horde ``rc``, else None."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("code")
    return body.get("rc")


def is_keepalive_comment(line: str) -> bool:
    """Return whether a stream line is the front's keepalive comment, ``: horde queue_position=7 wait_time=42``."""
    if not line.startswith(COMMENT_PREFIX):
        return False
    return line[len(COMMENT_PREFIX) :].strip().split(" ", 1)[0] == KEEPALIVE_COMMENT_WORD


def classify_benign_refusal(status_code: int, error_code: str | None) -> BenignRefusal | None:
    """Return the operating condition a refusal reports, or None when it is not one the suite treats as benign.

    Args:
        status_code: The HTTP status.
        error_code: The envelope's ``error.code`` (or the Horde ``rc``), when there is one.
    """
    if status_code == 429:
        return BenignRefusal.RATE_LIMITED
    if error_code is None:
        return None
    return BENIGN_REFUSALS.get(error_code)


@dataclass
class StreamTranscript:
    """Represents what a client read from one server-sent event stream.

    Feed every line with [`feed_line`][tests.stress.locustsuite.chat_checks.StreamTranscript.feed_line] in arrival order.
    """

    chunks: list[dict[str, Any]] = field(default_factory=list)
    """The JSON chunks before ``[DONE]``, error events excluded."""
    error_envelope: dict[str, Any] | None = None
    """The first ``data: {"error": ...}`` event, when one arrived."""
    keepalive_comments: int = 0
    """How many keepalive comments arrived."""
    foreign_comments: list[str] = field(default_factory=list)
    """Comment lines that are not the front's keepalive."""
    unparsable_data: list[str] = field(default_factory=list)
    """``data:`` lines that are neither JSON objects nor ``[DONE]``."""
    done: bool = False
    """Whether ``[DONE]`` arrived."""
    data_after_done: int = 0
    """How many ``data:`` lines arrived after ``[DONE]``."""
    first_chunk_at: float | None = None
    """When the first chunk or error event arrived."""
    first_content_at: float | None = None
    """When the first chunk carrying content or tool calls arrived."""
    first_keepalive_at: float | None = None
    """When the first keepalive arrived."""
    reply_chunks: int = 0
    """How many chunks carrying content or tool calls arrived."""
    content_characters: int = 0
    """How many characters of ``delta.content`` arrived, the measure the Horde settles a cancelled generation on."""

    def feed_line(self, line: str, received_at: float) -> None:
        """Mutate the transcript with one line of the stream.

        Args:
            line: The line without its line ending; blank lines separate events and are ignored.
            received_at: When the line arrived, on any clock the caller keeps consistent.
        """
        if line.startswith(COMMENT_PREFIX):
            self._feed_comment(line, received_at)
            return
        if not line.startswith(DATA_FIELD_PREFIX):
            return
        data = line[len(DATA_FIELD_PREFIX) :].strip()
        if self.done:
            self.data_after_done += 1
            return
        if data == DONE_MARKER:
            self.done = True
            return
        try:
            event = json.loads(data)
        except ValueError:
            self.unparsable_data.append(data[:EXCERPT_CHARACTERS])
            return
        if not isinstance(event, dict):
            self.unparsable_data.append(data[:EXCERPT_CHARACTERS])
            return
        if self.first_chunk_at is None:
            self.first_chunk_at = received_at
        if "error" in event:
            if self.error_envelope is None:
                self.error_envelope = event
            return
        self.chunks.append(event)
        if not _chunk_carries_reply(event):
            return
        self.reply_chunks += 1
        for choice in event.get("choices") or []:
            self.content_characters += len((choice.get("delta") or {}).get("content") or "")
        if self.first_content_at is None:
            self.first_content_at = received_at

    def _feed_comment(self, line: str, received_at: float) -> None:
        """Mutate the keepalive count, or keep a comment that is not a keepalive."""
        if not is_keepalive_comment(line):
            self.foreign_comments.append(line[len(COMMENT_PREFIX) :].strip()[:EXCERPT_CHARACTERS])
            return
        self.keepalive_comments += 1
        if self.first_keepalive_at is None:
            self.first_keepalive_at = received_at


def _chunk_carries_reply(chunk: Mapping[str, Any]) -> bool:
    """Return whether a chunk carries content or tool calls."""
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content") is not None or delta.get("tool_calls"):
            return True
    return False


def find_completion_violations(body: Any, expectation: CompletionExpectation) -> list[str]:
    """Return how a non-streamed completion body breaks the expected reply; empty when it honours it.

    Args:
        body: The parsed JSON body.
        expectation: The reply the scenario expects.
    """
    if not isinstance(body, dict):
        return [f"the body is not a JSON object: {str(body)[:EXCERPT_CHARACTERS]}"]
    violations: list[str] = []
    if body.get("object") != COMPLETION_OBJECT:
        violations.append(f"object is {body.get('object')!r}, not {COMPLETION_OBJECT!r}")
    violations.extend(_find_identity_violations(body, expectation))
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        violations.append(f"choices holds {len(choices) if isinstance(choices, list) else 'no list'} entries, not exactly 1")
        return violations
    choice = choices[0]
    if choice.get("index") != 0:
        violations.append(f"choices[0].index is {choice.get('index')!r}, not 0")
    message = choice.get("message")
    if not isinstance(message, dict):
        violations.append("choices[0].message is not an object")
        return violations
    if message.get("role") != "assistant":
        violations.append(f"message.role is {message.get('role')!r}, not 'assistant'")
    if "content" not in message:
        violations.append("message.content is missing; the front always writes it")
    violations.extend(_find_finish_reason_violations(choice.get("finish_reason"), expectation))
    violations.extend(_find_reply_violations(message.get("content"), message.get("tool_calls"), expectation))
    violations.extend(_find_usage_violations(body.get("usage")))
    violations.extend(_find_x_horde_violations(body.get("x_horde"), body.get("id")))
    return violations


def find_stream_violations(transcript: StreamTranscript, expectation: CompletionExpectation, *, include_usage: bool) -> list[str]:
    """Return how a delivered stream breaks the documented chunk order or the expected reply; empty when it honours both.

    The order is: a role chunk, a content chunk when there is text, a tool-call chunk when there are calls, a finish
    chunk with ``x_horde``, a usage chunk when asked for, then ``[DONE]``. Keepalive comments may come first.

    Args:
        transcript: What the client read.
        expectation: The reply the scenario expects.
        include_usage: Whether the request set ``stream_options.include_usage``.
    """
    violations = _find_transcript_framing_violations(transcript)
    if transcript.error_envelope is not None:
        violations.append(f"the stream carried an error event: {json.dumps(transcript.error_envelope)[:EXCERPT_CHARACTERS]}")
        return violations
    chunks = transcript.chunks
    if not chunks:
        violations.append("the stream carried no chunk")
        return violations
    first_chunk = chunks[0]
    violations.extend(_find_chunk_consistency_violations(chunks, expectation))
    first_choices = first_chunk.get("choices") or []
    if not first_choices or first_choices[0].get("delta") != {"role": "assistant"}:
        violations.append(
            f"the first chunk's delta is not exactly {{'role': 'assistant'}}: {json.dumps(first_choices)[:EXCERPT_CHARACTERS]}"
        )
    choice_chunks = [chunk for chunk in chunks if chunk.get("choices")]
    usage_chunks = [chunk for chunk in chunks if not chunk.get("choices")]
    finish_chunks = [chunk for chunk in choice_chunks if chunk["choices"][0].get("finish_reason") is not None]
    if len(finish_chunks) != 1:
        violations.append(f"{len(finish_chunks)} chunks carry a finish_reason, not exactly 1")
    else:
        finish_chunk = finish_chunks[0]
        if finish_chunk is not choice_chunks[-1]:
            violations.append("the finish chunk is not the last chunk with choices")
        if finish_chunk["choices"][0].get("delta") != {}:
            violations.append(f"the finish chunk's delta is not empty: {finish_chunk['choices'][0].get('delta')!r}")
        violations.extend(_find_finish_reason_violations(finish_chunk["choices"][0].get("finish_reason"), expectation))
        violations.extend(_find_x_horde_violations(finish_chunk.get("x_horde"), finish_chunk.get("id")))
    for chunk in choice_chunks:
        delta = chunk["choices"][0].get("delta") or {}
        if delta.get("content") is not None and delta.get("tool_calls"):
            violations.append("a chunk carries both content and tool calls; Lite reads calls only from chunks without content")
    violations.extend(_find_streamed_tool_call_violations(choice_chunks))
    violations.extend(_find_stream_usage_violations(chunks, usage_chunks, include_usage=include_usage))
    content, tool_calls = reassemble_stream_message(chunks)
    violations.extend(_find_reply_violations(content, tool_calls, expectation))
    return violations


def reassemble_stream_message(chunks: Sequence[Mapping[str, Any]]) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Return the reply's content and tool calls rebuilt from a stream's deltas, as an OpenAI SDK accumulates them.

    Args:
        chunks: The stream's chunks in arrival order.

    Returns:
        The concatenated content (None when no chunk carried any) and the calls by ``index`` (None when none arrived).
    """
    content_parts: list[str] = []
    calls_by_index: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content") is not None:
                content_parts.append(delta["content"])
            for delta_call in delta.get("tool_calls") or []:
                call_index = delta_call.get("index", len(calls_by_index))
                slot = calls_by_index.setdefault(call_index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                slot["id"] = delta_call.get("id") or slot["id"]
                slot["type"] = delta_call.get("type") or slot["type"]
                delta_function = delta_call.get("function") or {}
                slot["function"]["name"] += delta_function.get("name") or ""
                slot["function"]["arguments"] += delta_function.get("arguments") or ""
    content = "".join(content_parts) if content_parts else None
    tool_calls = [calls_by_index[call_index] for call_index in sorted(calls_by_index)] if calls_by_index else None
    return content, tool_calls


def find_error_violations(
    expectation: ErrorExpectation,
    *,
    status_code: int,
    envelope: Any,
    headers: Mapping[str, str],
    streamed: bool,
) -> list[str]:
    """Return how a refusal breaks the expected one; empty when it matches.

    Args:
        expectation: The refusal the scenario expects.
        status_code: The HTTP status the response had (200 for an error event after a stream's headers).
        envelope: The parsed error body or error event, ``{"error": {...}}``.
        headers: The response headers, looked up case-insensitively by the caller's mapping.
        streamed: Whether the request was streamed.
    """
    violations: list[str] = []
    expected_status = 200 if streamed and expectation.after_headers else expectation.status
    if status_code != expected_status:
        violations.append(f"status is {status_code}, expected {expected_status}")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("error"), dict):
        violations.append(f"the error is not an OpenAI envelope: {str(envelope)[:EXCERPT_CHARACTERS]}")
        return violations
    error = envelope["error"]
    for envelope_key in ("message", "type", "param", "code"):
        if envelope_key not in error:
            violations.append(f"error.{envelope_key} is missing; the envelope writes every key")
    if error.get("code") not in expectation.codes:
        violations.append(f"error.code is {error.get('code')!r}, expected one of {sorted(expectation.codes)}")
    # The type follows the status the error has, or would have had before a stream's headers.
    expected_type = ERROR_TYPE_BY_STATUS.get(expectation.status, OTHER_STATUS_ERROR_TYPE)
    if error.get("type") != expected_type:
        violations.append(f"error.type is {error.get('type')!r}, expected {expected_type!r} for status {expectation.status}")
    if error.get("param") != expectation.param:
        violations.append(f"error.param is {error.get('param')!r}, expected {expectation.param!r}")
    if expectation.rc is not None:
        metadata = error.get("metadata") or {}
        if metadata.get("rc") != expectation.rc:
            violations.append(f"error.metadata.rc is {metadata.get('rc')!r}, expected {expectation.rc!r}")
    message = str(error.get("message") or "")
    for fragment in expectation.message_fragments:
        if fragment not in message:
            violations.append(f"error.message does not mention {fragment!r}: {message[:EXCERPT_CHARACTERS]!r}")
    violations.extend(_find_retry_header_violations(expectation, headers=headers, streamed=streamed))
    return violations


def find_cors_violations(headers: Mapping[str, str]) -> list[str]:
    """Return a violation when a response a browser reads lacks ``Access-Control-Allow-Origin: *``.

    Args:
        headers: The response headers, looked up case-insensitively by the caller's mapping.
    """
    allow_origin = headers.get(ALLOW_ORIGIN_HEADER)
    if allow_origin != ANY_ORIGIN:
        return [f"{ALLOW_ORIGIN_HEADER} is {allow_origin!r}, expected {ANY_ORIGIN!r}"]
    return []


def _find_retry_header_violations(expectation: ErrorExpectation, *, headers: Mapping[str, str], streamed: bool) -> list[str]:
    """Return the retry header violations of a refusal."""
    violations: list[str] = []
    should_retry = headers.get(SHOULD_RETRY_HEADER)
    # A stream's headers go out once the Horde holds the request, so every stream says not to retry.
    requires_no_retry = expectation.should_not_retry is True or (streamed and expectation.after_headers)
    if requires_no_retry and should_retry != "false":
        violations.append(f"{SHOULD_RETRY_HEADER} is {should_retry!r}, expected 'false'")
    if expectation.should_not_retry is False and not (streamed and expectation.after_headers) and should_retry is not None:
        violations.append(f"{SHOULD_RETRY_HEADER} is {should_retry!r} on a refusal before the Horde held the request")
    # Retry-After rides the HTTP response; an in-stream error event has no headers of its own.
    if streamed and expectation.after_headers:
        return violations
    retry_after = headers.get(RETRY_AFTER_HEADER)
    if expectation.retry_after is True and retry_after is None:
        violations.append(f"{RETRY_AFTER_HEADER} is missing")
    if expectation.retry_after is False and retry_after is not None:
        violations.append(f"{RETRY_AFTER_HEADER} is {retry_after!r}, expected none")
    return violations


def _find_identity_violations(completion: Mapping[str, Any], expectation: CompletionExpectation) -> list[str]:
    """Return the ``id``, ``model``, ``created`` and ``system_fingerprint`` violations of a completion or chunk."""
    violations: list[str] = []
    completion_id = completion.get("id")
    if not isinstance(completion_id, str) or not completion_id.startswith(COMPLETION_ID_PREFIX):
        violations.append(f"id {completion_id!r} does not start with {COMPLETION_ID_PREFIX!r}")
    if completion.get("model") != expectation.model:
        violations.append(f"model is {completion.get('model')!r}, expected the requested {expectation.model!r}")
    if not isinstance(completion.get("created"), int):
        violations.append(f"created is {completion.get('created')!r}, not an integer")
    # A served reply always has a worker, so its fingerprint is never null.
    fingerprint = completion.get("system_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.startswith(SYSTEM_FINGERPRINT_PREFIX):
        violations.append(f"system_fingerprint {fingerprint!r} does not start with {SYSTEM_FINGERPRINT_PREFIX!r}")
    return violations


def _find_finish_reason_violations(finish_reason: Any, expectation: CompletionExpectation) -> list[str]:
    """Return a violation when ``finish_reason`` is not one the scenario allows."""
    if finish_reason not in expectation.finish_reasons:
        return [f"finish_reason is {finish_reason!r}, expected one of {sorted(expectation.finish_reasons)}"]
    return []


def _find_reply_violations(content: Any, tool_calls: Any, expectation: CompletionExpectation) -> list[str]:
    """Return the content and tool call violations of a reply."""
    violations = _find_content_violations(content, expectation)
    call_list = tool_calls or []
    if not isinstance(call_list, list):
        return [*violations, f"tool_calls is not a list: {str(tool_calls)[:EXCERPT_CHARACTERS]}"]
    if not expectation.min_tool_calls <= len(call_list) <= expectation.max_tool_calls:
        violations.append(
            f"the reply carries {len(call_list)} tool calls, expected {expectation.min_tool_calls} to {expectation.max_tool_calls}"
        )
    seen_call_ids: set[str] = set()
    for call_index, tool_call in enumerate(call_list):
        violations.extend(_find_tool_call_violations(call_index, tool_call, expectation, seen_call_ids))
    return violations


def _find_content_violations(content: Any, expectation: CompletionExpectation) -> list[str]:
    """Return the violations of ``message.content`` against the expectation's content rule."""
    rule = expectation.content_rule
    if rule == ContentRule.ANY:
        return []
    if rule == ContentRule.NULL:
        return [] if content is None else [f"content is {str(content)[:EXCERPT_CHARACTERS]!r}, expected null for a tool-only reply"]
    if not isinstance(content, str) or not content:
        return [f"content is {content!r}, expected non-empty text"]
    violations: list[str] = []
    if expectation.nonce is not None and expectation.nonce not in content:
        violations.append(
            f"content lacks the request's nonce {expectation.nonce!r}, so it answers another request: {content[:EXCERPT_CHARACTERS]!r}"
        )
    if expectation.content_fragment is not None and expectation.content_fragment not in content:
        violations.append(f"content lacks {expectation.content_fragment!r}: {content[:EXCERPT_CHARACTERS]!r}")
    if rule == ContentRule.TEXT or violations:
        return violations
    try:
        document = json.loads(content)
    except ValueError:
        return [f"content is not JSON: {content[:EXCERPT_CHARACTERS]!r}"]
    if not isinstance(document, dict):
        return [f"content is JSON but not an object: {content[:EXCERPT_CHARACTERS]!r}"]
    if rule == ContentRule.JSON_OBJECT or expectation.json_schema is None:
        return []
    schema_errors = sorted(jsonschema.Draft202012Validator(expectation.json_schema).iter_errors(document), key=str)
    return [f"content breaks the request's schema: {schema_error.message}" for schema_error in schema_errors]


def _find_tool_call_violations(call_index: int, tool_call: Any, expectation: CompletionExpectation, seen_call_ids: set[str]) -> list[str]:
    """Return the violations of one tool call, adding its id to ``seen_call_ids``."""
    if not isinstance(tool_call, dict):
        return [f"tool_calls[{call_index}] is not an object"]
    violations: list[str] = []
    call_id = tool_call.get("id")
    if not isinstance(call_id, str) or not call_id:
        violations.append(f"tool_calls[{call_index}].id is {call_id!r}, not a non-empty string")
    elif call_id in seen_call_ids:
        violations.append(f"tool_calls[{call_index}].id {call_id!r} repeats an earlier call's id")
    else:
        seen_call_ids.add(call_id)
    if tool_call.get("type") != "function":
        violations.append(f"tool_calls[{call_index}].type is {tool_call.get('type')!r}, not 'function'")
    function = tool_call.get("function") or {}
    if function.get("name") not in expectation.tool_call_names:
        violations.append(f"tool_calls[{call_index}] calls {function.get('name')!r}, expected one of {sorted(expectation.tool_call_names)}")
    try:
        arguments = json.loads(function.get("arguments") or "")
    except ValueError:
        return [
            *violations,
            f"tool_calls[{call_index}].function.arguments is not JSON: {str(function.get('arguments'))[:EXCERPT_CHARACTERS]!r}",
        ]
    if not isinstance(arguments, dict):
        violations.append(f"tool_calls[{call_index}].function.arguments is not a JSON object")
    if expectation.nonce is not None and expectation.nonce not in str(function.get("arguments")):
        violations.append(
            f"tool_calls[{call_index}] arguments lack the request's nonce {expectation.nonce!r}, so they answer another request"
        )
    return violations


def _find_usage_violations(usage: Any) -> list[str]:
    """Return the violations of a ``usage`` object: three non-negative integers whose total adds up."""
    if not isinstance(usage, dict):
        return [f"usage is {usage!r}, not an object"]
    counts = {count_key: usage.get(count_key) for count_key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    bad_counts = [count_key for count_key, count in counts.items() if not isinstance(count, int) or count < 0]
    if bad_counts:
        return [f"usage counts {bad_counts} are not non-negative integers: {usage!r}"]
    if counts["total_tokens"] != counts["prompt_tokens"] + counts["completion_tokens"]:
        return [f"usage.total_tokens {counts['total_tokens']} is not prompt_tokens plus completion_tokens: {usage!r}"]
    return []


def _find_x_horde_violations(x_horde: Any, completion_id: Any) -> list[str]:
    """Return the violations of the ``x_horde`` details: the request id behind ``id``, the kudos and the charge basis."""
    if not isinstance(x_horde, dict):
        return [f"x_horde is {x_horde!r}, not an object"]
    violations: list[str] = []
    if isinstance(completion_id, str) and x_horde.get("request_id") != completion_id.removeprefix(COMPLETION_ID_PREFIX):
        violations.append(f"x_horde.request_id {x_horde.get('request_id')!r} is not the request id inside id {completion_id!r}")
    kudos = x_horde.get("kudos")
    if isinstance(kudos, bool) or not isinstance(kudos, int | float) or kudos < 0:
        violations.append(f"x_horde.kudos is {kudos!r}, not a non-negative number")
    if not isinstance(x_horde.get("estimated_usage"), bool):
        violations.append(f"x_horde.estimated_usage is {x_horde.get('estimated_usage')!r}, not a boolean")
    if "charge_basis" in x_horde and x_horde["charge_basis"] != OUTPUT_ESTIMATE_CHARGE_BASIS:
        violations.append(f"x_horde.charge_basis is {x_horde['charge_basis']!r}, not {OUTPUT_ESTIMATE_CHARGE_BASIS!r}")
    queue_wait = x_horde.get("queue_wait_seconds")
    if queue_wait is not None and (isinstance(queue_wait, bool) or not isinstance(queue_wait, int | float) or queue_wait < 0):
        violations.append(f"x_horde.queue_wait_seconds is {queue_wait!r}, not a non-negative number")
    return violations


def find_abandoned_stream_violations(transcript: StreamTranscript, expectation: CompletionExpectation) -> list[str]:
    """Return how the part of a stream a client read before closing the connection breaks the chunk contract; empty
    when every line so far was well formed.

    A stream closed by its client has no finish chunk, usage chunk or ``[DONE]``, so only what precedes them is checked:
    the framing of each line, the role chunk first, the values every chunk shares, and no chunk carrying both content
    and tool calls.

    Args:
        transcript: What the client read before it closed the connection.
        expectation: The reply the scenario expects; only its model is compared.
    """
    violations = _find_line_framing_violations(transcript)
    if transcript.error_envelope is not None:
        violations.append(f"the stream carried an error event: {json.dumps(transcript.error_envelope)[:EXCERPT_CHARACTERS]}")
        return violations
    chunks = transcript.chunks
    if not chunks:
        violations.append("the stream carried no chunk before the client closed it")
        return violations
    violations.extend(_find_chunk_consistency_violations(chunks, expectation))
    first_choices = chunks[0].get("choices") or []
    if not first_choices or first_choices[0].get("delta") != {"role": "assistant"}:
        violations.append(
            f"the first chunk's delta is not exactly {{'role': 'assistant'}}: {json.dumps(first_choices)[:EXCERPT_CHARACTERS]}"
        )
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content") is not None and delta.get("tool_calls"):
                violations.append("a chunk carries both content and tool calls; Lite reads calls only from chunks without content")
    return violations


def _find_transcript_framing_violations(transcript: StreamTranscript) -> list[str]:
    """Return the framing violations of a stream: ``[DONE]``, unparsable data, foreign comments."""
    violations: list[str] = []
    if not transcript.done:
        violations.append("the stream ended without [DONE]")
    violations.extend(_find_line_framing_violations(transcript))
    return violations


def _find_line_framing_violations(transcript: StreamTranscript) -> list[str]:
    """Return the framing violations of the lines a stream carried: data after ``[DONE]``, unparsable data, foreign
    comments."""
    violations: list[str] = []
    if transcript.data_after_done:
        violations.append(f"{transcript.data_after_done} data lines arrived after [DONE]")
    if transcript.unparsable_data:
        violations.append(f"data lines that are not JSON objects: {transcript.unparsable_data[:3]}")
    if transcript.foreign_comments:
        violations.append(f"comment lines that are not keepalives: {transcript.foreign_comments[:3]}")
    return violations


def _find_chunk_consistency_violations(chunks: Sequence[Mapping[str, Any]], expectation: CompletionExpectation) -> list[str]:
    """Return the violations of the values every chunk shares: ``object``, ``id``, ``model`` and ``system_fingerprint``."""
    violations = [
        f"chunk {chunk_index} object is {chunk.get('object')!r}"
        for chunk_index, chunk in enumerate(chunks)
        if chunk.get("object") != CHUNK_OBJECT
    ]
    violations.extend(_find_identity_violations(chunks[0], expectation))
    for shared_key in ("id", "model", "system_fingerprint", "created"):
        distinct_values = {json.dumps(chunk.get(shared_key)) for chunk in chunks}
        if len(distinct_values) > 1:
            violations.append(f"chunks disagree on {shared_key}: {sorted(distinct_values)[:3]}")
    return violations


def _find_streamed_tool_call_violations(choice_chunks: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the violations of streamed tool calls: each call whole, with consecutive 0-based ``index`` values."""
    violations: list[str] = []
    call_indexes: list[Any] = []
    for chunk in choice_chunks:
        for delta_call in chunk["choices"][0].get("delta", {}).get("tool_calls") or []:
            call_indexes.append(delta_call.get("index"))
            function = delta_call.get("function") or {}
            if not delta_call.get("id") or not function.get("name") or function.get("arguments") is None:
                violations.append(f"a streamed tool call is not whole: {json.dumps(delta_call)[:EXCERPT_CHARACTERS]}")
    if call_indexes and call_indexes != list(range(len(call_indexes))):
        violations.append(f"streamed tool call indexes are {call_indexes}, not 0-based and consecutive")
    return violations


def _find_stream_usage_violations(
    chunks: Sequence[Mapping[str, Any]],
    usage_chunks: Sequence[Mapping[str, Any]],
    *,
    include_usage: bool,
) -> list[str]:
    """Return the violations of a stream's usage: one final chunk with ``choices: []`` exactly when asked for."""
    if not include_usage:
        carrying_usage = [chunk_index for chunk_index, chunk in enumerate(chunks) if "usage" in chunk]
        violations = [f"chunks {carrying_usage} carry usage without stream_options.include_usage"] if carrying_usage else []
        if usage_chunks:
            violations.append(f"{len(usage_chunks)} chunks without choices arrived without stream_options.include_usage")
        return violations
    if len(usage_chunks) != 1 or usage_chunks[0] is not chunks[-1]:
        return [f"{len(usage_chunks)} usage chunks arrived, expected exactly 1 as the last chunk"]
    violations = _find_usage_violations(usage_chunks[0].get("usage"))
    early_usage = [chunk_index for chunk_index, chunk in enumerate(chunks[:-1]) if chunk.get("usage") is not None]
    if early_usage:
        violations.append(f"chunks {early_usage} carry usage before the usage chunk")
    return violations


@dataclass(frozen=True)
class FrontVerdict:
    """Represents the judgement of one front response against a scenario's expected outcome."""

    violations: list[str]
    """How the response breaks the contract; empty when it honours it."""
    benign_refusal: BenignRefusal | None = None
    """The operating condition the response reported instead of an outcome, when it did."""
    matched_error: ErrorExpectation | None = None
    """The expected refusal the response matched, when it was one."""
    error_envelope: dict[str, Any] | None = None
    """The error envelope the response delivered, before the headers or in the stream."""
    environment_condition: EnvironmentCondition | None = None
    """The deployment condition the response reported instead of an outcome, when it did."""


def judge_front_response(
    expectation: CompletionExpectation | ErrorExpectation,
    alternative_error: ErrorExpectation | None,
    *,
    status_code: int,
    headers: Mapping[str, str],
    body: Any,
    transcript: StreamTranscript | None,
    streamed: bool,
    include_usage: bool,
    cors_expected: bool,
    nonce: str | None = None,
    kudos_upfront_benign: bool = True,
    fail_fast: bool = False,
) -> FrontVerdict:
    """Return the verdict on one front response: a deployment condition, a benign operating condition, the expected
    outcome, or violations.

    Args:
        expectation: The outcome the scenario expects.
        alternative_error: A second refusal the contract allows, or None.
        status_code: The HTTP status.
        headers: The response headers, looked up case-insensitively by the caller's mapping.
        body: The parsed JSON body of a response that was not a 200 stream, or None.
        transcript: What the client read from a 200 stream, or None.
        streamed: Whether the request was streamed.
        include_usage: Whether a streamed request set ``stream_options.include_usage``.
        cors_expected: Whether the request carried an ``Origin``, so the response has to carry the CORS header.
        nonce: The request's nonce, used to say whether a reply that should not have arrived answers this request.
        kudos_upfront_benign: Whether a 403 ``KudosUpfront`` is an operating condition for this request; False for a
            request too small for the Horde to ask upfront kudos.
        fail_fast: Whether the request asked ``horde.availability_mode: fail_fast``, so a ``queue_unavailable`` refusal
            of the shape ``QUEUE_UNAVAILABLE_REFUSAL`` describes is an operating condition.
    """
    violations = find_cors_violations(headers) if cors_expected else []
    stream_started = streamed and status_code == 200
    if stream_started and headers.get(SHOULD_RETRY_HEADER) != "false":
        violations.append(f"a stream's headers carry {SHOULD_RETRY_HEADER} {headers.get(SHOULD_RETRY_HEADER)!r}, expected 'false'")
    error_envelope = _delivered_error_envelope(status_code=status_code, body=body, transcript=transcript, stream_started=stream_started)
    if error_envelope is None and status_code != 200:
        benign_refusal = classify_benign_refusal(status_code, None)
        if benign_refusal is not None:
            return FrontVerdict(violations=violations, benign_refusal=benign_refusal)
        environment_condition = classify_environment_condition(status_code, error_code_of(body), has_envelope=isinstance(body, dict))
        if environment_condition is not None:
            return FrontVerdict(violations=violations, environment_condition=environment_condition)
        return FrontVerdict(violations=[*violations, f"status {status_code} without an error envelope: {str(body)[:EXCERPT_CHARACTERS]}"])
    if error_envelope is not None:
        return _judge_error(
            expectation,
            alternative_error,
            violations,
            kudos_upfront_benign=kudos_upfront_benign,
            status_code=status_code,
            headers=headers,
            envelope=error_envelope,
            transcript=transcript,
            streamed=streamed,
            fail_fast=fail_fast,
        )
    if isinstance(expectation, ErrorExpectation):
        delivered = (
            describe_stream(transcript, nonce=nonce)
            if stream_started and transcript is not None
            else describe_completion(body, nonce=nonce)
        )
        return FrontVerdict(violations=[*violations, f"expected {expectation.status} {sorted(expectation.codes)}, got {delivered}"])
    if stream_started and transcript is not None:
        return FrontVerdict(violations=[*violations, *find_stream_violations(transcript, expectation, include_usage=include_usage)])
    return FrontVerdict(violations=[*violations, *find_completion_violations(body, expectation)])


def _delivered_error_envelope(
    *, status_code: int, body: Any, transcript: StreamTranscript | None, stream_started: bool
) -> dict[str, Any] | None:
    """Return the error envelope a response delivered, from the stream once it started or from the body otherwise."""
    if stream_started:
        return transcript.error_envelope if transcript is not None else None
    if status_code != 200 and isinstance(body, dict) and isinstance(body.get("error"), dict):
        return body
    return None


def _judge_error(
    expectation: CompletionExpectation | ErrorExpectation,
    alternative_error: ErrorExpectation | None,
    violations: list[str],
    *,
    status_code: int,
    headers: Mapping[str, str],
    envelope: dict[str, Any],
    transcript: StreamTranscript | None,
    streamed: bool,
    kudos_upfront_benign: bool,
    fail_fast: bool,
) -> FrontVerdict:
    """Return the verdict on a delivered error: benign, one of the allowed refusals, or a violation."""
    error_code = envelope["error"].get("code")
    environment_condition = classify_environment_condition(status_code, error_code, has_envelope=True)
    if environment_condition is not None:
        return FrontVerdict(violations=violations, environment_condition=environment_condition, error_envelope=envelope)
    benign_refusal = classify_benign_refusal(status_code, error_code)
    if benign_refusal == BenignRefusal.KUDOS_UPFRONT and not kudos_upfront_benign:
        refusal = "a request too small for upfront kudos was refused with KudosUpfront"
        return FrontVerdict(violations=[*violations, refusal], error_envelope=envelope)
    if benign_refusal is not None:
        return FrontVerdict(violations=violations, benign_refusal=benign_refusal, error_envelope=envelope)
    if fail_fast and error_code == QUEUE_UNAVAILABLE_CODE:
        refusal_violations = find_error_violations(
            QUEUE_UNAVAILABLE_REFUSAL, status_code=status_code, envelope=envelope, headers=headers, streamed=streamed
        )
        if refusal_violations:
            return FrontVerdict(violations=[*violations, *refusal_violations], error_envelope=envelope)
        return FrontVerdict(violations=violations, benign_refusal=BenignRefusal.QUEUE_UNAVAILABLE, error_envelope=envelope)
    allowed_errors = [candidate for candidate in (expectation, alternative_error) if isinstance(candidate, ErrorExpectation)]
    if not allowed_errors:
        message = str(envelope["error"].get("message") or "")[:EXCERPT_CHARACTERS]
        return FrontVerdict(
            violations=[*violations, f"expected a completion, got {status_code} {error_code}: {message}"], error_envelope=envelope
        )
    framing_violations = _find_transcript_framing_violations(transcript) if transcript is not None and status_code == 200 else []
    first_candidate_violations: list[str] = []
    for allowed_error in allowed_errors:
        candidate_violations = find_error_violations(
            allowed_error, status_code=status_code, envelope=envelope, headers=headers, streamed=streamed
        )
        if not candidate_violations:
            return FrontVerdict(violations=[*violations, *framing_violations], matched_error=allowed_error, error_envelope=envelope)
        first_candidate_violations = first_candidate_violations or candidate_violations
    return FrontVerdict(violations=[*violations, *framing_violations, *first_candidate_violations], error_envelope=envelope)


def queue_unavailable_reason(envelope: Mapping[str, Any] | None) -> str | None:
    """Return the reason a ``queue_unavailable`` refusal states, ``no_eligible_threads`` or ``wait_exceeds_budget``, or
    None when its message states neither.

    Args:
        envelope: The refusal's error envelope, ``{"error": {...}}``.
    """
    error = (envelope or {}).get("error")
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    for fragment, reason in QUEUE_UNAVAILABLE_REASON_BY_FRAGMENT.items():
        if fragment in message:
            return reason
    return None


def describe_completion(body: Any, *, nonce: str | None) -> str:
    """Return a short description of a delivered completion body, for a violation message: its request id, finish
    reason and whether it answers the request with ``nonce``."""
    if not isinstance(body, dict):
        return f"a 200 body that is not a completion: {str(body)[:EXCERPT_CHARACTERS]}"
    choices = body.get("choices") or [{}]
    message = choices[0].get("message") or {}
    return _describe_reply(
        body.get("id"), choices[0].get("finish_reason"), f"{message.get('content')} {message.get('tool_calls')}", nonce=nonce
    )


def describe_stream(transcript: StreamTranscript, *, nonce: str | None) -> str:
    """Return a short description of what a stream delivered instead of an error event, for a violation message: an
    empty or truncated stream reads differently from a completion, and a completion says whose request it answers."""
    if not transcript.chunks:
        return f"a stream with no chunk and no error event ([DONE] {'arrived' if transcript.done else 'missing'})"
    content, tool_calls = reassemble_stream_message(transcript.chunks)
    finish_reasons = [chunk["choices"][0].get("finish_reason") for chunk in transcript.chunks if chunk.get("choices")]
    finish_reason = next((reason for reason in finish_reasons if reason is not None), None)
    described = _describe_reply(transcript.chunks[0].get("id"), finish_reason, f"{content} {tool_calls}", nonce=nonce)
    return f"{described}, streamed in {len(transcript.chunks)} chunks ([DONE] {'arrived' if transcript.done else 'missing'})"


def _describe_reply(completion_id: Any, finish_reason: Any, reply_text: str, *, nonce: str | None) -> str:
    """Return the description of a delivered reply shared by bodies and streams."""
    if nonce is None:
        answers = "unknown"
    else:
        answers = "this request" if nonce in reply_text else "another request"
    return f"a completion {completion_id} (finish_reason {finish_reason!r}) answering {answers}: {reply_text[:EXCERPT_CHARACTERS]!r}"
