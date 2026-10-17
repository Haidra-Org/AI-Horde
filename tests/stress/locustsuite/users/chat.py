# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust users for the chat front scale demo.

Four populations drive the OpenAI-compatible front (``python -m horde.oai``) and the Horde instances behind it:

- ``ChatWorkerSimulator`` pops ``/api/v2/generate/text/pop`` with a chat declaration (``chat_jobs``,
  ``chat_features``, ``chat_limits``, ``model_id``) straight from a Horde instance and submits each chat job after a
  simulated generation time as a structured reply (``message``, ``finish_reason``, token counts, ``model_id``). The
  reply carries the wall-clock time the submit was sent, so a client can tell how long the front took to wake. With
  ``--chat-worker-progress`` it also posts the reply as content deltas to ``/api/v2/generate/text/progress`` while it
  generates a job whose payload carries ``stream_progress``.
- ``ChatStreamClient`` posts ``stream: true`` requests to the front and reads the server-sent events to ``[DONE]``,
  timing the first keepalive, the content chunk and the whole response.
- ``ChatClient`` posts non-streamed requests and blocks for the JSON body.
- ``ChatImpatientClient`` disconnects after a short client-side timeout, streamed or not, so the front's cancel path
  runs under load.

Every client request is appended to a JSONL evidence file through ``chat_evidence_recorder``;
``tests/stress/chat_scale_demo.py summarize`` joins it with the front's log to derive the wake latency and the usage
mix. Expected refusals (408 for a non-streamed timeout, 429 for a Horde rate limit or concurrency cap, 503 for a full
front) count as successes and are recorded under their own names, so only 5xx, transport errors and malformed
streams fail a run.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import random
import re
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import gevent.lock
import requests
from locust import HttpUser, between, tag, task
from locust.env import Environment
from locust.exception import RescheduleTask

from tests.integration._fake_chat_worker import build_chat_submit_body, build_pop_body

from ..chat_checks import COMMENT_PREFIX, COMPLETION_ID_PREFIX, DATA_FIELD_PREFIX, DONE_MARKER, error_code_of, is_keepalive_comment
from ..chat_wire import (
    CHAT_COMPLETIONS_PATH,
    CONNECT_TIMEOUT_SECONDS,
    STREAM_READ_TIMEOUT_SECONDS,
    TEXT_POP_PATH,
    TEXT_PROGRESS_PATH,
    TEXT_SUBMIT_PATH,
    WORKER_BRIDGE_AGENT,
)
from ..config import _EXPECTED_RC_RECOVER, _config
from ..helpers import _headers, _is_expected_rc, _is_too_many_workers, _random_worker_name, _record_expected, _safe_json

logger = logging.getLogger(__name__)

STREAM_REQUEST_NAME: str = "/v1/chat/completions [stream]"
"""The statistics name of a streamed chat request."""

NONSTREAM_REQUEST_NAME: str = "/v1/chat/completions [non-stream]"
"""The statistics name of a non-streamed chat request."""

IMPATIENT_REQUEST_NAME: str = "/v1/chat/completions [impatient]"
"""The statistics name of a request the client abandons after its timeout."""

EVENT_STREAM_REQUEST_TYPE: str = "SSE"
"""The request type the stream timings are recorded under, apart from the POST that opened the stream."""

WORKER_SUBMIT_MARKER: str = "stress-chat-submitted-at="
"""The text a simulated worker writes before the Unix time it sent the submit at."""

WORKER_SUBMIT_PATTERN: re.Pattern[str] = re.compile(re.escape(WORKER_SUBMIT_MARKER) + r"(\d+(?:\.\d+)?)")
"""Find the worker's submit time in a completion's content."""

EXPECTED_REFUSAL_STATUSES: frozenset[int] = frozenset({408, 429, 503})
"""Front statuses that are an operating condition under load, not a defect: a non-streamed timeout, a Horde rate
limit or concurrency cap passed through, and a front at its in-flight limit."""

EXPECTED_REFUSAL_RCS: frozenset[str] = frozenset({"KudosUpfront", "SharedKeyInsufficientKudos"})
"""Horde return codes a 400 may carry under load that the text requester also treats as expected."""

NONSTREAM_READ_MARGIN_SECONDS: float = 30.0
"""How much longer than the front's non-streamed timeout a client waits, so the front's 408 arrives first."""

DEFAULT_CHAT_MODEL: str = "koboldcpp/LLaMA2-13B-Tiefighter"
"""The model clients request and simulated workers serve unless the run names another."""

WORKER_MAX_LENGTH: int = 512
"""The most tokens a simulated worker accepts per job; above the clients' ``max_tokens`` so every job fits."""

WORKER_MAX_CONTEXT_LENGTH: int = 4096
"""The context a simulated worker advertises; chat requests grow their context to fit the messages."""

CHARACTERS_PER_TOKEN: int = 4
"""Characters per token in the simulated workers' reported counts; only the counts' presence matters to the front."""

WORKER_GENERATION_TEXT: str = "Stress chat reply from a simulated worker."
"""The text every simulated worker returns, before the submit-time marker."""

STREAM_PROGRESS_FIELD: str = "stream_progress"
"""The chat job payload field that is true when the client streams, so the worker may post content deltas."""

PROGRESS_ACCEPTED_STATUS: int = 200
"""The progress route's status for an accepted delta."""

