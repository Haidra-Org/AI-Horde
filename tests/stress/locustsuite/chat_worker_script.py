# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Decide what a simulated chat worker declares and replies, and check the jobs it receives, without a GPU.

A client scenario that needs a particular worker behaviour writes a reply marker, ``[reply:<script>]``, into its last
user message; the worker reads it from the popped payload and answers as scripted, so the client knows the outcome it
caused. Without a marker the worker answers as a well-behaved backend would. Every reply echoes the request's nonce
(``[nonce:<hex>]`` in a user message) into its text, JSON document or call arguments, so a client detects a reply
delivered to the wrong request. The pop and submit bodies come from ``tests/integration/_fake_chat_worker.py``, the fake
worker the integration suite uses, so both speak one contract.

- [`ChatPersonaRole`][tests.stress.locustsuite.chat_worker_script.ChatPersonaRole] and
  [`build_persona`][tests.stress.locustsuite.chat_worker_script.build_persona]: the three declarations the suite runs, a
  conformant worker, a limited one that leaves out guarantees and features and caps the tool temperature, and a flaky
  one that sometimes breaks the request's contract.
- [`nonce_marker`][tests.stress.locustsuite.chat_worker_script.nonce_marker] and
  [`find_request_nonce`][tests.stress.locustsuite.chat_worker_script.find_request_nonce]: how a client tags a request
  and the worker finds the tag.
- [`build_scripted_reply`][tests.stress.locustsuite.chat_worker_script.build_scripted_reply] and
  [`build_misbehaving_reply`][tests.stress.locustsuite.chat_worker_script.build_misbehaving_reply]: the reply to a popped
  chat payload.
- [`find_job_payload_violations`][tests.stress.locustsuite.chat_worker_script.find_job_payload_violations]: what a popped
  payload breaks of the routing and dispatch rules, checked against the worker's own declaration.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from horde_sdk.generation_parameters.text.chat_limits import CHAT_SAMPLING_DEFAULTS
from horde_sdk.generation_parameters.text.chat_policy import ChatFeature, ChatGuarantee
from horde_sdk.worker.chat_declaration import ChatFaultReason, ChatFinishReason, ChatImageTransport

__all__ = [
    "DEFAULT_PERSONA_MAX_LENGTH",
    "ChatPersona",
    "ChatPersonaRole",
    "ReplyScript",
    "WorkerReply",
    "build_misbehaving_reply",
    "build_persona",
    "build_scripted_reply",
    "find_job_payload_violations",
    "find_reply_script",
    "find_request_nonce",
    "nonce_marker",
    "reply_marker",
]


class ReplyScript(StrEnum):
    """A reply a client asks the simulated worker for with ``[reply:<value>]`` in its last user message."""

    TWO_CALLS = "two_calls"
    """Two calls to the first offered tool, with distinct ids; one survives ``parallel_tool_calls: false``."""
    UNDECLARED = "undeclared"
    """A call to a function the request did not offer: refused as ``ChatSubmitInvalid`` under every mode."""
    DUP_IDS = "dup_ids"
    """Two calls sharing one id: refused as ``ChatSubmitInvalid`` under every mode."""
    PROSE = "prose"
    """Prose with no call, which breaks a forced ``tool_choice`` under ``strict``."""
    NONCONFORMANT = "nonconformant"
    """``{"x": 0}``, which breaks any schema with other required keys under ``strict``."""
    TEXT_AND_CALL = "text_and_call"
    """Text and one call in the same reply."""
    FAULT_GENERATION_ERROR = "fault_generation_error"
    """A faulted submit with ``fault_reason`` ``generation_error`` instead of a reply."""


REPLY_MARKER_PATTERN: re.Pattern[str] = re.compile(r"\[reply:([a-z_]+)\]")
"""Find the reply marker in a user message."""

NONCE_MARKER_PATTERN: re.Pattern[str] = re.compile(r"\[nonce:([0-9a-f]+)\]")
"""Find a request's nonce in a user message."""

