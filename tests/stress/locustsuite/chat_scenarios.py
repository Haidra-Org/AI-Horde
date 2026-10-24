# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat front request permutations the stress mix sends, each with the one outcome the contract promises for it.

Every scenario is a named function that builds a [`ChatScenario`][tests.stress.locustsuite.chat_scenarios.ChatScenario]:
the request body, which worker persona has to be online for it, and a
[`CompletionExpectation`][tests.stress.locustsuite.chat_checks.CompletionExpectation] or
[`ErrorExpectation`][tests.stress.locustsuite.chat_checks.ErrorExpectation]. The functions are grouped by intent in
[`SCENARIO_BUILDERS`][tests.stress.locustsuite.chat_scenarios.SCENARIO_BUILDERS]; the front client picks a group by the
run's weights and a scenario within it at random. Each docstring says why the permutation is worth sending.

The expected outcomes follow ``docs/how-to/send_chat_completions.md`` and ``docs/reference/chat_requests.md``. A
scenario that needs a worker behaviour writes a reply marker (see
[`chat_worker_script`][tests.stress.locustsuite.chat_worker_script]) and targets a model only the suite's simulated
workers serve, so no other worker can answer it differently.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import random
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_SCHEMA_NESTING, MAX_CHAT_TEXT_BYTES, MIN_CHAT_MAX_TOKENS

from horde.chat_contract import MAX_CHAT_BODY_BYTES, MAX_CHAT_MESSAGE_COUNT

from .chat_checks import CompletionExpectation, ContentRule, ErrorExpectation
from .chat_worker_script import VISION_OK_PREFIX, ChatPersonaRole, ReplyScript, nonce_marker, reply_marker
from .config import _TINY_PNG_B64

__all__ = [
    "LITE_ORIGIN",
    "OVERSIZED_BODY_BYTES",
    "SCENARIO_BUILDERS",
    "STRESS_CLIENT_TITLE",
    "AuthorizationMode",
    "ChatModelNames",
    "ChatScenario",
    "HourlyBudget",
    "ScenarioContext",
    "ScenarioGroup",
    "StreamMode",
    "ANONYMOUS_SHARE_GROUPS",
    "FAIL_FAST_SHARE_GROUPS",
    "UPFRONT_KUDOS_FREE_TOKENS",
    "as_anonymous",
    "as_fail_fast",
    "build_authorization_headers",
    "build_nested_object_schema",
    "build_oversized_body",
    "parse_group_weights",
    "pick_scenario_builder",
]

SECONDS_PER_HOUR: float = 3600.0
"""The window an [`HourlyBudget`][tests.stress.locustsuite.chat_scenarios.HourlyBudget] counts over."""

REPLY_TOKENS: int = 64
"""The ``max_tokens`` of most scenarios: short, so a request costs little kudos and fits every persona."""

LOW_TOOL_TEMPERATURE: float = 0.2
"""The temperature of tool requests to the limited persona, below its tool temperature limit."""

HIGH_TOOL_TEMPERATURE: float = 0.9
"""A tool temperature above the limited persona's limit."""

FAULTED_CODE: str = "ChatRequestFaulted"
"""The front's code for a request the Horde faulted, after its workers' replies were refused or faulted."""

DECLARATION_GAP_CODE: str = "no_worker_supports_features"
"""The front's code for a request no online worker of the model declares enough for."""

NO_WORKER_SUPPORTS_LIMIT_CODE: str = "no_worker_supports_limit"
"""The front's code for a request over the size limits every online worker serving its model declares."""

UNSUPPORTED_PARAMETER_CODE: str = "UnsupportedChatParameter"
"""The code of a refused key or value."""

REQUEST_CONFLICT_CODE: str = "ChatRequestConflict"
"""The code of contradictory request values."""

BAD_REQUEST_CODE: str = "BadRequest"
"""The code of a malformed body."""

WORKER_FAULT_GENERATION_ERROR_MESSAGE: str = "The worker's backend failed while generating."
"""The Horde's sentence for ``fault_reason`` ``generation_error``, which the 502 message ends with."""

STRESS_CLIENT_TITLE: str = "aihorde-stress-chat-mix"
"""The ``X-Title`` the front attributes the suite's traffic to."""

LITE_ORIGIN: str = "https://lite.koboldai.net"
"""The ``Origin`` a browser running KoboldAI Lite sends."""

GET_WEATHER_TOOL: Mapping[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False},
    },
}
"""The first offered tool; a well-behaved worker calls it."""

GET_TIME_TOOL: Mapping[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the local time for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
"""The second offered tool; forced scenarios name it so a call to it proves the choice was honoured."""

OFFERED_TOOL_NAMES: frozenset[str] = frozenset({"get_weather", "get_time"})
"""The names of the offered tools."""

RATING_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "score": {"type": "integer", "minimum": 2},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "score", "tags"],
    "additionalProperties": False,
}
"""A ``strict`` schema with ``minimum``, ``required`` and ``additionalProperties``; ``{"x": 0}`` breaks it."""


class ScenarioGroup(StrEnum):
    """The intent a group of scenarios exercises; the run weights groups with ``--chat-mix-group-weights``."""

    ORDINARY = "ordinary"
    """Plain client use: prompts, history, limits, sampling, stock defaults."""
    TOOLS = "tools"
    """Tool-using clients."""
    STRUCTURED = "structured"
    """Structured output."""
    CONFORMANCE = "conformance"
    """``strict`` against ``best_effort`` for one request, with workers that break the contract."""
    GAP = "gap"
    """Requests the limited persona lacks a declaration or a limit for."""
    MISUSE = "misuse"
    """Clients doing it wrong; refused before anything is queued."""
    LITE = "lite"
    """Requests shaped as KoboldAI Lite's OpenAI mode sends them from a browser."""
    VISION = "vision"
    """Image input; runs only when the conformant persona declares vision."""


class AuthorizationMode(StrEnum):
    """How a scenario authenticates."""

    REQUESTOR_KEY = "requestor_key"
    """``Authorization: Bearer <requestor key>``, as an OpenAI SDK sends it."""
    ANONYMOUS = "anonymous"
    """No ``Authorization`` header, which the front serves under the anonymous key."""
    MALFORMED = "malformed"
    """An ``Authorization`` header that is not ``Bearer <key>``."""
    UNKNOWN_KEY = "unknown_key"
    """A bearer key no user owns."""


class StreamMode(StrEnum):
    """Whether a scenario is sent streamed."""

    EITHER = "either"
    """Streamed or not at random, with or without the usage chunk."""
    LITE_STREAM = "lite_stream"
    """Always streamed, without ``stream_options``, as Lite sends it."""
    NEVER = "never"
    """Never streamed: the refusal comes before any header, so streaming adds nothing."""


UNKNOWN_BEARER_KEY: str = "stress-unknown-key-0000"
"""A bearer key no user owns."""

MALFORMED_AUTHORIZATION: str = "Basic c3RyZXNzOnN0cmVzcw=="
"""An ``Authorization`` value that is not ``Bearer <key>``."""


def build_authorization_headers(authorization: AuthorizationMode, *, api_key: str) -> dict[str, str]:
    """Return the ``Authorization`` header a scenario sends, or none for an anonymous one.

    Args:
        authorization: How the scenario authenticates.
        api_key: The requestor key, used under ``AuthorizationMode.REQUESTOR_KEY``.
    """
    if authorization == AuthorizationMode.REQUESTOR_KEY:
        return {"Authorization": f"Bearer {api_key}"}
    if authorization == AuthorizationMode.MALFORMED:
        return {"Authorization": MALFORMED_AUTHORIZATION}
    if authorization == AuthorizationMode.UNKNOWN_KEY:
        return {"Authorization": f"Bearer {UNKNOWN_BEARER_KEY}"}
    return {}