PROGRESS_STOP_STATUSES: frozenset[int] = frozenset({403, 404, 409, 413})
"""Progress statuses after which a job does not post more deltas and still submits: the key does not own the lease, the
generation has no lease, the ``seq`` is out of sequence, or the generation reached its cap. The simulator does not
resync after a 409."""

PROGRESS_EXPECTED_STATUSES: frozenset[int] = frozenset({PROGRESS_ACCEPTED_STATUS, 404, 413, 429, 503})
"""Progress statuses that are an operating condition rather than a defect: accepted, a lease that ended before the
post (the client left), the cap, the rate limit, and Redis unavailable. A 403 or 409 means the simulator and the Horde
disagree about the stream, so it fails the request."""

PROGRESS_READ_TIMEOUT_SECONDS: float = 10.0
"""How long a progress post waits for its answer; a stalled post delays the posts after it and the submit."""

SYSTEM_MESSAGE: str = "You are a terse assistant."
"""The system message every client sends."""

USER_MESSAGE_WORDS: tuple[str, ...] = (
    "lighthouse",
    "harbor",
    "orchard",
    "glacier",
    "lantern",
    "meadow",
    "canyon",
    "violin",
    "compass",
    "quarry",
)
"""Words a client builds its user message from; a random suffix keeps every request distinct."""

CLIENT_THINK_MIN_SECONDS: float = 0.2
"""The shortest pause between two requests of one client; short, so each client keeps a request open."""

CLIENT_THINK_MAX_SECONDS: float = 1.0
"""The longest pause between two requests of one client."""

WORKER_POLL_MIN_SECONDS: float = 0.5
"""The shortest pause between two pops of one simulated worker, about what a real worker idles between pops."""

WORKER_POLL_MAX_SECONDS: float = 1.5
"""The longest pause between two pops of one simulated worker."""

RATE_LIMIT_BACKOFF_MIN_SECONDS: float = 2.0
"""The shortest back-off after a 429, as the text requester uses."""

RATE_LIMIT_BACKOFF_MAX_SECONDS: float = 6.0
"""The longest back-off after a 429."""

TRANSPORT_ERROR_BACKOFF_MIN_SECONDS: float = 2.0
"""The shortest wait after a refused or failed connection before the client's next request."""

TRANSPORT_ERROR_BACKOFF_MAX_SECONDS: float = 6.0
"""The longest wait after a refused or failed connection. OpenAI's SDKs back off before retrying, and a client that
reconnects at once turns a full listen backlog into a refusal storm that never drains."""

RESPONSE_EXCERPT_CHARACTERS: int = 200
"""How much of an unexpected response body a failure message quotes."""


class ChatClientKind(StrEnum):
    """Which client population sent a request."""

    STREAM = "stream"
    NONSTREAM = "nonstream"
    IMPATIENT_STREAM = "impatient_stream"
    IMPATIENT_NONSTREAM = "impatient_nonstream"


class ChatOutcome(StrEnum):
    """How a client request ended."""

    COMPLETED = "completed"
    """The completion arrived (and, when streamed, ``[DONE]`` followed)."""
    REFUSED = "refused"
    """The front answered with an error status before any completion."""
    ERROR_CHUNK = "error_chunk"
    """A stream delivered an error chunk instead of the completion."""
    TRUNCATED = "truncated"
    """A stream ended without ``[DONE]``."""
    DISCONNECTED = "disconnected"
    """The client gave up at its timeout, as the impatient client does on purpose."""
    TRANSPORT_ERROR = "transport_error"
    """The connection failed or stalled without the client choosing to leave."""


@dataclasses.dataclass
class ChatRequestRecord:
    """Represents one client request in the evidence file. Times are Unix seconds on the load generator's clock."""

    client: ChatClientKind
    """The population that sent the request."""
    sent_at: float
    """When the request was sent."""
    kind: str = "chat_request"
    """The evidence record type."""
    request_id: str | None = None
    """The Horde request id, from the completion's ``id``; None when no completion arrived."""
    status: int | None = None
    """The front's HTTP status; None on a transport error."""
    error_code: str | None = None
    """The OpenAI error envelope's ``code`` for a refusal or error chunk."""
    first_heartbeat_at: float | None = None
    """When the first keepalive comment arrived; None when the content came first or the request was not streamed."""
    heartbeats: int = 0
    """How many keepalive comments arrived."""
    content_at: float | None = None
    """When the completion content arrived."""
    finished_at: float | None = None
    """When the response ended: ``[DONE]``, the JSON body, the error, or the client's disconnect."""
    worker_submitted_at: float | None = None
    """When the simulated worker sent its submit, read from the content; None for content from another worker."""
    estimated_usage: bool | None = None
    """``x_horde.estimated_usage``: True when the front estimated the token counts from text length."""
    usage_present: bool = False
    """Whether a ``usage`` object arrived (always for non-streamed, with ``include_usage`` for streamed)."""
    finish_reason: str | None = None
    """The first choice's ``finish_reason``."""
    outcome: ChatOutcome = ChatOutcome.TRANSPORT_ERROR
    """How the request ended."""