PLAIN_REPLY: str = "Sunny and mild, a stress test reply."
"""The text of a well-behaved reply to a request without tools or a response format, before the nonce."""

PROSE_REPLY: str = "Sunny."
"""The text of a scripted prose reply, before the nonce."""

TEXT_BEFORE_CALL: str = "Checking."
"""The text a ``text_and_call`` reply carries beside its call, before the nonce."""

NOT_JSON_REPLY: str = "This is not JSON."
"""The content a misbehaving worker returns for a response format, before the nonce."""

UNOFFERED_FUNCTION_NAME: str = "not_offered"
"""The function name an ``undeclared`` reply calls."""

VISION_OK_PREFIX: str = "vision-ok"
"""The start of the reply to a job whose images all arrived as JPEG data URLs; the vision scenario requires it."""

VISION_UNEXPECTED_PREFIX: str = "vision-unexpected"
"""The start of the reply to a job whose images arrived in another shape."""

JPEG_DATA_URL_PREFIX: str = "data:image/jpeg;base64,"
"""The data URL prefix a ``base64`` transport worker receives images under; the Horde stores every image as JPEG."""

JPEG_MAGIC: bytes = b"\xff\xd8\xff"
"""The first bytes of every JPEG file."""

WORKER_MESSAGE_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})
"""The roles a chat job carries; ``developer`` arrives as ``system``."""

DISPATCHED_TOOL_CHOICES: frozenset[str] = frozenset({"auto", "required"})
"""The ``tool_choice`` values a worker may receive: ``none`` is never sent and a function choice arrives as ``required``."""

DEFAULT_PERSONA_MAX_LENGTH: int = 512
"""The chat output limit every persona declares; a ``max_tokens`` above it has no worker."""

DEFAULT_PERSONA_MAX_CONTEXT_LENGTH: int = 8192
"""The chat context limit every persona declares."""

LIMITED_TOOLS_MAX_TEMPERATURE: float = 0.5
"""The tool temperature limit of the limited persona; a tools request above it has no worker under either mode."""


class ChatPersonaRole(StrEnum):
    """Which declaration a simulated chat worker pops with."""

    CONFORMANT = "conformant"
    """Every feature and guarantee, chat jobs only; answers every scenario as scripted."""
    LIMITED = "limited"
    """``seed`` and ``tools`` with only ``prompt_overflow_refused``, a tool temperature limit, and text jobs as well."""
    FLAKY = "flaky"
    """Every feature but ``vision`` and ``tools_with_response_format`` and every guarantee, chat jobs only; sometimes breaks a reply's
    contract so refusal accounting and the fault cap run at volume."""


@dataclass(frozen=True)
class ChatPersona:
    """Represents the chat declaration one simulated worker pops with."""

    role: ChatPersonaRole
    """The persona."""
    features: tuple[str, ...]
    """The declared features."""
    guarantees: tuple[str, ...]
    """The declared guarantees."""
    tools_max_temperature: float | None
    """The declared tool temperature limit, or None for none."""
    prompt_jobs: bool
    """Whether the worker also accepts text jobs."""
    image_transports: tuple[str, ...]
    """The declared image transports; empty without vision."""
    chat_max_length: int = DEFAULT_PERSONA_MAX_LENGTH
    """The declared chat output limit."""
    chat_max_context_length: int = DEFAULT_PERSONA_MAX_CONTEXT_LENGTH
    """The declared chat context limit."""