@dataclass(frozen=True)
class ChatModelNames:
    """Represents the model ids the simulated personas serve, one per persona so a scenario reaches the one it needs."""

    conformant: str
    """The conformant persona's model."""
    limited: str
    """The limited persona's model."""
    flaky: str
    """The flaky persona's model."""


@dataclass(frozen=True)
class ScenarioContext:
    """Represents what a scenario builder reads from the run."""

    models: ChatModelNames
    """The personas' models."""
    worker_max_length: int
    """The ``chat_limits.max_length`` every persona declares."""
    nonce: str
    """A value unique to this request, written into its text so no two requests are identical."""


@dataclass(frozen=True)
class ChatScenario:
    """Represents one front request and the outcome the contract promises for it."""

    name: str
    """The scenario's name, used in the statistics row."""
    group: ScenarioGroup
    """The group it belongs to."""
    body: dict[str, Any]
    """The request body, without ``stream`` and ``stream_options``."""
    expectation: CompletionExpectation | ErrorExpectation
    """The outcome the contract promises."""
    persona: ChatPersonaRole | None
    """The persona that has to be online for the outcome; None when the request is refused before it is queued."""
    alternative_error: ErrorExpectation | None = None
    """A second outcome the contract allows, such as a fault after a flaky worker broke three replies in a row."""
    stream_mode: StreamMode = StreamMode.EITHER
    """Whether it is streamed."""
    authorization: AuthorizationMode = AuthorizationMode.REQUESTOR_KEY
    """How it authenticates."""
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    """Headers beyond authorization and ``X-Title``."""
    aborts_worker_jobs: bool = False
    """True when its faults count towards the worker's hourly aborted-job threshold, so it draws on the run's aborting
    budget."""
    oversized_body: bool = False
    """True when the body is replaced by one above the front's byte limit, so the run budgets it."""
    kudos_upfront_benign: bool = True
    """Whether a 403 ``KudosUpfront`` is an operating condition; False where the request is too small for the Horde to
    ask upfront kudos at all, so the refusal would be a contract failure."""
    fail_fast: bool = False
    """True when the body asks ``horde.availability_mode: fail_fast``, so a 503 ``queue_unavailable`` is an operating
    condition beside the expected outcome."""


class HourlyBudget:
    """Count events in the last hour and refuse one past the limit, so scenarios that cost a shared resource stay rare.

    Each aborting scenario (an undeclared function, duplicate call ids, a worker fault) faults its request's jobs three
    times, and every fault counts as an aborted job of the worker that popped it. The Horde puts a worker into
    maintenance past 20 aborted jobs an hour (10 while a deployment is in raid mode, as dev is), and a worker name the
    run cannot replace would end its chat coverage, so the run caps these scenarios in total, across personas.

    Thread Safety:
        Not locked; Locust users run as greenlets on one thread and never yield inside a call.
    """

    def __init__(self, limit_per_hour: int) -> None:
        """Create a budget of ``limit_per_hour`` events; 0 refuses every event."""
        self.limit_per_hour: int = limit_per_hour
        """The most events the last hour may hold."""
        self._taken_at: deque[float] = deque()

    def try_take(self, now: float | None = None) -> bool:
        """Return True and count one event when the last hour holds fewer than the limit, else False.

        Args:
            now: The current time in seconds; the wall clock when omitted.
        """
        current_time = time.time() if now is None else now
        while self._taken_at and current_time - self._taken_at[0] >= SECONDS_PER_HOUR:
            self._taken_at.popleft()
        if len(self._taken_at) >= self.limit_per_hour:
            return False
        self._taken_at.append(current_time)
        return True


def parse_group_weights(weights_text: str) -> dict[ScenarioGroup, int]:
    """Parse ``group=weight`` pairs, separated by commas, into weights by group; an omitted group weighs 0.

    Args:
        weights_text: Such as ``ordinary=6,tools=4``.

    Raises:
        ValueError: A pair names an unknown group or a weight that is not a non-negative integer.
    """
    weights = dict.fromkeys(ScenarioGroup, 0)
    for pair in weights_text.split(","):
        if not pair.strip():
            continue
        group_name, separator, weight_text = pair.partition("=")
        if not separator:
            raise ValueError(f"Expected group=weight, got {pair!r}")
        group = ScenarioGroup(group_name.strip())
        weight = int(weight_text)
        if weight < 0:
            raise ValueError(f"The weight of {group_name!r} is negative")
        weights[group] = weight
    return weights


def build_nested_object_schema(object_count: int) -> dict[str, Any]:
    """Return a ``strict``-valid schema of ``object_count`` object schemas nested one inside the next, the innermost
    holding a string property.

    The SDK counts nesting by schema levels below the root: the innermost object sits ``object_count - 1`` levels below
    it and its string property ``object_count`` levels, so ``MAX_CHAT_SCHEMA_NESTING`` objects is the deepest schema
    accepted and one more is refused.

    Args:
        object_count: How many object schemas the chain holds, the root included.
    """
    innermost: dict[str, Any] = {
        "type": "object",
        "properties": {"leaf": {"type": "string"}},
        "required": ["leaf"],
        "additionalProperties": False,
    }
    schema = innermost
    for _level in range(object_count - 1):
        schema = {"type": "object", "properties": {"inner": schema}, "required": ["inner"], "additionalProperties": False}
    return schema


def build_oversized_body(model: str, total_bytes: int) -> bytes:
    """Return a valid JSON chat request body of exactly ``total_bytes`` bytes, its one user message padded to fit.

    Args:
        model: The request's model.
        total_bytes: The body's size; it has to exceed the size of the body with an empty message.
    """
    placeholder = "PADDING"
    template = json.dumps({"model": model, "messages": [{"role": "user", "content": placeholder}]})
    before_padding, after_padding = template.split(placeholder)
    padding_length = total_bytes - len(before_padding.encode()) - len(after_padding.encode())
    if padding_length < 0:
        raise ValueError(f"A body of {total_bytes} bytes cannot hold a chat request for {model!r}")
    return f"{before_padding}{'a' * padding_length}{after_padding}".encode()


def _user(text: str) -> dict[str, Any]:
    """Create a user message."""
    return {"role": "user", "content": text}


def _scripted_user(context: ScenarioContext, text: str, script: ReplyScript) -> dict[str, Any]:
    """Create a user message carrying a reply marker and the request's nonce."""
    return _user(f"{text} {reply_marker(script)} {nonce_marker(context.nonce)}")


def _body(model: str, messages: list[dict[str, Any]], **request_keys: Any) -> dict[str, Any]:
    """Create a request body with the suite's short ``max_tokens`` unless the keys set another."""
    return {"model": model, "messages": messages, "max_tokens": REPLY_TOKENS, **request_keys}


def _text_reply(context: ScenarioContext, model: str, *, content_rule: ContentRule = ContentRule.TEXT) -> CompletionExpectation:
    """Create the expectation of a text reply to this request (it carries the nonce) that finished with ``stop``."""
    return CompletionExpectation(model=model, finish_reasons=frozenset({"stop"}), content_rule=content_rule, nonce=context.nonce)


def _tool_reply(
    context: ScenarioContext, model: str, *, names: frozenset[str], finish_reasons: frozenset[str], calls: int, content_rule: ContentRule
) -> CompletionExpectation:
    """Create the expectation of a reply to this request with exactly ``calls`` tool calls."""
    return CompletionExpectation(
        nonce=context.nonce,
        model=model,
        finish_reasons=finish_reasons,
        content_rule=content_rule,
        tool_call_names=names,
        min_tool_calls=calls,
        max_tool_calls=calls,
    )