class ChatScenarioSettings:
    """Hold the scenario settings populated at test start from the chat arguments."""

    def __init__(self) -> None:
        """Create the settings with the defaults the arguments also use."""
        self.front_host: str = ""
        """The front's base URL, without a trailing slash."""
        self.horde_urls: list[str] = []
        """The Horde instances' base URLs (without ``/api``) the simulated workers pop from, round-robin."""
        self.model: str = DEFAULT_CHAT_MODEL
        """The model clients request and workers serve."""
        self.max_tokens: int = 64
        """The ``max_tokens`` every client request carries."""
        self.include_usage: bool = True
        """Whether streamed requests ask for the usage chunk."""
        self.impatient_timeout_seconds: float = 2.0
        """How long an impatient client waits before disconnecting."""
        self.impatient_stream_chance: float = 0.5
        """The chance an impatient request is streamed rather than non-streamed."""
        self.nonstream_timeout_seconds: float = 300.0
        """The front's non-streamed timeout; the non-streamed client waits a margin longer."""
        self.gen_time_min_seconds: float = 3.0
        """The shortest simulated generation time."""
        self.gen_time_max_seconds: float = 8.0
        """The longest simulated generation time."""
        self.unreported_usage_chance: float = 0.1
        """The chance a simulated worker leaves out its token counts, so the front's estimate path also runs."""
        self.worker_progress: bool = False
        """Whether a simulated worker posts content deltas for a job whose payload carries ``stream_progress``."""
        self.worker_progress_interval_seconds: float = 1.0
        """The time between two progress posts of one job."""
        self._horde_url_cycle: itertools.cycle[str] | None = None
        self._requestor_key_counter: itertools.count[int] = itertools.count()
        self._worker_key_counter: itertools.count[int] = itertools.count()

    def configure(self, options: Any, *, fallback_horde_url: str) -> None:
        """Mutate the settings from Locust's parsed options.

        Args:
            options: ``environment.parsed_options``.
            fallback_horde_url: The Horde the workers pop from when ``--chat-horde-urls`` is empty.

        Raises:
            ValueError: ``--chat-worker-progress-interval`` is not positive.
        """
        self.front_host = options.chat_front_host.rstrip("/")
        self.horde_urls = [horde_url.strip().rstrip("/") for horde_url in options.chat_horde_urls.split(",") if horde_url.strip()]
        if not self.horde_urls:
            self.horde_urls = [fallback_horde_url.rstrip("/")]
        self.model = options.chat_model
        self.max_tokens = options.chat_max_tokens
        self.include_usage = not options.chat_no_include_usage
        self.impatient_timeout_seconds = options.chat_impatient_timeout
        self.impatient_stream_chance = options.chat_impatient_stream_chance
        self.nonstream_timeout_seconds = options.chat_nonstream_timeout
        self.gen_time_min_seconds = options.sim_gen_time_min
        self.gen_time_max_seconds = options.sim_gen_time_max
        self.unreported_usage_chance = options.chat_unreported_usage_chance
        if options.chat_worker_progress_interval <= 0:
            raise ValueError(f"--chat-worker-progress-interval must be positive, got {options.chat_worker_progress_interval}")
        self.worker_progress = options.chat_worker_progress
        self.worker_progress_interval_seconds = options.chat_worker_progress_interval
        self._horde_url_cycle = itertools.cycle(self.horde_urls)

    def next_horde_url(self) -> str:
        """Return the next Horde instance in round-robin order."""
        if self._horde_url_cycle is None:
            raise RuntimeError("The chat scenario settings were read before test start configured them.")
        return next(self._horde_url_cycle)

    def next_requestor_key(self) -> str:
        """Return the next requestor key in round-robin order.

        Round-robin spreads clients evenly across keys, so no key passes the Horde's per-user concurrency cap before
        the others; the anonymous key is used when the run has none.
        """
        return _pick_round_robin(_config.get("requestor_api_keys", []), self._requestor_key_counter)

    def next_worker_key(self) -> str:
        """Return the next worker key in round-robin order, which keeps each key under the three-worker cap."""
        return _pick_round_robin(_config.get("worker_api_keys", []), self._worker_key_counter)