def build_persona(role: ChatPersonaRole, *, vision: bool) -> ChatPersona:
    """Create the declaration of a persona.

    Args:
        role: The persona.
        vision: Whether the conformant persona also declares ``vision`` with the ``base64`` transport. The base64
            transport lets a worker without a GPU or object storage access check the image it receives.
    """
    every_guarantee = tuple(guarantee.value for guarantee in ChatGuarantee)
    if role == ChatPersonaRole.LIMITED:
        return ChatPersona(
            role=role,
            features=(ChatFeature.SEED.value, ChatFeature.TOOLS.value),
            guarantees=(ChatGuarantee.PROMPT_OVERFLOW_REFUSED.value,),
            tools_max_temperature=LIMITED_TOOLS_MAX_TEMPERATURE,
            prompt_jobs=True,
            image_transports=(),
        )
    if role == ChatPersonaRole.FLAKY:
        flaky_features = tuple(
            feature.value for feature in ChatFeature if feature not in (ChatFeature.VISION, ChatFeature.TOOLS_WITH_RESPONSE_FORMAT)
        )
        return ChatPersona(
            role=role,
            features=flaky_features,
            guarantees=every_guarantee,
            tools_max_temperature=None,
            prompt_jobs=False,
            image_transports=(),
        )
    conformant_features = tuple(feature.value for feature in ChatFeature if vision or feature != ChatFeature.VISION)
    return ChatPersona(
        role=role,
        features=conformant_features,
        guarantees=every_guarantee,
        tools_max_temperature=None,
        prompt_jobs=False,
        image_transports=(ChatImageTransport.BASE64.value,) if vision else (),
    )


@dataclass(frozen=True)
class WorkerReply:
    """Represents what a simulated worker submits for one chat job."""

    content: str | None
    """The reply text; None for a reply that only calls tools, and for a fault."""
    tool_calls: list[dict[str, Any]] | None
    """The calls, or None."""
    finish_reason: str
    """The submitted ``finish_reason``."""
    fault_reason: str | None = None
    """When set, the worker faults the job with this ``fault_reason`` instead of submitting a reply."""
    breaks_strict_contract: bool = False
    """True when a ``strict`` request's reply verification refuses it as ``ChatSubmitNonconformant``."""
    breaks_every_contract: bool = False
    """True when reply verification refuses it as ``ChatSubmitInvalid`` under every conformance mode."""


def reply_marker(script: ReplyScript) -> str:
    """Return the marker a client writes into its last user message to ask for ``script``."""
    return f"[reply:{script.value}]"


def nonce_marker(nonce: str) -> str:
    """Return the tag a client writes into a user message so the worker can echo ``nonce`` into its reply.

    Args:
        nonce: Lowercase hexadecimal, unique to the request.
    """
    return f"[nonce:{nonce}]"