def _faulted(rc: str, *, message_fragments: tuple[str, ...] = ()) -> ErrorExpectation:
    """Create the expectation of a 502 for a request that faulted after its workers' replies were refused or faulted."""
    return ErrorExpectation(
        status=502,
        codes=frozenset({FAULTED_CODE}),
        after_headers=True,
        rc=rc,
        message_fragments=message_fragments,
        should_not_retry=True,
    )


def _declaration_gap(missing: str) -> ErrorExpectation:
    """Create the expectation of a 404 naming a declaration or limit every online worker of the model lacks."""
    return ErrorExpectation(
        status=404,
        codes=frozenset({DECLARATION_GAP_CODE}),
        message_fragments=(missing,),
        should_not_retry=True,
        retry_after=False,
    )


def _refused(code: str, *, status: int = 400, param: str | None = None) -> ErrorExpectation:
    """Create the expectation of a refusal before anything was queued: no ``x-should-retry``."""
    return ErrorExpectation(status=status, codes=frozenset({code}), param=param, should_not_retry=False)


def _tools_body(context: ScenarioContext, model: str, text: str, **request_keys: Any) -> dict[str, Any]:
    """Create a body offering both tools."""
    return _body(
        model, [_user(f"{text} {nonce_marker(context.nonce)}")], tools=[dict(GET_WEATHER_TOOL), dict(GET_TIME_TOOL)], **request_keys
    )


def _forced_get_time() -> dict[str, Any]:
    """Return a ``tool_choice`` forcing ``get_time``."""
    return {"type": "function", "function": {"name": "get_time"}}


def _rating_format() -> dict[str, Any]:
    """Return a ``strict`` ``json_schema`` response format over the rating schema."""
    return {"type": "json_schema", "json_schema": {"name": "rating", "schema": dict(RATING_SCHEMA), "strict": True}}


def _tool_round_history(context: ScenarioContext) -> list[dict[str, Any]]:
    """Return a finished tool round as an OpenAI SDK replays it: the assistant message with the SDK's own fields, then
    the tool result."""
    return [
        _user(f"What is the weather in Paris? {nonce_marker(context.nonce)}"),
        {
            "role": "assistant",
            "content": None,
            "refusal": None,
            "annotations": [],
            "tool_calls": [
                {
                    "id": "call_round_1",
                    "index": 0,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}', "parsed_arguments": None},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_round_1", "content": '{"temperature_c": 18, "sky": "sunny"}'},
    ]


# Ordinary client use


def plain_prompt(context: ScenarioContext) -> ChatScenario:
    """One user message: the request every OpenAI client sends first."""
    model = context.models.conformant
    return ChatScenario(
        "plain_prompt",
        ScenarioGroup.ORDINARY,
        _body(model, [_user(f"Say hello. {nonce_marker(context.nonce)}")]),
        _text_reply(context, model),
        ChatPersonaRole.CONFORMANT,
    )


def system_and_history(context: ScenarioContext) -> ChatScenario:
    """A system prompt, a developer message and earlier turns: the roles a chat UI replays every turn."""
    model = context.models.conformant
    messages = [
        {"role": "system", "content": "You are terse."},
        {"role": "developer", "content": "Answer in one sentence."},
        _user("Hi."),
        {"role": "assistant", "content": "Hello."},
        {"role": "user", "content": [{"type": "text", "text": "And the weather?"}, {"type": "text", "text": nonce_marker(context.nonce)}]},
    ]
    return ChatScenario(
        "system_and_history", ScenarioGroup.ORDINARY, _body(model, messages), _text_reply(context, model), ChatPersonaRole.CONFORMANT
    )


def max_tokens_at_worker_limit(context: ScenarioContext) -> ChatScenario:
    """``max_tokens`` equal to the card's ``max_completion_tokens``: the largest request a worker still takes."""
    model = context.models.conformant
    body = _body(model, [_user(f"Say hello. {nonce_marker(context.nonce)}")], max_tokens=context.worker_max_length)
    return ChatScenario("max_tokens_at_worker_limit", ScenarioGroup.ORDINARY, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT)


def max_tokens_over_worker_limit(context: ScenarioContext) -> ChatScenario:
    """One token above every worker's limit, the mistake clients make most: the first status read reports the exceeded
    completion limit, and the front cancels the request before a stream's headers with 400 ``no_worker_supports_limit``
    on ``max_tokens`` and no ``Retry-After``."""
    model = context.models.conformant
    body = _body(model, [_user(f"Say hello. {nonce_marker(context.nonce)}")], max_tokens=context.worker_max_length + 1)
    expectation = ErrorExpectation(
        status=400,
        codes=frozenset({NO_WORKER_SUPPORTS_LIMIT_CODE}),
        param="max_tokens",
        should_not_retry=True,
        retry_after=False,
    )
    return ChatScenario("max_tokens_over_worker_limit", ScenarioGroup.ORDINARY, body, expectation, ChatPersonaRole.CONFORMANT)


def stop_seed_and_penalties(context: ScenarioContext) -> ChatScenario:
    """Stop sequences, a seed and both penalties: forwarded only to a worker declaring ``seed`` and the penalties."""
    model = context.models.conformant
    body = _body(
        model,
        [_user(f"Count to three. {nonce_marker(context.nonce)}")],
        stop=["\nUser:", "###"],
        seed=42,
        presence_penalty=0.5,
        frequency_penalty=-0.5,
        temperature=0.0,
        top_p=1.0,
    )
    return ChatScenario("stop_seed_and_penalties", ScenarioGroup.ORDINARY, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT)


def sampling_at_bounds(context: ScenarioContext) -> ChatScenario:
    """Every sampling value at the edge of OpenAI's range, which the front and the native validation both accept."""
    model = context.models.conformant
    body = _body(
        model,
        [_user(f"Say hello. {nonce_marker(context.nonce)}")],
        temperature=2.0,
        top_p=1.0,
        presence_penalty=2.0,
        frequency_penalty=-2.0,
    )
    return ChatScenario("sampling_at_bounds", ScenarioGroup.ORDINARY, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT)


def stock_client_defaults(context: ScenarioContext) -> ChatScenario:
    """The keys stock clients send at their defaults (``n``, ``logprobs``, ``store``, empty ``tools``, a text format):
    the front drops them, so the request is served."""
    model = context.models.conformant
    body = _body(
        model,
        [_user(f"Say hello. {nonce_marker(context.nonce)}")],
        n=1,
        logprobs=False,
        top_logprobs=0,
        store=False,
        service_tier="auto",
        tools=[],
        tool_choice=None,
        parallel_tool_calls=None,
        response_format={"type": "text"},
        user="stress-user",
        metadata={"suite": "stress"},
    )
    return ChatScenario("stock_client_defaults", ScenarioGroup.ORDINARY, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT)


def unknown_nested_keys(context: ScenarioContext) -> ChatScenario:
    """Unknown keys inside a message and a content part: dropped, as OpenAI ignores them, never refused."""
    model = context.models.conformant
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": f"Say hello. {nonce_marker(context.nonce)}", "cache_control": {}}],
            "x_custom": 1,
        }
    ]
    return ChatScenario(
        "unknown_nested_keys", ScenarioGroup.ORDINARY, _body(model, messages), _text_reply(context, model), ChatPersonaRole.CONFORMANT
    )


def sdk_retry_hint(context: ScenarioContext) -> ChatScenario:
    """A request carrying ``x-stainless-retry-count``, as the OpenAI SDKs send on a retry; the front counts it in
    ``horde.oai.client_retry_hints``."""
    model = context.models.conformant
    body = _body(model, [_user(f"Say hello. {nonce_marker(context.nonce)}")])
    return ChatScenario(
        "sdk_retry_hint",
        ScenarioGroup.ORDINARY,
        body,
        _text_reply(context, model),
        ChatPersonaRole.CONFORMANT,
        extra_headers={"x-stainless-retry-count": "1"},
    )