def count_progress_deltas(*, generation_seconds: float, interval_seconds: float) -> int:
    """Return how many deltas a generation posts: one per whole interval of its generation time, and at least one.

    Args:
        generation_seconds: The simulated generation time.
        interval_seconds: The time between two progress posts; positive.
    """
    return max(1, int(generation_seconds // interval_seconds))


def split_reply_into_deltas(reply_text: str, delta_count: int) -> list[str]:
    """Return the reply split into contiguous, non-empty pieces of near-equal length that concatenate to it.

    A reply shorter than ``delta_count`` characters is split into one piece per character, since the progress route
    refuses an empty delta.

    Args:
        reply_text: The text the deltas carry; not empty.
        delta_count: How many pieces to split it into; values below one give one piece.

    Raises:
        ValueError: ``reply_text`` is empty.
    """
    if not reply_text:
        raise ValueError("A progress reply must not be empty.")
    piece_count = max(1, min(delta_count, len(reply_text)))
    reply_length = len(reply_text)
    return [reply_text[index * reply_length // piece_count : (index + 1) * reply_length // piece_count] for index in range(piece_count)]


def progress_request_name(status_code: int) -> str:
    """Return the statistics name of a progress post answered with ``status_code``, so each status has its own row."""
    return f"{TEXT_PROGRESS_PATH} [{status_code}]"


def _pick_round_robin(keys: list[str], counter: itertools.count[int]) -> str:
    """Return the key at the counter's next position, or the anonymous key when there are none."""
    if not keys:
        return _config.get("anonymous_api_key", "0000000000")
    return keys[next(counter) % len(keys)]


chat_settings = ChatScenarioSettings()
"""The process-wide scenario settings the users read."""


class ChatEvidenceRecorder:
    """Append one JSON record per client request to the run's evidence file.

    The file stays open for the run and every record is flushed as it is written, so a killed run leaves every
    completed record readable without paying an open and close per request at thousands of requests a second.

    Thread Safety:
        Writes hold a gevent lock, so concurrent greenlets never interleave lines.
    """

    def __init__(self) -> None:
        """Create the recorder with no file; writes are dropped until ``configure`` names one."""
        self._lock = gevent.lock.RLock()
        self._handle: IO[str] | None = None

    def configure(self, path: str | None) -> None:
        """Open and truncate the evidence file, so a run never includes an earlier run's records.

        Args:
            path: The file to write; empty or None disables the recorder.
        """
        with self._lock:
            self.close()
            if not path:
                return
            evidence_path = Path(path)
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = evidence_path.open("w", encoding="utf-8")

    def write(self, record: dict[str, object]) -> None:
        """Append one record; a no-op when no file is open."""
        with self._lock:
            if self._handle is None:
                return
            self._handle.write(json.dumps(record, sort_keys=True) + "\n")
            self._handle.flush()

    def close(self) -> None:
        """Close the file, if one is open."""
        with self._lock:
            if self._handle is None:
                return
            self._handle.close()
            self._handle = None


chat_evidence_recorder = ChatEvidenceRecorder()
"""The process-wide recorder the clients and the start and stop hooks write through."""


def _write_record(record: ChatRequestRecord) -> None:
    """Append a request record to the evidence file."""
    chat_evidence_recorder.write(dataclasses.asdict(record))


def _fire_timing(environment: Environment, name: str, started_at: float, ended_at: float | None) -> None:
    """Record one stream timing as its own statistics row; a timing that never ended is not recorded."""
    if ended_at is None:
        return
    environment.events.request.fire(
        request_type=EVENT_STREAM_REQUEST_TYPE,
        name=name,
        response_time=(ended_at - started_at) * 1000,
        response_length=0,
        exception=None,
        context={},
    )


def _record_named(environment: Environment, resp: Any, name: str) -> None:
    """Record an expected response under ``name`` so the report shows its frequency."""
    _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))


def _parse_worker_submit_time(content: str | None) -> float | None:
    """Return the worker's submit time written into ``content``, or None when it carries none."""
    if not content:
        return None
    match = WORKER_SUBMIT_PATTERN.search(content)
    if match is None:
        return None
    return float(match.group(1))


def _request_id_from_completion_id(completion_id: str | None) -> str | None:
    """Return the Horde request id inside a completion's ``id``."""
    if not completion_id:
        return None
    return completion_id.removeprefix(COMPLETION_ID_PREFIX)


def _build_chat_body(*, stream: bool) -> dict[str, object]:
    """Create a chat completions request body with a distinct user message."""
    user_message = " ".join(random.sample(USER_MESSAGE_WORDS, k=4)) + " " + uuid.uuid4().hex[:12]
    body: dict[str, object] = {
        "model": chat_settings.model,
        "messages": [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": chat_settings.max_tokens,
        "stream": stream,
    }
    if stream and chat_settings.include_usage:
        body["stream_options"] = {"include_usage": True}
    return body


def _apply_chunk(record: ChatRequestRecord, chunk: dict[str, Any], received_at: float) -> None:
    """Mutate ``record`` with what one streamed data chunk carries."""
    if "error" in chunk:
        record.outcome = ChatOutcome.ERROR_CHUNK
        error = chunk["error"]
        record.error_code = error.get("code") if isinstance(error, dict) else None
        return
    record.request_id = record.request_id or _request_id_from_completion_id(chunk.get("id"))
    if chunk.get("usage"):
        record.usage_present = True
    x_horde = chunk.get("x_horde")
    if isinstance(x_horde, dict) and "estimated_usage" in x_horde:
        record.estimated_usage = bool(x_horde["estimated_usage"])
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if content is not None and record.content_at is None:
            record.content_at = received_at
            record.worker_submitted_at = _parse_worker_submit_time(content)
        if choice.get("finish_reason") and record.finish_reason is None:
            record.finish_reason = choice["finish_reason"]


def _consume_event_stream(resp: Any, record: ChatRequestRecord, *, deadline: float | None) -> None:
    """Mutate ``record`` by reading a server-sent event stream until ``[DONE]``, the end of the body, or ``deadline``.

    Args:
        resp: The streamed response.
        record: The request's record.
        deadline: The Unix time the client gives up at, or None to read to the end. The deadline is checked as each
            line arrives; the request's read timeout bounds the wait for a line.

    Raises:
        requests.RequestException: The connection failed or a read timed out.
    """
    lines = resp.iter_lines(decode_unicode=True)
    for raw_line in lines:
        received_at = time.time()
        line = raw_line or ""
        if line.startswith(COMMENT_PREFIX):
            if is_keepalive_comment(line):
                record.heartbeats += 1
                if record.first_heartbeat_at is None and record.content_at is None:
                    record.first_heartbeat_at = received_at
        elif line.startswith(DATA_FIELD_PREFIX):
            data = line[len(DATA_FIELD_PREFIX) :].strip()
            if data == DONE_MARKER:
                record.finished_at = received_at
                if record.outcome != ChatOutcome.ERROR_CHUNK:
                    record.outcome = ChatOutcome.COMPLETED
                # Reading to the end of the body lets the connection go back to the pool, so the client's next
                # request reuses it as an OpenAI SDK client would, instead of opening a new connection each time.
                for _trailing_line in lines:
                    pass
                return
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, dict):
                _apply_chunk(record, chunk, received_at)
        if deadline is not None and received_at >= deadline:
            record.finished_at = received_at
            record.outcome = ChatOutcome.DISCONNECTED
            resp.close()
            return
    record.finished_at = time.time()
    if record.outcome != ChatOutcome.ERROR_CHUNK:
        record.outcome = ChatOutcome.TRUNCATED


def _apply_completion_body(record: ChatRequestRecord, body: dict[str, Any] | None) -> None:
    """Mutate ``record`` with a non-streamed completion body."""
    if not isinstance(body, dict):
        return
    record.request_id = _request_id_from_completion_id(body.get("id"))
    record.usage_present = bool(body.get("usage"))
    x_horde = body.get("x_horde")
    if isinstance(x_horde, dict) and "estimated_usage" in x_horde:
        record.estimated_usage = bool(x_horde["estimated_usage"])
    choices = body.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        record.worker_submitted_at = _parse_worker_submit_time(message.get("content"))
        record.finish_reason = choices[0].get("finish_reason")


def _back_off_after_transport_error(record: ChatRequestRecord) -> None:
    """Wait a few seconds when ``record`` ended in a transport error, before the client's next request."""
    if record.outcome != ChatOutcome.TRANSPORT_ERROR:
        return
    time.sleep(random.uniform(TRANSPORT_ERROR_BACKOFF_MIN_SECONDS, TRANSPORT_ERROR_BACKOFF_MAX_SECONDS))


class _ChatClientBase(HttpUser):
    """Shared request handling for the client populations, which talk to the front rather than to a Horde."""

    abstract = True
    wait_time = between(CLIENT_THINK_MIN_SECONDS, CLIENT_THINK_MAX_SECONDS)

    def on_start(self) -> None:
        """Point the client at the front and take a requestor key."""
        if chat_settings.front_host:
            self.client.base_url = chat_settings.front_host
        self.api_key = chat_settings.next_requestor_key()

    def _chat_headers(self) -> dict[str, str]:
        """Return the request headers: the API key as a bearer token, as an OpenAI client sends it."""
        return {"Authorization": f"Bearer {self.api_key}"}

    def _handle_refusal(self, resp: Any, record: ChatRequestRecord, name: str) -> None:
        """Classify a non-200 front response, record it, and back off after a rate limit.

        Raises:
            RescheduleTask: After a 429 back-off, so the client starts a fresh request.
        """
        body = _safe_json(resp)
        record.status = resp.status_code
        record.error_code = error_code_of(body)
        record.outcome = ChatOutcome.REFUSED
        record.finished_at = time.time()
        _write_record(record)
        is_expected = resp.status_code in EXPECTED_REFUSAL_STATUSES or (
            resp.status_code == 400 and record.error_code in EXPECTED_REFUSAL_RCS
        )
        if not is_expected:
            resp.failure(f"Status {resp.status_code}: {resp.text[:RESPONSE_EXCERPT_CHARACTERS]}")
            return
        resp.success()
        _record_named(self.environment, resp, f"{name} {resp.status_code}")
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            backoff_seconds = (
                float(retry_after) if retry_after else random.uniform(RATE_LIMIT_BACKOFF_MIN_SECONDS, RATE_LIMIT_BACKOFF_MAX_SECONDS)
            )
            time.sleep(min(backoff_seconds, RATE_LIMIT_BACKOFF_MAX_SECONDS * 2))
            raise RescheduleTask()

    def _send_streamed(self, *, name: str, client_kind: ChatClientKind, read_timeout: float, deadline_seconds: float | None) -> None:
        """Send one streamed request, read its events, and record it.

        Args:
            name: The statistics name.
            client_kind: The population sending it.
            read_timeout: The longest wait for the response headers or between two lines.
            deadline_seconds: How long after sending the client gives up, or None to read to ``[DONE]``.
        """
        record = ChatRequestRecord(client=client_kind, sent_at=time.time())
        deadline = record.sent_at + deadline_seconds if deadline_seconds is not None else None
        with self.client.post(
            CHAT_COMPLETIONS_PATH,
            json=_build_chat_body(stream=True),
            headers=self._chat_headers(),
            catch_response=True,
            name=name,
            stream=True,
            timeout=(CONNECT_TIMEOUT_SECONDS, read_timeout),
        ) as resp:
            if resp.status_code == 0:
                self._finish_transport_error(resp, record, name, gave_up=deadline is not None)
            elif resp.status_code != 200:
                self._handle_refusal(resp, record, name)
            else:
                self._read_stream(resp, record, name, deadline=deadline)
        self._fire_stream_timings(name, record)
        _back_off_after_transport_error(record)

    def _read_stream(self, resp: Any, record: ChatRequestRecord, name: str, *, deadline: float | None) -> None:
        """Read an accepted stream into ``record``, write the record, and mark the response's result."""
        record.status = resp.status_code
        try:
            _consume_event_stream(resp, record, deadline=deadline)
        except requests.RequestException as error:
            record.finished_at = time.time()
            if deadline is not None:
                record.outcome = ChatOutcome.DISCONNECTED
            else:
                record.outcome = ChatOutcome.TRANSPORT_ERROR
                resp.failure(f"Stream read failed: {error}")
        finally:
            resp.close()
        _write_record(record)
        if record.outcome == ChatOutcome.COMPLETED:
            resp.success()
        elif record.outcome == ChatOutcome.DISCONNECTED:
            resp.success()
            # The body was consumed as a stream, so its length is not available here.
            _record_expected(
                self.environment, "POST", f"{name} disconnected", ((record.finished_at or time.time()) - record.sent_at) * 1000, 0
            )
        elif record.outcome == ChatOutcome.ERROR_CHUNK:
            resp.failure(f"Stream delivered an error chunk: {record.error_code}")
        elif record.outcome == ChatOutcome.TRUNCATED:
            resp.failure("Stream ended without [DONE]")

    def _fire_stream_timings(self, name: str, record: ChatRequestRecord) -> None:
        """Record the time to the first keepalive, to the content and to the end as their own statistics rows."""
        _fire_timing(self.environment, f"{name} [time-to-heartbeat]", record.sent_at, record.first_heartbeat_at)
        _fire_timing(self.environment, f"{name} [time-to-content]", record.sent_at, record.content_at)
        if record.outcome == ChatOutcome.COMPLETED:
            _fire_timing(self.environment, f"{name} [total]", record.sent_at, record.finished_at)

    def _send_nonstreamed(self, *, name: str, client_kind: ChatClientKind, read_timeout: float, gives_up: bool) -> None:
        """Send one non-streamed request, wait for its body, and record it.

        Args:
            name: The statistics name.
            client_kind: The population sending it.
            read_timeout: The longest wait for the response.
            gives_up: Whether a read timeout is the client leaving on purpose rather than a failure.
        """
        record = ChatRequestRecord(client=client_kind, sent_at=time.time())
        with self.client.post(
            CHAT_COMPLETIONS_PATH,
            json=_build_chat_body(stream=False),
            headers=self._chat_headers(),
            catch_response=True,
            name=name,
            timeout=(CONNECT_TIMEOUT_SECONDS, read_timeout),
        ) as resp:
            if resp.status_code == 0:
                self._finish_transport_error(resp, record, name, gave_up=gives_up)
            elif resp.status_code != 200:
                self._handle_refusal(resp, record, name)
            else:
                record.status = resp.status_code
                record.finished_at = time.time()
                record.content_at = record.finished_at
                record.outcome = ChatOutcome.COMPLETED
                _apply_completion_body(record, _safe_json(resp))
                _write_record(record)
                resp.success()
        _back_off_after_transport_error(record)

    def _finish_transport_error(self, resp: Any, record: ChatRequestRecord, name: str, *, gave_up: bool) -> None:
        """Record a request that got no HTTP response: a deliberate disconnect when ``gave_up``, else a failure."""
        record.finished_at = time.time()
        error = getattr(resp, "error", None)
        if gave_up and isinstance(error, requests.Timeout):
            record.outcome = ChatOutcome.DISCONNECTED
            _write_record(record)
            resp.success()
            _record_named(self.environment, resp, f"{name} disconnected")
            return
        record.outcome = ChatOutcome.TRANSPORT_ERROR
        _write_record(record)
        resp.failure(f"Transport error: {error}")


class ChatStreamClient(_ChatClientBase):
    """Post streamed chat requests to the front and read each to ``[DONE]``."""

    weight = 70

    @tag("chat", "client", "stream")
    @task
    def stream_completion(self) -> None:
        """Send one streamed request."""
        self._send_streamed(
            name=STREAM_REQUEST_NAME,
            client_kind=ChatClientKind.STREAM,
            read_timeout=STREAM_READ_TIMEOUT_SECONDS,
            deadline_seconds=None,
        )


class ChatClient(_ChatClientBase):
    """Post non-streamed chat requests to the front and block for the JSON body."""

    weight = 25

    @tag("chat", "client", "nonstream")
    @task
    def completion(self) -> None:
        """Send one non-streamed request."""
        self._send_nonstreamed(
            name=NONSTREAM_REQUEST_NAME,
            client_kind=ChatClientKind.NONSTREAM,
            read_timeout=chat_settings.nonstream_timeout_seconds + NONSTREAM_READ_MARGIN_SECONDS,
            gives_up=False,
        )


class ChatImpatientClient(_ChatClientBase):
    """Post chat requests and disconnect after a short timeout, so the front cancels them on the Horde."""

    weight = 5

    @tag("chat", "client", "impatient")
    @task
    def abandoned_completion(self) -> None:
        """Send one request, streamed or not, and leave at the impatient timeout."""
        timeout_seconds = chat_settings.impatient_timeout_seconds
        if random.random() < chat_settings.impatient_stream_chance:
            self._send_streamed(
                name=IMPATIENT_REQUEST_NAME,
                client_kind=ChatClientKind.IMPATIENT_STREAM,
                read_timeout=timeout_seconds,
                deadline_seconds=timeout_seconds,
            )
            return
        self._send_nonstreamed(
            name=IMPATIENT_REQUEST_NAME,
            client_kind=ChatClientKind.IMPATIENT_NONSTREAM,
            read_timeout=timeout_seconds,
            gives_up=True,
        )


class ChatWorkerSimulator(HttpUser):
    """Simulate a text worker that accepts chat jobs: pop with a chat declaration, wait, submit a structured reply.

    The pop and submit bodies come from the fake worker the integration suite uses, so both speak one contract.
    """

    weight = 1
    fixed_count = 0
    wait_time = between(WORKER_POLL_MIN_SECONDS, WORKER_POLL_MAX_SECONDS)

    def create_worker_name(self) -> str:
        """Create a worker name unlikely to collide with another simulated worker's."""
        return _random_worker_name("StressChatWorker-", 6)

    def on_start(self) -> None:
        """Pick a Horde instance, a name and a worker key."""
        self.client.base_url = chat_settings.next_horde_url()
        self.worker_name = self.create_worker_name()
        self.api_key = chat_settings.next_worker_key()

    def _record_expected_response(self, resp: Any, path: str) -> None:
        """Mark a response expected and record it under the path's expected name."""
        resp.success()
        _record_expected(self.environment, "POST", path, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))

    @tag("chat", "worker")
    @task
    def pop_and_submit_chat(self) -> None:
        """Pop one job and, when one is assigned, submit it after the simulated generation time.

        Raises:
            RescheduleTask: After an expected refusal, so the worker pops again with a fresh identity when needed.
        """
        pop_payload = build_pop_body(
            name=self.worker_name,
            models=[chat_settings.model],
            bridge_agent=WORKER_BRIDGE_AGENT,
            chat_jobs=True,
            chat_max_context_length=WORKER_MAX_CONTEXT_LENGTH,
            chat_max_length=WORKER_MAX_LENGTH,
            max_context_length=WORKER_MAX_CONTEXT_LENGTH,
            max_length=WORKER_MAX_LENGTH,
        )
        pop_payload.update({"nsfw": True, "softprompts": [], "threads": 1})
        with self.client.post(
            TEXT_POP_PATH,
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=TEXT_POP_PATH,
        ) as resp:
            if resp.status_code == 0:
                resp.failure(f"Chat pop transport error: {resp.error}")
                return
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (_is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)):
                    self._record_expected_response(resp, TEXT_POP_PATH)
                    return_code = body.get("rc") if isinstance(body, dict) else None
                    if _is_too_many_workers(body) or return_code in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = chat_settings.next_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    self._record_expected_response(resp, TEXT_POP_PATH)
                    raise RescheduleTask()
                resp.failure(f"Chat pop failed: Status {resp.status_code}: {resp.text[:RESPONSE_EXCERPT_CHARACTERS]}")
                return
            pop_body = body or {}
            job_id = pop_body.get("id")
            if not job_id:
                resp.success()
                return
            job_payload = pop_body.get("payload") or {}
            is_chat_job = pop_body.get("chat") is True
            messages = job_payload.get("messages")
            if is_chat_job and (not isinstance(messages, list) or "prompt" in job_payload):
                resp.failure("Chat job payload must carry messages and no prompt")
                return
            resp.success()

        prompt_characters = sum(len(str(message.get("content", ""))) for message in messages or [] if isinstance(message, dict))
        generation_seconds = random.uniform(chat_settings.gen_time_min_seconds, chat_settings.gen_time_max_seconds)
        posts_progress = is_chat_job and chat_settings.worker_progress and job_payload.get(STREAM_PROGRESS_FIELD) is True
        if posts_progress:
            # The submitted generation starts with the posted text, so the front relays it as a prefix.
            request_cancelled = post_progress_while_generating(
                self.client,
                api_key=self.api_key,
                generation_id=job_id,
                reply_text=WORKER_GENERATION_TEXT,
                generation_seconds=generation_seconds,
                interval_seconds=chat_settings.worker_progress_interval_seconds,
            )
            if request_cancelled:
                return
        else:
            time.sleep(generation_seconds)
        submitted_at = time.time()
        generation = f"{WORKER_GENERATION_TEXT} {WORKER_SUBMIT_MARKER}{submitted_at:.6f}"
        submit_payload: dict[str, object]
        if is_chat_job:
            reports_usage = random.random() >= chat_settings.unreported_usage_chance
            submit_payload = build_chat_submit_body(
                generation_id=job_id,
                content=generation,
                model_id=pop_body.get("model") or chat_settings.model,
                prompt_tokens=max(1, prompt_characters // CHARACTERS_PER_TOKEN) if reports_usage else None,
                completion_tokens=max(1, len(generation) // CHARACTERS_PER_TOKEN) if reports_usage else None,
            )
        else:
            # A text job reaches a chat worker too; it is submitted as a text worker submits it.
            submit_payload = {"id": job_id, "generation": generation, "state": "ok", "seed": random.randint(0, 999999999)}
        with self.client.post(
            TEXT_SUBMIT_PATH,
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=TEXT_SUBMIT_PATH,
        ) as resp:
            if resp.status_code == 0:
                resp.failure(f"Chat submit transport error: {resp.error}")
                return
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code in (404, 429) or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                self._record_expected_response(resp, TEXT_SUBMIT_PATH)
                return
            resp.failure(f"Chat submit failed: {resp.status_code}: {resp.text[:RESPONSE_EXCERPT_CHARACTERS]}")


@dataclasses.dataclass(frozen=True)
class ProgressAnswer:
    """Represents how the progress route answered one post."""

    status_code: int
    """The HTTP status; 0 for a transport error."""
    cancelled: bool
    """Whether a ``200`` acknowledgement reported the request cancelled; False for any other status."""


def post_progress_while_generating(
    client: Any,
    *,
    api_key: str,
    generation_id: str,
    reply_text: str,
    generation_seconds: float,
    interval_seconds: float,
) -> bool:
    """Post ``reply_text`` as content deltas spread over the generation time, then return when it has passed.

    The reply is split evenly into one delta per progress interval, each posted at the end of its slot, as a worker
    posts what it generated in the last interval. An unanswered or refused-for-now post (transport error, 429, 503) is
    sent again unchanged at the next slot, and the slots after it wait, so ``seq`` stays in order; text still unposted
    at the end is carried only by the submit. A stop status ends the posting, and the generation time still runs out
    before the caller submits. An acknowledgement carrying ``cancelled`` ends the generation at once, as a worker
    stops generating, and the caller does not submit.

    Args:
        client: The simulated worker's Locust HTTP session.
        api_key: The worker's key, which owns the generation's lease.
        generation_id: The popped generation id.
        reply_text: The text the deltas carry; the submitted reply must start with it.
        generation_seconds: The simulated generation time.
        interval_seconds: The time between two progress posts; positive.

    Returns:
        True when an acknowledgement reported the request cancelled, so the job is not submitted.
    """
    deltas = split_reply_into_deltas(
        reply_text,
        count_progress_deltas(generation_seconds=generation_seconds, interval_seconds=interval_seconds),
    )
    slot_seconds = generation_seconds / len(deltas)
    started_at = time.monotonic()
    generated_length = 0
    accepted_length = 0
    next_seq = 1
    pending_content: str | None = None
    for slot_index, delta in enumerate(deltas):
        _sleep_until(started_at + (slot_index + 1) * slot_seconds)
        generated_length += len(delta)
        if pending_content is None:
            pending_content = reply_text[accepted_length:generated_length]
        answer = post_progress_delta(client, api_key=api_key, generation_id=generation_id, seq=next_seq, content=pending_content)
        if answer.cancelled:
            return True
        if answer.status_code == PROGRESS_ACCEPTED_STATUS:
            accepted_length += len(pending_content)
            next_seq += 1
            pending_content = None
            continue
        if answer.status_code in PROGRESS_STOP_STATUSES:
            break
    _sleep_until(started_at + generation_seconds)
    return False


def post_progress_delta(client: Any, *, api_key: str, generation_id: str, seq: int, content: str) -> ProgressAnswer:
    """Post one content delta and return the status it was answered with, 0 for a transport error, and whether its
    acknowledgement reported the request cancelled.

    The request is recorded under its status's own name, so the run's statistics count the posts by outcome.

    Args:
        client: The simulated worker's Locust HTTP session.
        api_key: The worker's key, which owns the generation's lease.
        generation_id: The popped generation id.
        seq: The delta's sequence number, one more than the last accepted one.
        content: The delta's text.
    """
    progress_body = {"id": generation_id, "seq": seq, "delta": {"content": content}}
    with client.post(
        TEXT_PROGRESS_PATH,
        json=progress_body,
        headers=_headers(api_key),
        catch_response=True,
        name=TEXT_PROGRESS_PATH,
        timeout=(CONNECT_TIMEOUT_SECONDS, PROGRESS_READ_TIMEOUT_SECONDS),
    ) as resp:
        resp.request_meta["name"] = progress_request_name(resp.status_code)
        if resp.status_code == 0:
            resp.failure(f"Chat progress transport error: {resp.error}")
            return ProgressAnswer(status_code=0, cancelled=False)
        if resp.status_code == 409:
            # Every post is either the next seq or an unchanged resend, so a 409 means an answer was lost or the
            # Horde holds a different sequence; resyncing to its expected_seq is left to real workers.
            logger.warning(
                "Chat progress for %s refused out of sequence at seq %d: %s",
                generation_id,
                seq,
                resp.text[:RESPONSE_EXCERPT_CHARACTERS],
            )
        if resp.status_code in PROGRESS_EXPECTED_STATUSES:
            resp.success()
        else:
            resp.failure(f"Chat progress failed: {resp.status_code}: {resp.text[:RESPONSE_EXCERPT_CHARACTERS]}")
        body = _safe_json(resp) if resp.status_code == PROGRESS_ACCEPTED_STATUS else None
        return ProgressAnswer(status_code=resp.status_code, cancelled=isinstance(body, dict) and body.get("cancelled") is True)


def _sleep_until(deadline: float) -> None:
    """Sleep until the ``time.monotonic`` deadline; return at once when it has passed."""
    remaining_seconds = deadline - time.monotonic()
    if remaining_seconds > 0:
        time.sleep(remaining_seconds)