def find_request_nonce(messages: Sequence[Any]) -> str:
    """Return the nonce of the last user message that carries one, or an empty string when none does."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            match = NONCE_MARKER_PATTERN.search(_message_text(message))
            if match is not None:
                return match.group(1)
    return ""


def build_scripted_reply(payload: Mapping[str, Any]) -> WorkerReply:
    """Create the reply to a popped chat payload: the marker's script when the last user message has one, else the
    reply a well-behaved backend gives. Every reply carries the request's nonce.

    Args:
        payload: The popped job's ``payload``.
    """
    messages = payload.get("messages") or []
    script = find_reply_script(messages)
    nonce = find_request_nonce(messages)
    first_tool_name = _first_offered_tool_name(payload)
    if script == ReplyScript.TWO_CALLS:
        return WorkerReply(
            content=None,
            tool_calls=[_tool_call(first_tool_name, nonce), _tool_call(first_tool_name, f"{nonce} again")],
            finish_reason=ChatFinishReason.TOOL_CALLS.value,
        )
    if script == ReplyScript.UNDECLARED:
        return WorkerReply(
            content=None,
            tool_calls=[_tool_call(UNOFFERED_FUNCTION_NAME, nonce)],
            finish_reason=ChatFinishReason.TOOL_CALLS.value,
            breaks_every_contract=True,
        )
    if script == ReplyScript.DUP_IDS:
        shared_call_id = _new_call_id()
        return WorkerReply(
            content=None,
            tool_calls=[
                _tool_call(first_tool_name, nonce, call_id=shared_call_id),
                _tool_call(first_tool_name, f"{nonce} again", call_id=shared_call_id),
            ],
            finish_reason=ChatFinishReason.TOOL_CALLS.value,
            breaks_every_contract=True,
        )
    if script == ReplyScript.PROSE:
        return WorkerReply(
            content=f"{PROSE_REPLY} {nonce}", tool_calls=None, finish_reason=ChatFinishReason.STOP.value, breaks_strict_contract=True
        )
    if script == ReplyScript.NONCONFORMANT:
        # Well-formed JSON that misses every key a test schema requires, and carries a key none allows.
        nonconformant_document = json.dumps({"x": 0, "nonce": nonce})
        return WorkerReply(
            content=nonconformant_document, tool_calls=None, finish_reason=ChatFinishReason.STOP.value, breaks_strict_contract=True
        )
    if script == ReplyScript.TEXT_AND_CALL:
        return WorkerReply(
            content=f"{TEXT_BEFORE_CALL} {nonce}",
            tool_calls=[_tool_call(first_tool_name, nonce)],
            finish_reason=ChatFinishReason.TOOL_CALLS.value,
        )
    if script == ReplyScript.FAULT_GENERATION_ERROR:
        return WorkerReply(
            content=None, tool_calls=None, finish_reason=ChatFinishReason.STOP.value, fault_reason=ChatFaultReason.GENERATION_ERROR.value
        )
    return _build_well_behaved_reply(payload, nonce)


def build_misbehaving_reply(payload: Mapping[str, Any]) -> WorkerReply:
    """Create the reply a flaky backend gives when it breaks the request's contract: prose where a tool call is required,
    text that is not JSON where a response format is set, and the well-behaved reply where nothing can be broken.

    Args:
        payload: The popped job's ``payload``.
    """
    nonce = find_request_nonce(payload.get("messages") or [])
    if payload.get("tools") and payload.get("tool_choice") == "required":
        return WorkerReply(
            content=f"{PROSE_REPLY} {nonce}", tool_calls=None, finish_reason=ChatFinishReason.STOP.value, breaks_strict_contract=True
        )
    response_format = payload.get("response_format") or {}
    if response_format.get("type") in ("json_object", "json_schema") and not _expects_tool_call(payload):
        return WorkerReply(
            content=f"{NOT_JSON_REPLY} {nonce}", tool_calls=None, finish_reason=ChatFinishReason.STOP.value, breaks_strict_contract=True
        )
    return _build_well_behaved_reply(payload, nonce)


def _build_well_behaved_reply(payload: Mapping[str, Any], nonce: str) -> WorkerReply:
    """Create the reply a conforming backend gives, carrying ``nonce``: a call when tools are offered to a user turn, a
    document that fits the response format, a description of the images, or plain text."""
    if _expects_tool_call(payload):
        return WorkerReply(
            content=None,
            tool_calls=[_tool_call(_first_offered_tool_name(payload), nonce)],
            finish_reason=ChatFinishReason.TOOL_CALLS.value,
        )
    response_format = payload.get("response_format") or {}
    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema") or {}
        return WorkerReply(
            content=json.dumps(build_schema_sample(schema, text=nonce)), tool_calls=None, finish_reason=ChatFinishReason.STOP.value
        )
    if response_format.get("type") == "json_object":
        return WorkerReply(content=json.dumps({"x": 1, "nonce": nonce}), tool_calls=None, finish_reason=ChatFinishReason.STOP.value)
    image_urls = find_image_urls(payload.get("messages") or [])
    if image_urls:
        return WorkerReply(content=f"{describe_image_urls(image_urls)} {nonce}", tool_calls=None, finish_reason=ChatFinishReason.STOP.value)
    return WorkerReply(content=f"{PLAIN_REPLY} {nonce}", tool_calls=None, finish_reason=ChatFinishReason.STOP.value)


def _expects_tool_call(payload: Mapping[str, Any]) -> bool:
    """Return whether a conforming backend calls a tool: tools arrived and the last message is the user's turn.

    After a tool result the backend answers in prose, which is how a tool round ends.
    """
    messages = payload.get("messages") or []
    last_role = messages[-1].get("role") if messages else None
    return bool(payload.get("tools")) and last_role == "user"


def build_schema_sample(schema: Mapping[str, Any], *, text: str) -> Any:
    """Return a document that satisfies a JSON Schema built from the keywords the suite's schemas use.

    Covers ``type`` object (every property), integer and number (``minimum`` honoured), string, boolean, array
    (``minItems`` honoured), ``enum`` and ``anyOf``. A schema using anything else gets a document that may not fit it.

    Args:
        schema: The schema.
        text: The value of every string, such as the request's nonce.
    """
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return build_schema_sample(schema["anyOf"][0], text=text)
    schema_type = schema.get("type")
    if schema_type == "object":
        return {
            property_name: build_schema_sample(property_schema, text=text)
            for property_name, property_schema in (schema.get("properties") or {}).items()
        }
    if schema_type in ("integer", "number"):
        return max(5, schema.get("minimum", 5))
    if schema_type == "string":
        return text
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        return [build_schema_sample(schema.get("items") or {}, text=text) for _ in range(schema.get("minItems", 0))]
    return None


def find_image_urls(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return the ``url`` of every image part in the messages, in order."""
    image_urls: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image_urls.append(str((part.get("image_url") or {}).get("url", "")))
    return image_urls