def anonymous_plain(context: ScenarioContext) -> ChatScenario:
    """No ``Authorization`` header: served under the anonymous key, after requests from users with kudos."""
    model = context.models.conformant
    body = _body(model, [_user(f"Say hello. {nonce_marker(context.nonce)}")], max_tokens=MIN_CHAT_MAX_TOKENS)
    return ChatScenario(
        "anonymous_plain",
        ScenarioGroup.ORDINARY,
        body,
        _text_reply(context, model),
        ChatPersonaRole.CONFORMANT,
        authorization=AuthorizationMode.ANONYMOUS,
        kudos_upfront_benign=False,
    )


# Tool-using clients


def tools_auto(context: ScenarioContext) -> ChatScenario:
    """Tools under ``auto``: the reply calls the first tool, with null content and ``finish_reason`` ``tool_calls``."""
    model = context.models.conformant
    body = _tools_body(context, model, "What is the weather in Paris?", tool_choice="auto")
    expectation = _tool_reply(
        context, model, names=OFFERED_TOOL_NAMES, finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario("tools_auto", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


def tools_required(context: ScenarioContext) -> ChatScenario:
    """Tools under ``required``: needs ``tool_choice_enforced`` under ``strict``."""
    model = context.models.conformant
    body = _tools_body(context, model, "What is the weather in Paris?", tool_choice="required")
    expectation = _tool_reply(
        context, model, names=OFFERED_TOOL_NAMES, finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario("tools_required", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


def tools_forced_function(context: ScenarioContext) -> ChatScenario:
    """A forced ``get_time``: the worker receives only that tool under ``required``, and the reply finishes with ``stop``
    as OpenAI's does."""
    model = context.models.conformant
    body = _tools_body(context, model, "Hello there.", tool_choice=_forced_get_time())
    expectation = _tool_reply(
        context, model, names=frozenset({"get_time"}), finish_reasons=frozenset({"stop"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario("tools_forced_function", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


def tool_choice_none(context: ScenarioContext) -> ChatScenario:
    """Tools under ``none``: the worker receives no tools (it checks), so the reply is prose with no call."""
    model = context.models.conformant
    body = _tools_body(context, model, "What is the weather in Paris?", tool_choice="none")
    return ChatScenario("tool_choice_none", ScenarioGroup.TOOLS, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT)


def single_call_strict(context: ScenarioContext) -> ChatScenario:
    """``parallel_tool_calls: false`` with a worker answering two calls: ``strict`` keeps only the first."""
    model = context.models.conformant
    body = _body(
        model,
        [_scripted_user(context, "Weather in Paris and Rome?", ReplyScript.TWO_CALLS)],
        tools=[dict(GET_WEATHER_TOOL)],
        parallel_tool_calls=False,
    )
    expectation = _tool_reply(
        context, model, names=frozenset({"get_weather"}), finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario("single_call_strict", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


def single_call_best_effort(context: ScenarioContext) -> ChatScenario:
    """The same two-call reply under ``best_effort``: both calls are delivered."""
    model = context.models.conformant
    body = _body(
        model,
        [_scripted_user(context, "Weather in Paris and Rome?", ReplyScript.TWO_CALLS)],
        tools=[dict(GET_WEATHER_TOOL)],
        parallel_tool_calls=False,
        horde={"conformance": "best_effort"},
    )
    expectation = _tool_reply(
        context, model, names=frozenset({"get_weather"}), finish_reasons=frozenset({"tool_calls"}), calls=2, content_rule=ContentRule.NULL
    )
    return ChatScenario("single_call_best_effort", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


def tool_result_follow_up(context: ScenarioContext) -> ChatScenario:
    """The second request of a tool round, replayed as the OpenAI SDK replays it (its own fields included): prose."""
    model = context.models.conformant
    body = _body(model, _tool_round_history(context), tools=[dict(GET_WEATHER_TOOL), dict(GET_TIME_TOOL)])
    return ChatScenario("tool_result_follow_up", ScenarioGroup.TOOLS, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT)


def text_and_call(context: ScenarioContext) -> ChatScenario:
    """A reply with text and a call: the stream sends them in separate chunks, which Lite's parser needs."""
    model = context.models.conformant
    body = _body(model, [_scripted_user(context, "Check the weather.", ReplyScript.TEXT_AND_CALL)], tools=[dict(GET_WEATHER_TOOL)])
    expectation = _tool_reply(
        context, model, names=frozenset({"get_weather"}), finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.TEXT
    )
    return ChatScenario("text_and_call", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


def tools_with_response_format(context: ScenarioContext) -> ChatScenario:
    """Tools beside a response format: only a worker declaring ``tools_with_response_format`` serves it, and the call
    is exempt from the format."""
    model = context.models.conformant
    body = _tools_body(context, model, "What is the weather in Paris?", response_format={"type": "json_object"})
    expectation = _tool_reply(
        context, model, names=OFFERED_TOOL_NAMES, finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario("tools_with_response_format", ScenarioGroup.TOOLS, body, expectation, ChatPersonaRole.CONFORMANT)


# Structured output


def json_object_reply(context: ScenarioContext) -> ChatScenario:
    """``json_object``: the content is one JSON object."""
    model = context.models.conformant
    body = _body(model, [_user(f"Give a JSON object. {nonce_marker(context.nonce)}")], response_format={"type": "json_object"})
    return ChatScenario(
        "json_object_reply",
        ScenarioGroup.STRUCTURED,
        body,
        _text_reply(context, model, content_rule=ContentRule.JSON_OBJECT),
        ChatPersonaRole.CONFORMANT,
    )


def json_schema_strict(context: ScenarioContext) -> ChatScenario:
    """A ``strict`` schema with ``minimum``, ``required`` and ``additionalProperties``: the content validates against the
    schema as sent."""
    model = context.models.conformant
    body = _body(model, [_user(f"Rate a film as JSON. {nonce_marker(context.nonce)}")], response_format=_rating_format())
    expectation = CompletionExpectation(
        model=model,
        finish_reasons=frozenset({"stop"}),
        content_rule=ContentRule.JSON_SCHEMA,
        json_schema=RATING_SCHEMA,
        nonce=context.nonce,
    )
    return ChatScenario("json_schema_strict", ScenarioGroup.STRUCTURED, body, expectation, ChatPersonaRole.CONFORMANT)


def json_schema_at_nesting_limit(context: ScenarioContext) -> ChatScenario:
    """A schema nested exactly as deep as allowed: accepted, and the reply validates against it."""
    model = context.models.conformant
    schema = build_nested_object_schema(MAX_CHAT_SCHEMA_NESTING)
    response_format = {"type": "json_schema", "json_schema": {"name": "deep", "schema": schema, "strict": True}}
    body = _body(model, [_user(f"Fill the nested document. {nonce_marker(context.nonce)}")], response_format=response_format)
    expectation = CompletionExpectation(
        model=model, finish_reasons=frozenset({"stop"}), content_rule=ContentRule.JSON_SCHEMA, json_schema=schema, nonce=context.nonce
    )
    return ChatScenario("json_schema_at_nesting_limit", ScenarioGroup.STRUCTURED, body, expectation, ChatPersonaRole.CONFORMANT)


# Conformance modes


def nonconformant_schema_strict(context: ScenarioContext) -> ChatScenario:
    """A worker answering a ``strict`` schema with a document that breaks it: every reply is refused as
    ``ChatSubmitNonconformant``, the third fault faults the request, and the client gets 502 naming the refusal. The
    faults count as ``nonconformant_replies``, not aborted jobs."""
    model = context.models.conformant
    body = _body(model, [_scripted_user(context, "Rate a film as JSON.", ReplyScript.NONCONFORMANT)], response_format=_rating_format())
    return ChatScenario(
        "nonconformant_schema_strict", ScenarioGroup.CONFORMANCE, body, _faulted("ChatSubmitNonconformant"), ChatPersonaRole.CONFORMANT
    )


def nonconformant_schema_best_effort(context: ScenarioContext) -> ChatScenario:
    """The same reply under ``best_effort``: delivered as the worker wrote it."""
    model = context.models.conformant
    body = _body(
        model,
        [_scripted_user(context, "Rate a film as JSON.", ReplyScript.NONCONFORMANT)],
        response_format=_rating_format(),
        horde={"conformance": "best_effort"},
    )
    return ChatScenario(
        "nonconformant_schema_best_effort",
        ScenarioGroup.CONFORMANCE,
        body,
        _text_reply(context, model, content_rule=ContentRule.JSON_OBJECT),
        ChatPersonaRole.CONFORMANT,
    )


def prose_under_forced_strict(context: ScenarioContext) -> ChatScenario:
    """Prose where a forced function was required: refused under ``strict``, faulted after three replies, 502."""
    model = context.models.conformant
    body = _body(
        model,
        [_scripted_user(context, "Hello.", ReplyScript.PROSE)],
        tools=[dict(GET_WEATHER_TOOL), dict(GET_TIME_TOOL)],
        tool_choice=_forced_get_time(),
    )
    return ChatScenario(
        "prose_under_forced_strict", ScenarioGroup.CONFORMANCE, body, _faulted("ChatSubmitNonconformant"), ChatPersonaRole.CONFORMANT
    )


def prose_under_forced_best_effort(context: ScenarioContext) -> ChatScenario:
    """The same prose under ``best_effort``: delivered with no call."""
    model = context.models.conformant
    body = _body(
        model,
        [_scripted_user(context, "Hello.", ReplyScript.PROSE)],
        tools=[dict(GET_WEATHER_TOOL), dict(GET_TIME_TOOL)],
        tool_choice=_forced_get_time(),
        horde={"conformance": "best_effort"},
    )
    return ChatScenario(
        "prose_under_forced_best_effort", ScenarioGroup.CONFORMANCE, body, _text_reply(context, model), ChatPersonaRole.CONFORMANT
    )


def undeclared_function_call(context: ScenarioContext) -> ChatScenario:
    """A call to a function the client did not offer: refused as ``ChatSubmitInvalid`` under every mode, and each fault
    counts as an aborted job, so the run budgets it."""
    model = context.models.conformant
    body = _body(
        model,
        [_scripted_user(context, "Weather?", ReplyScript.UNDECLARED)],
        tools=[dict(GET_WEATHER_TOOL)],
        horde={"conformance": "best_effort"},
    )
    return ChatScenario(
        "undeclared_function_call",
        ScenarioGroup.CONFORMANCE,
        body,
        _faulted("ChatSubmitInvalid"),
        ChatPersonaRole.CONFORMANT,
        aborts_worker_jobs=True,
    )


def duplicate_call_ids(context: ScenarioContext) -> ChatScenario:
    """Two calls sharing an id, which no client can answer: refused as ``ChatSubmitInvalid`` under every mode."""
    model = context.models.conformant
    body = _body(model, [_scripted_user(context, "Weather?", ReplyScript.DUP_IDS)], tools=[dict(GET_WEATHER_TOOL)])
    return ChatScenario(
        "duplicate_call_ids",
        ScenarioGroup.CONFORMANCE,
        body,
        _faulted("ChatSubmitInvalid"),
        ChatPersonaRole.CONFORMANT,
        aborts_worker_jobs=True,
    )


def worker_fault_reason(context: ScenarioContext) -> ChatScenario:
    """A worker faulting every attempt with ``generation_error``: the 502 ends with the Horde's sentence for it and
    ``metadata.rc`` is ``ChatWorkerFault``."""
    model = context.models.conformant
    body = _body(model, [_scripted_user(context, "Say hello.", ReplyScript.FAULT_GENERATION_ERROR)])
    return ChatScenario(
        "worker_fault_reason",
        ScenarioGroup.CONFORMANCE,
        body,
        _faulted("ChatWorkerFault", message_fragments=(WORKER_FAULT_GENERATION_ERROR_MESSAGE,)),
        ChatPersonaRole.CONFORMANT,
        aborts_worker_jobs=True,
    )


def flaky_schema_strict(context: ScenarioContext) -> ChatScenario:
    """A ``strict`` schema on the flaky worker: whatever it submits, a delivered reply matches the schema, because the
    Horde refuses the ones that do not; three bad replies in a row fault the request."""
    model = context.models.flaky
    body = _body(model, [_user(f"Rate a film as JSON. {nonce_marker(context.nonce)}")], response_format=_rating_format())
    expectation = CompletionExpectation(
        model=model,
        finish_reasons=frozenset({"stop"}),
        content_rule=ContentRule.JSON_SCHEMA,
        json_schema=RATING_SCHEMA,
        nonce=context.nonce,
    )
    return ChatScenario(
        "flaky_schema_strict",
        ScenarioGroup.CONFORMANCE,
        body,
        expectation,
        ChatPersonaRole.FLAKY,
        alternative_error=_faulted("ChatSubmitNonconformant"),
    )


def flaky_required_strict(context: ScenarioContext) -> ChatScenario:
    """``tool_choice: required`` on the flaky worker: a delivered reply always carries a call."""
    model = context.models.flaky
    body = _tools_body(context, model, "What is the weather in Paris?", tool_choice="required")
    expectation = _tool_reply(
        context, model, names=OFFERED_TOOL_NAMES, finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario(
        "flaky_required_strict",
        ScenarioGroup.CONFORMANCE,
        body,
        expectation,
        ChatPersonaRole.FLAKY,
        alternative_error=_faulted("ChatSubmitNonconformant"),
    )


def flaky_schema_best_effort(context: ScenarioContext) -> ChatScenario:
    """A schema on the flaky worker under ``best_effort``: delivered whatever the worker wrote."""
    model = context.models.flaky
    body = _body(
        model,
        [_user(f"Rate a film as JSON. {nonce_marker(context.nonce)}")],
        response_format=_rating_format(),
        horde={"conformance": "best_effort"},
    )
    return ChatScenario(
        "flaky_schema_best_effort",
        ScenarioGroup.CONFORMANCE,
        body,
        _text_reply(context, model, content_rule=ContentRule.TEXT),
        ChatPersonaRole.FLAKY,
    )


# Declaration gaps on the limited persona


def limited_plain_strict(context: ScenarioContext) -> ChatScenario:
    """A plain ``strict`` request to the limited worker: served, since it declares ``prompt_overflow_refused``."""
    model = context.models.limited
    return ChatScenario(
        "limited_plain_strict",
        ScenarioGroup.GAP,
        _body(model, [_user(f"Say hello. {nonce_marker(context.nonce)}")]),
        _text_reply(context, model),
        ChatPersonaRole.LIMITED,
    )


def gap_forced_strict(context: ScenarioContext) -> ChatScenario:
    """A forced function under ``strict`` where no worker declares ``tool_choice_enforced``: 404 at once, naming it."""
    model = context.models.limited
    body = _tools_body(context, model, "Hello.", tool_choice=_forced_get_time(), temperature=LOW_TOOL_TEMPERATURE)
    return ChatScenario("gap_forced_strict", ScenarioGroup.GAP, body, _declaration_gap("tool_choice_enforced"), ChatPersonaRole.LIMITED)


def gap_forced_best_effort(context: ScenarioContext) -> ChatScenario:
    """The same forced function under ``best_effort``: no guarantee is required, so the limited worker serves it."""
    model = context.models.limited
    body = _tools_body(
        context, model, "Hello.", tool_choice=_forced_get_time(), temperature=LOW_TOOL_TEMPERATURE, horde={"conformance": "best_effort"}
    )
    expectation = _tool_reply(
        context, model, names=frozenset({"get_time"}), finish_reasons=frozenset({"stop"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario("gap_forced_best_effort", ScenarioGroup.GAP, body, expectation, ChatPersonaRole.LIMITED)


def gap_tool_temperature(context: ScenarioContext) -> ChatScenario:
    """Tools above the worker's ``tools_max_temperature``: 404 under either mode, since a clamped value changes the
    request."""
    model = context.models.limited
    body = _tools_body(context, model, "Weather?", temperature=HIGH_TOOL_TEMPERATURE, horde={"conformance": "best_effort"})
    return ChatScenario("gap_tool_temperature", ScenarioGroup.GAP, body, _declaration_gap("tools_max_temperature"), ChatPersonaRole.LIMITED)


def gap_json_object(context: ScenarioContext) -> ChatScenario:
    """``json_object`` where no worker declares it: a feature, so 404 under either mode."""
    model = context.models.limited
    body = _body(
        model,
        [_user(f"Give JSON. {nonce_marker(context.nonce)}")],
        response_format={"type": "json_object"},
        horde={"conformance": "best_effort"},
    )
    return ChatScenario("gap_json_object", ScenarioGroup.GAP, body, _declaration_gap("json_object"), ChatPersonaRole.LIMITED)


# Clients doing it wrong


def unsupported_top_level_key(context: ScenarioContext) -> ChatScenario:
    """``logit_bias``: an unserved top-level key is refused by name, never dropped."""
    body = _body(context.models.conformant, [_user("Hi.")], logit_bias={"50256": -100})
    return ChatScenario(
        "unsupported_top_level_key",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="logit_bias"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def tool_choice_without_tools(context: ScenarioContext) -> ChatScenario:
    """``tool_choice`` without ``tools``: refused as OpenAI refuses it."""
    body = _body(context.models.conformant, [_user("Hi.")], tool_choice="required")
    return ChatScenario(
        "tool_choice_without_tools",
        ScenarioGroup.MISUSE,
        body,
        _refused(REQUEST_CONFLICT_CODE, param="tool_choice"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def parallel_calls_without_tools(context: ScenarioContext) -> ChatScenario:
    """``parallel_tool_calls`` without ``tools``: refused as OpenAI refuses it."""
    body = _body(context.models.conformant, [_user("Hi.")], parallel_tool_calls=False)
    expectation = _refused(REQUEST_CONFLICT_CODE, param="parallel_tool_calls")
    return ChatScenario("parallel_calls_without_tools", ScenarioGroup.MISUSE, body, expectation, None, stream_mode=StreamMode.NEVER)


def temperature_out_of_range(context: ScenarioContext) -> ChatScenario:
    """``temperature`` 2.5: refused with OpenAI's message and ``param``."""
    body = _body(context.models.conformant, [_user("Hi.")], temperature=2.5)
    return ChatScenario(
        "temperature_out_of_range",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="temperature"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def max_tokens_below_floor(context: ScenarioContext) -> ChatScenario:
    """``max_tokens`` below the Horde floor of 16: refused, never raised."""
    body = _body(context.models.conformant, [_user("Hi.")], max_tokens=MIN_CHAT_MAX_TOKENS - 1)
    return ChatScenario(
        "max_tokens_below_floor",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="max_tokens"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def two_choices(context: ScenarioContext) -> ChatScenario:
    """``n`` 2: one request yields one choice, so more is refused."""
    body = _body(context.models.conformant, [_user("Hi.")], n=2)
    return ChatScenario(
        "two_choices", ScenarioGroup.MISUSE, body, _refused(UNSUPPORTED_PARAMETER_CODE, param="n"), None, stream_mode=StreamMode.NEVER
    )


def differing_token_limits(context: ScenarioContext) -> ChatScenario:
    """``max_tokens`` and ``max_completion_tokens`` that differ: contradictory, refused."""
    body = _body(context.models.conformant, [_user("Hi.")], max_completion_tokens=REPLY_TOKENS + 1)
    return ChatScenario(
        "differing_token_limits",
        ScenarioGroup.MISUSE,
        body,
        _refused(REQUEST_CONFLICT_CODE, param="max_completion_tokens"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def too_many_messages(context: ScenarioContext) -> ChatScenario:
    """One message more than the limit: refused before validation of the messages themselves."""
    messages = [
        _user("Hi.") if message_index % 2 == 0 else {"role": "assistant", "content": "Hello."}
        for message_index in range(MAX_CHAT_MESSAGE_COUNT + 1)
    ]
    return ChatScenario(
        "too_many_messages",
        ScenarioGroup.MISUSE,
        _body(context.models.conformant, messages),
        _refused(UNSUPPORTED_PARAMETER_CODE, param="messages"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def message_text_over_limit(context: ScenarioContext) -> ChatScenario:
    """Message text one byte above 1 MiB: refused, though the body is far below the byte limit."""
    body = _body(context.models.conformant, [_user("a" * (MAX_CHAT_TEXT_BYTES + 1))])
    return ChatScenario(
        "message_text_over_limit",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="messages"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def oversized_body(context: ScenarioContext) -> ChatScenario:
    """A body one byte above the front's default byte limit: 413 before it is parsed. The client sends a prepared body
    of that size in place of this one, and the run budgets it."""
    return ChatScenario(
        "oversized_body",
        ScenarioGroup.MISUSE,
        _body(context.models.conformant, [_user("Hi.")]),
        # The 413 is refused from the Content-Length before any byte of the body is parsed, so no path is at fault.
        _refused("RequestTooLarge", status=413, param=None),
        None,
        stream_mode=StreamMode.NEVER,
        oversized_body=True,
    )


def tool_result_without_call(context: ScenarioContext) -> ChatScenario:
    """A tool message no assistant call precedes: refused with OpenAI's message at the message's role."""
    messages = [_user("Hi."), {"role": "tool", "tool_call_id": "call_missing", "content": "18C"}]
    body = _body(context.models.conformant, messages, tools=[dict(GET_WEATHER_TOOL)])
    return ChatScenario(
        "tool_result_without_call",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="messages[1].role"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def unanswered_tool_calls(context: ScenarioContext) -> ChatScenario:
    """A history ending with calls nobody answered: refused at ``messages``."""
    messages = [
        _user("Weather?"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_open", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        },
    ]
    body = _body(context.models.conformant, messages, tools=[dict(GET_WEATHER_TOOL)])
    return ChatScenario(
        "unanswered_tool_calls",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="messages"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def tool_calls_on_user_message(context: ScenarioContext) -> ChatScenario:
    """``tool_calls`` on a user message: refused at its path, as OpenAI refuses it."""
    messages = [
        {
            "role": "user",
            "content": "Hi.",
            "tool_calls": [{"id": "call_x", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        }
    ]
    body = _body(context.models.conformant, messages)
    expectation = _refused(UNSUPPORTED_PARAMETER_CODE, param="messages[0].tool_calls")
    return ChatScenario("tool_calls_on_user_message", ScenarioGroup.MISUSE, body, expectation, None, stream_mode=StreamMode.NEVER)


def strict_schema_open_object(context: ScenarioContext) -> ChatScenario:
    """A ``strict`` schema without ``additionalProperties: false``: refused with OpenAI's message."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    response_format = {"type": "json_schema", "json_schema": {"name": "open", "schema": schema, "strict": True}}
    body = _body(context.models.conformant, [_user("Hi.")], response_format=response_format)
    expectation = _refused(UNSUPPORTED_PARAMETER_CODE, param="response_format")
    return ChatScenario("strict_schema_open_object", ScenarioGroup.MISUSE, body, expectation, None, stream_mode=StreamMode.NEVER)


def schema_nested_too_deep(context: ScenarioContext) -> ChatScenario:
    """A schema one level deeper than allowed: refused, ``strict`` or not."""
    schema = build_nested_object_schema(MAX_CHAT_SCHEMA_NESTING + 1)
    response_format = {"type": "json_schema", "json_schema": {"name": "too_deep", "schema": schema}}
    body = _body(context.models.conformant, [_user("Hi.")], response_format=response_format)
    return ChatScenario(
        "schema_nested_too_deep",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="response_format.json_schema.schema"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def json_schema_without_schema(context: ScenarioContext) -> ChatScenario:
    """A ``json_schema`` format with no ``schema``: a malformed body."""
    body = _body(context.models.conformant, [_user("Hi.")], response_format={"type": "json_schema", "json_schema": {"name": "missing"}})
    return ChatScenario(
        "json_schema_without_schema",
        ScenarioGroup.MISUSE,
        body,
        _refused(BAD_REQUEST_CODE, param="response_format.json_schema.schema"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def empty_messages(context: ScenarioContext) -> ChatScenario:
    """An empty ``messages``: refused with OpenAI's message."""
    body = _body(context.models.conformant, [])
    return ChatScenario(
        "empty_messages", ScenarioGroup.MISUSE, body, _refused(BAD_REQUEST_CODE, param="messages"), None, stream_mode=StreamMode.NEVER
    )


def model_variant_suffix(context: ScenarioContext) -> ChatScenario:
    """A model id with ``:``, reserved for variants none of which is served: 404 ``model_not_found``."""
    body = _body(f"{context.models.conformant}:fast", [_user("Hi.")])
    return ChatScenario(
        "model_variant_suffix",
        ScenarioGroup.MISUSE,
        body,
        _refused("model_not_found", status=404, param="model"),
        None,
        stream_mode=StreamMode.NEVER,
    )


def unknown_model(context: ScenarioContext) -> ChatScenario:
    """A model neither the reference lists nor any worker serves: the Horde's 404 ``UnknownChatModel``, passed through."""
    body = _body(f"stress/no-such-model-{context.nonce}", [_user("Hi.")])
    # The Horde refuses before it holds anything, and its refusal names no body path, so no param and no x-should-retry.
    expectation = ErrorExpectation(status=404, codes=frozenset({"UnknownChatModel"}), param=None, should_not_retry=False)
    return ChatScenario("unknown_model", ScenarioGroup.MISUSE, body, expectation, None, stream_mode=StreamMode.NEVER)


def malformed_authorization(context: ScenarioContext) -> ChatScenario:
    """An ``Authorization`` header that is not ``Bearer <key>``: 401 before anything else."""
    body = _body(context.models.conformant, [_user("Hi.")])
    return ChatScenario(
        "malformed_authorization",
        ScenarioGroup.MISUSE,
        body,
        # The header, not a body path, is at fault, so the front sends no param.
        _refused("InvalidAuthorizationHeader", status=401, param=None),
        None,
        stream_mode=StreamMode.NEVER,
        authorization=AuthorizationMode.MALFORMED,
    )


def unknown_api_key(context: ScenarioContext) -> ChatScenario:
    """A bearer key no user owns: the Horde's 401 ``InvalidAPIKey``, passed through."""
    body = _body(context.models.conformant, [_user("Hi.")])
    # The Horde refuses before it holds anything, and its refusal names no body path, so no param and no x-should-retry.
    expectation = ErrorExpectation(status=401, codes=frozenset({"InvalidAPIKey"}), param=None, should_not_retry=False)
    return ChatScenario(
        "unknown_api_key",
        ScenarioGroup.MISUSE,
        body,
        expectation,
        None,
        stream_mode=StreamMode.NEVER,
        authorization=AuthorizationMode.UNKNOWN_KEY,
    )


def native_only_horde_key(context: ScenarioContext) -> ChatScenario:
    """``horde.operation_key`` on the front, which only the front itself may send to the native route: refused."""
    body = _body(context.models.conformant, [_user("Hi.")], horde={"operation_key": "8a5a5c51-6c38-4a3e-9a51-0f2a0d0f6a11"})
    return ChatScenario(
        "native_only_horde_key",
        ScenarioGroup.MISUSE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="horde.operation_key"),
        None,
        stream_mode=StreamMode.NEVER,
    )


# KoboldAI Lite


def _lite_headers() -> dict[str, str]:
    """Return the headers a browser running Lite adds."""
    return {"Origin": LITE_ORIGIN}


def _lite_body(model: str, messages: list[dict[str, Any]], **request_keys: Any) -> dict[str, Any]:
    """Create a body with Lite's keys: its sampler sliders, stop strings and a presence penalty above zero."""
    return {
        "max_tokens": 200,
        "model": model,
        "temperature": 0.7,
        "top_p": 0.92,
        "stop": ["\nUser:", "\n### Instruction:"],
        "presence_penalty": 0.1,
        "messages": messages,
        **request_keys,
    }


def lite_plain_turn(context: ScenarioContext) -> ChatScenario:
    """A chat turn exactly as Lite builds it, streamed from a browser origin."""
    model = context.models.conformant
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        _user(f"Say hello in five words. {nonce_marker(context.nonce)}"),
    ]
    return ChatScenario(
        "lite_plain_turn",
        ScenarioGroup.LITE,
        _lite_body(model, messages),
        _text_reply(context, model),
        ChatPersonaRole.CONFORMANT,
        stream_mode=StreamMode.LITE_STREAM,
        extra_headers=_lite_headers(),
    )


def lite_tool_turn(context: ScenarioContext) -> ChatScenario:
    """An MCP tool turn as Lite sends it: the call arrives in a chunk without content, which is where Lite reads it."""
    model = context.models.conformant
    body = _lite_body(
        model, [_user(f"What's the weather in Paris? Use the tool. {nonce_marker(context.nonce)}")], tools=[dict(GET_WEATHER_TOOL)]
    )
    expectation = _tool_reply(
        context, model, names=frozenset({"get_weather"}), finish_reasons=frozenset({"tool_calls"}), calls=1, content_rule=ContentRule.NULL
    )
    return ChatScenario(
        "lite_tool_turn",
        ScenarioGroup.LITE,
        body,
        expectation,
        ChatPersonaRole.CONFORMANT,
        stream_mode=StreamMode.LITE_STREAM,
        extra_headers=_lite_headers(),
    )


def lite_tool_follow_up(context: ScenarioContext) -> ChatScenario:
    """Lite's replay of the call with ``content: null`` and the raw tool message: prose."""
    model = context.models.conformant
    messages = [
        _user(f"What's the weather in Paris? Use the tool. {nonce_marker(context.nonce)}"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_lite", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}],
        },
        {"role": "tool", "tool_call_id": "call_lite", "content": '{"temperature_c": 18, "sky": "sunny"}'},
    ]
    return ChatScenario(
        "lite_tool_follow_up",
        ScenarioGroup.LITE,
        _lite_body(model, messages, tools=[dict(GET_WEATHER_TOOL)]),
        _text_reply(context, model),
        ChatPersonaRole.CONFORMANT,
        stream_mode=StreamMode.LITE_STREAM,
        extra_headers=_lite_headers(),
    )


def lite_sampler_extras(context: ScenarioContext) -> ChatScenario:
    """Lite's opt-in "send non-standard params": Kobold sampler keys, refused by name, with the CORS header so the
    browser can show the error."""
    body = _lite_body(context.models.conformant, [_user("Hi.")], rep_pen=1.1)
    return ChatScenario(
        "lite_sampler_extras",
        ScenarioGroup.LITE,
        body,
        _refused(UNSUPPORTED_PARAMETER_CODE, param="rep_pen"),
        None,
        stream_mode=StreamMode.NEVER,
        extra_headers=_lite_headers(),
    )


# Vision


def vision_base64_image(context: ScenarioContext) -> ChatScenario:
    """A user message with a PNG data URL: the Horde normalises it to JPEG, and the base64 worker checks it receives a
    JPEG data URL, answering ``vision-ok`` only then."""
    model = context.models.conformant
    content = [
        {"type": "text", "text": f"Describe the image. {nonce_marker(context.nonce)}"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_TINY_PNG_B64}", "detail": "low"}},
    ]
    return ChatScenario(
        "vision_base64_image",
        ScenarioGroup.VISION,
        _body(model, [{"role": "user", "content": content}]),
        CompletionExpectation(model=model, finish_reasons=frozenset({"stop"}), nonce=context.nonce, content_fragment=VISION_OK_PREFIX),
        ChatPersonaRole.CONFORMANT,
    )


ScenarioBuilder = Callable[[ScenarioContext], ChatScenario]
"""A function building one scenario."""

SCENARIO_BUILDERS: Mapping[ScenarioGroup, tuple[ScenarioBuilder, ...]] = {
    ScenarioGroup.ORDINARY: (
        plain_prompt,
        system_and_history,
        max_tokens_at_worker_limit,
        max_tokens_over_worker_limit,
        stop_seed_and_penalties,
        sampling_at_bounds,
        stock_client_defaults,
        unknown_nested_keys,
        sdk_retry_hint,
        anonymous_plain,
    ),
    ScenarioGroup.TOOLS: (
        tools_auto,
        tools_required,
        tools_forced_function,
        tool_choice_none,
        single_call_strict,
        single_call_best_effort,
        tool_result_follow_up,
        text_and_call,
        tools_with_response_format,
    ),
    ScenarioGroup.STRUCTURED: (json_object_reply, json_schema_strict, json_schema_at_nesting_limit),
    ScenarioGroup.CONFORMANCE: (
        nonconformant_schema_strict,
        nonconformant_schema_best_effort,
        prose_under_forced_strict,
        prose_under_forced_best_effort,
        undeclared_function_call,
        duplicate_call_ids,
        worker_fault_reason,
        flaky_schema_strict,
        flaky_required_strict,
        flaky_schema_best_effort,
    ),
    ScenarioGroup.GAP: (limited_plain_strict, gap_forced_strict, gap_forced_best_effort, gap_tool_temperature, gap_json_object),
    ScenarioGroup.MISUSE: (
        unsupported_top_level_key,
        tool_choice_without_tools,
        parallel_calls_without_tools,
        temperature_out_of_range,
        max_tokens_below_floor,
        two_choices,
        differing_token_limits,
        too_many_messages,
        message_text_over_limit,
        oversized_body,
        tool_result_without_call,
        unanswered_tool_calls,
        tool_calls_on_user_message,
        strict_schema_open_object,
        schema_nested_too_deep,
        json_schema_without_schema,
        empty_messages,
        model_variant_suffix,
        unknown_model,
        malformed_authorization,
        unknown_api_key,
        native_only_horde_key,
    ),
    ScenarioGroup.LITE: (lite_plain_turn, lite_tool_turn, lite_tool_follow_up, lite_sampler_extras),
    ScenarioGroup.VISION: (vision_base64_image,),
}
"""Every scenario builder, by group."""

OVERSIZED_BODY_BYTES: int = MAX_CHAT_BODY_BYTES + 1
"""The size of the body the ``oversized_body`` scenario sends: one byte above the front's default limit."""


ANONYMOUS_SHARE_GROUPS: frozenset[ScenarioGroup] = frozenset({ScenarioGroup.ORDINARY, ScenarioGroup.LITE})
"""The groups whose scenarios a run also sends anonymously, at ``--chat-mix-anonymous-share``."""

UPFRONT_KUDOS_FREE_TOKENS: int = 512
"""The most tokens a request may ask for without the Horde ever asking upfront kudos: the default floor of its free
output allowance (``TextWaitingPrompt.require_upfront_kudos``, between 512 and 4096 tokens by default for requests that
allow slow workers, as chat requests do by default, depending on the load on the request's eligible workers). Above it
an anonymous request, which holds no kudos, may get 403 ``KudosUpfront``; at or below it the refusal would break the
contract."""

NATIVE_DEFAULT_MAX_TOKENS: int = 512
"""The ``max_tokens`` a chat request gets when it sends none."""

FAIL_FAST_SHARE_GROUPS: frozenset[ScenarioGroup] = frozenset(
    {
        ScenarioGroup.ORDINARY,
        ScenarioGroup.TOOLS,
        ScenarioGroup.STRUCTURED,
        ScenarioGroup.CONFORMANCE,
        ScenarioGroup.GAP,
        ScenarioGroup.VISION,
    }
)
"""The groups whose scenarios a run also sends under ``horde.availability_mode: fail_fast``, at
``--chat-mix-fail-fast-share``. The misuse group is refused before anything is queued, so the mode never applies to it;
the Lite group's bodies keep the shape Lite sends."""

FAIL_FAST_AVAILABILITY_MODE: str = "fail_fast"
"""The ``horde.availability_mode`` value that refuses a request the online pool cannot serve within its budget."""


def as_anonymous(scenario: ChatScenario) -> ChatScenario:
    """Return the scenario sent without ``Authorization``, under the anonymous key.

    The front treats a missing header as the anonymous key and nothing else differs on the wire
    (``docs/decisions/0035-keep-anonymous-access-on-the-chat-front.md``), so the expected outcome stays the scenario's.
    What changes is the Horde's side: the anonymous user is ranked last and holds no kudos, so a request above
    ``UPFRONT_KUDOS_FREE_TOKENS`` may be refused with 403 ``KudosUpfront`` (benign), one at or below it never may; and
    every anonymous client shares one concurrency limit, whose 429 ``TooManyPrompts`` is benign like any rate limit.

    Args:
        scenario: A scenario that authenticates with the requestor key.
    """
    requested_tokens = scenario.body.get("max_tokens") or scenario.body.get("max_completion_tokens") or NATIVE_DEFAULT_MAX_TOKENS
    return dataclasses.replace(
        scenario,
        name=f"{scenario.name} anonymous",
        authorization=AuthorizationMode.ANONYMOUS,
        kudos_upfront_benign=requested_tokens > UPFRONT_KUDOS_FREE_TOKENS,
    )


def as_fail_fast(scenario: ChatScenario) -> ChatScenario:
    """Return the scenario sent with ``horde.availability_mode: fail_fast``.

    The mode changes only the first status read: a pool with zero eligible worker threads, or a wait estimate over the
    budget, ends the request at once with 503 ``queue_unavailable`` (``docs/reference/chat_requests.md``). Any other
    read leaves the request on its way, so the expected outcome stays the scenario's and the refusal is allowed beside
    it.

    Args:
        scenario: A scenario of one of ``FAIL_FAST_SHARE_GROUPS``.
    """
    body = copy.deepcopy(scenario.body)
    body["horde"] = {**(body.get("horde") or {}), "availability_mode": FAIL_FAST_AVAILABILITY_MODE}
    return dataclasses.replace(scenario, name=f"{scenario.name} fail_fast", body=body, fail_fast=True)


def pick_scenario_builder(group: ScenarioGroup, *, rng: random.Random) -> ScenarioBuilder:
    """Return a builder of the group, uniformly at random.

    Args:
        group: The group.
        rng: The random source.
    """
    return rng.choice(SCENARIO_BUILDERS[group])