def describe_image_urls(image_urls: Sequence[str]) -> str:
    """Return the vision reply: ``vision-ok`` when every image arrived as a JPEG data URL, else what arrived instead."""
    unexpected = [image_url[:40] for image_url in image_urls if not _is_jpeg_data_url(image_url)]
    if unexpected:
        return f"{VISION_UNEXPECTED_PREFIX} {unexpected}"
    return f"{VISION_OK_PREFIX} {len(image_urls)} jpeg"


def _is_jpeg_data_url(image_url: str) -> bool:
    """Return whether a URL is a base64 JPEG data URL whose bytes start like a JPEG file."""
    if not image_url.startswith(JPEG_DATA_URL_PREFIX):
        return False
    try:
        image_bytes = base64.b64decode(image_url[len(JPEG_DATA_URL_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        return False
    return image_bytes.startswith(JPEG_MAGIC)


def find_job_payload_violations(payload: Mapping[str, Any], persona: ChatPersona) -> list[str]:
    """Return how a popped chat payload breaks the payload contract or the worker's own declaration; empty when it
    honours both.

    The checks are the routing layer (nothing the worker did not declare reaches it) and the dispatch narrowing (no
    ``tool_choice`` ``none``, a function choice arrives as ``required`` with one tool), plus the payload shape: messages
    and no prompt, every sampling field present, ``developer`` rewritten to ``system``, budgets within the declaration.

    Args:
        payload: The popped job's ``payload``.
        persona: The declaration the worker popped with.
    """
    violations: list[str] = []
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        violations.append("the chat payload carries no messages")
        messages = []
    if "prompt" in payload:
        violations.append("the chat payload carries a prompt; a chat job never does")
    missing_sampling = [sampling_key for sampling_key in CHAT_SAMPLING_DEFAULTS if sampling_key not in payload]
    if missing_sampling:
        violations.append(f"the chat payload lacks sampling fields {missing_sampling}; every one is always sent")
    roles = {message.get("role") for message in messages if isinstance(message, dict)}
    if not roles <= WORKER_MESSAGE_ROLES:
        violations.append(f"the chat payload carries roles {sorted(str(role) for role in roles - WORKER_MESSAGE_ROLES)}")
    violations.extend(_find_tool_dispatch_violations(payload, persona))
    violations.extend(_find_declaration_violations(payload, persona, messages))
    max_length = payload.get("max_length")
    if not isinstance(max_length, int) or max_length > persona.chat_max_length:
        violations.append(f"max_length {max_length!r} exceeds the declared chat_limits.max_length {persona.chat_max_length}")
    max_context_length = payload.get("max_context_length")
    if not isinstance(max_context_length, int) or max_context_length > persona.chat_max_context_length:
        violations.append(f"max_context_length {max_context_length!r} exceeds the declared {persona.chat_max_context_length}")
    return violations


def _find_tool_dispatch_violations(payload: Mapping[str, Any], persona: ChatPersona) -> list[str]:
    """Return the dispatch narrowing violations of a payload's tool keys."""
    violations: list[str] = []
    tools = payload.get("tools")
    tool_choice = payload.get("tool_choice")
    if tool_choice is not None and not (isinstance(tool_choice, str) and tool_choice in DISPATCHED_TOOL_CHOICES):
        violations.append(f"tool_choice {json.dumps(tool_choice)} reached the worker; only auto and required are dispatched")
    if (tool_choice is not None or "parallel_tool_calls" in payload) and not tools:
        violations.append("tool_choice or parallel_tool_calls arrived without tools")
    tool_temperature_limit = persona.tools_max_temperature
    temperature = payload.get("temperature")
    if tools and tool_temperature_limit is not None and isinstance(temperature, int | float) and temperature > tool_temperature_limit:
        violations.append(
            f"a tools job at temperature {temperature} reached a worker whose tools_max_temperature is {tool_temperature_limit}"
        )
    return violations


def _find_declaration_violations(payload: Mapping[str, Any], persona: ChatPersona, messages: Sequence[Any]) -> list[str]:
    """Return the routing violations of a payload: a feature the worker did not declare reached it."""
    declared = set(persona.features) | set(persona.guarantees)
    violations: list[str] = []
    response_format_type = (payload.get("response_format") or {}).get("type")
    needed: list[str] = []
    if payload.get("tools") or any(
        isinstance(message, dict) and (message.get("role") == "tool" or message.get("tool_calls")) for message in messages
    ):
        needed.append(ChatFeature.TOOLS.value)
    if response_format_type in (ChatFeature.JSON_OBJECT.value, ChatFeature.JSON_SCHEMA.value):
        needed.append(response_format_type)
    if payload.get("tools") and response_format_type is not None:
        needed.append(ChatFeature.TOOLS_WITH_RESPONSE_FORMAT.value)
    if "seed" in payload:
        needed.append(ChatFeature.SEED.value)
    if payload.get("presence_penalty"):
        needed.append(ChatFeature.PRESENCE_PENALTY.value)
    if payload.get("frequency_penalty"):
        needed.append(ChatFeature.FREQUENCY_PENALTY.value)
    if find_image_urls(messages):
        needed.append(ChatFeature.VISION.value)
        if not persona.image_transports:
            violations.append("an image reached a worker that declares no image transport")
    undeclared = [feature for feature in needed if feature not in declared]
    if undeclared:
        violations.append(f"the job needs {undeclared}, which the worker did not declare")
    return violations


def find_reply_script(messages: Sequence[Any]) -> ReplyScript | None:
    """Return the script named by the marker in the last user message, or None without a known one."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            match = REPLY_MARKER_PATTERN.search(_message_text(message))
            if match is None:
                return None
            try:
                return ReplyScript(match.group(1))
            except ValueError:
                return None
    return None


def _message_text(message: Mapping[str, Any]) -> str:
    """Return a message's text, joining text parts."""
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _first_offered_tool_name(payload: Mapping[str, Any]) -> str:
    """Return the first offered tool's name, or ``get_weather`` when none arrived."""
    for tool in payload.get("tools") or []:
        name = (tool.get("function") or {}).get("name")
        if name:
            return str(name)
    return "get_weather"


def _new_call_id() -> str:
    """Create a call id unique to this reply."""
    return f"call_{uuid.uuid4().hex[:12]}"


def _tool_call(name: str, city: str, *, call_id: str | None = None) -> dict[str, Any]:
    """Create one call in the shape a backend mints it, with ``city`` as its only argument; both offered tools take a
    ``city`` string, which is where the request's nonce goes."""
    return {"id": call_id or _new_call_id(), "type": "function", "function": {"name": name, "arguments": json.dumps({"city": city})}}
