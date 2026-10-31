# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Serve AI Horde chat jobs from an OpenAI-compatible backend such as llama.cpp's server, vLLM or koboldcpp.

Usage: minimal_chat_worker.py --horde-url URL --api-key KEY --worker-name NAME --model MODEL --backend-url URL

Every option falls back to the environment variable listed by ``--help``. The worker pops chat jobs only, sends each
to the backend's ``POST /v1/chat/completions`` without streaming, and submits the reply or a fault.

The worker submits the backend's ``message`` as the backend returns it. A reasoning model's backend has to return the
reasoning in ``message.reasoning_content``. Reasoning inline in ``content`` reaches the requester as reply text.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

import requests
from horde_sdk.generation_parameters.text.chat_policy import ChatFeature, ChatWorkerLimits
from horde_sdk.worker.chat_declaration import (
    ChatBackendFacts,
    ChatFaultReason,
    ChatFinishReason,
    ChatJobPayload,
    ChatToolStrategy,
    ChatUsage,
    ChatWorkerDeclaration,
    build_chat_fault_submit,
    build_chat_reply_submit,
    declare_from_backend,
)

DESCRIPTION: Final = "Serve AI Horde chat jobs from an OpenAI-compatible backend."
"""The summary ``--help`` prints."""

CLIENT_AGENT: Final = "minimal_chat_worker:1:unknown"
"""The ``Client-Agent`` header and ``bridge_agent`` the horde records for this worker."""

EMPTY_POP_SLEEP_SECONDS: Final = 2
"""The pause after a pop without a job, so an idle worker does not poll the horde in a tight loop."""

JOB_DEADLINE_SECONDS: Final = 120
"""The time from a job's pop to the end of its last backend call, shared by the counter calls and the generation.

The horde's job lease is 150 seconds from the pop, and this worker does not post progress to extend it. The other 30
seconds leave room for the submit, which waits at most ``HORDE_TIMEOUT_SECONDS``.
"""

HORDE_TIMEOUT_SECONDS: Final = 30
"""The longest wait for the horde's response to a pop or submit."""

NATIVE_FINISH_REASON_LIMIT: Final = 32
"""The longest ``native_finish_reason`` the horde stores."""

NATIVE_TOOL_CALL_FINISH_REASONS: Final = frozenset({"tool_calls", "function_call"})
"""The backend finish reasons of a reply that calls tools: OpenAI's current one and its deprecated one."""

REPLY_REFUSAL_RCS: Final = frozenset({"ChatSubmitInvalid", "ChatSubmitNonconformant", "ChatModelMismatch"})
"""The return codes of a ``400`` that refuses the reply itself, which the worker follows with a ``reply_refused`` fault.

The horde refuses the same reply again, so the fault sends the job back to the queue. ``AbortedGen`` and
``DuplicateGen`` are also ``400`` responses. They refuse a job that the worker no longer holds, and a fault after them
would record a refused reply for a reply that the horde did not check.
"""

SUPPORTED_FEATURES: Final[frozenset[ChatFeature]] = frozenset(
    {
        ChatFeature.SEED,
        ChatFeature.PRESENCE_PENALTY,
        ChatFeature.FREQUENCY_PENALTY,
        ChatFeature.TOOLS,
        ChatFeature.JSON_OBJECT,
        ChatFeature.JSON_SCHEMA,
        ChatFeature.REASONING_EFFORT,
    },
)
"""The features ``--features`` accepts. ``vision`` needs an image transport and ``tools_with_response_format`` a
backend that keeps both apart, which this worker does not handle."""

TEMPLATE_OVERHEAD_HELP: Final = (
    "tokens added to every prompt count in place of the startup measurement, for every --token-counter (env CHAT_TEMPLATE_OVERHEAD)"
)
"""The ``--help`` text of ``--template-overhead``."""


class TokenCounter(StrEnum):
    """Identify how the fit check counts a prompt: the UTF-8 byte bound, or the backend's tokenizer."""

    BYTES = "bytes"
    """An upper bound from the UTF-8 length of the JSON, which needs no backend route."""

    LLAMACPP = "llamacpp"
    """llama.cpp's ``/apply-template`` and ``/tokenize``."""

    KOBOLDCPP = "koboldcpp"
    """koboldcpp's ``/api/extra/tokencount``."""

    VLLM = "vllm"
    """vLLM's ``/v1/chat/completions/render``."""


@dataclass(frozen=True, kw_only=True)
class BackendConfig:
    """Represents the settings for reaching the backend and counting its prompts."""

    model: str
    """The model name declared to the horde and sent to the backend."""
    backend_url: str
    """The backend root that serves ``/v1/chat/completions``."""
    token_counter: TokenCounter
    """How the fit check counts a prompt."""
    template_overhead: int | None
    """The operator's count of the tokens the fit check adds to every prompt count, for every token counter.

    It replaces the startup measurement: the probe's prompt count for the byte bound, and 0 for an exact counter,
    which counts the template itself.
    """


@dataclass(frozen=True, kw_only=True)
class WorkerConfig:
    """Represents the operator's settings."""

    horde_url: str
    """The horde root, such as ``https://aihorde.net``."""
    api_key: str
    """The worker owner's API key."""
    worker_name: str
    """The name the worker registers under."""
    backend: BackendConfig
    """The backend settings."""
    limits: ChatWorkerLimits
    """The context and reply lengths the backend serves."""
    features: frozenset[ChatFeature]
    """The optional features the operator knows the backend honours."""


def parse_config(argv: list[str] | None = None) -> WorkerConfig:
    """Read the settings from the command line, each falling back to its environment variable.

    Args:
        argv: The arguments to parse, or None for ``sys.argv``.

    Returns:
        The parsed settings. A missing or invalid setting exits through ``argparse``.
    """
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    for flag, env, kind in (
        ("--horde-url", "HORDE_URL", str),
        ("--api-key", "HORDE_WORKER_API_KEY", str),
        ("--worker-name", "HORDE_WORKER_NAME", str),
        ("--model", "HORDE_MODEL_NAME", str),
        ("--backend-url", "BACKEND_URL", str),
        ("--max-context-length", "CHAT_MAX_CONTEXT_LENGTH", int),
        ("--max-length", "CHAT_MAX_LENGTH", int),
        ("--features", "CHAT_FEATURES", str),
    ):
        parser.add_argument(flag, type=kind, default=os.environ.get(env), help=f"env {env}")
    parser.add_argument(
        "--template-overhead",
        type=int,
        default=os.environ.get("CHAT_TEMPLATE_OVERHEAD"),
        help=TEMPLATE_OVERHEAD_HELP,
    )
    parser.add_argument(
        "--token-counter",
        type=TokenCounter,
        choices=list(TokenCounter),
        default=os.environ.get("CHAT_TOKEN_COUNTER", TokenCounter.BYTES),
        help="env CHAT_TOKEN_COUNTER",
    )
    args = parser.parse_args(argv)
    required = ("horde_url", "api_key", "worker_name", "model", "backend_url", "max_context_length", "max_length")
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        parser.error(f"missing settings: {', '.join(missing)}")
    feature_names = frozenset(name.strip() for name in (args.features or "").split(",") if name.strip())
    if not feature_names <= SUPPORTED_FEATURES:
        parser.error(f"--features accepts only {', '.join(sorted(SUPPORTED_FEATURES))}")
    return WorkerConfig(
        horde_url=args.horde_url.rstrip("/"),
        api_key=args.api_key,
        worker_name=args.worker_name,
        backend=BackendConfig(
            model=args.model,
            backend_url=args.backend_url.rstrip("/"),
            token_counter=args.token_counter,
            template_overhead=args.template_overhead,
        ),
        limits=ChatWorkerLimits(max_context_length=args.max_context_length, max_length=args.max_length),
        features=frozenset(ChatFeature(name) for name in feature_names),
    )


def _post_json(session: requests.Session, url: str, body: dict[str, Any], *, deadline: float) -> dict[str, Any]:
    """Return the JSON body of a backend response, waiting no longer than the ``time.monotonic()`` ``deadline``.

    Raises:
        requests.Timeout: The deadline has passed before the call.
        requests.RequestException: The backend did not respond in time or responded with an error status.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout(f"The job's backend time ran out before {url}.")
    response = session.post(url, json=body, timeout=remaining)
    response.raise_for_status()
    response_body: dict[str, Any] = response.json()
    return response_body


def _byte_bound(body: dict[str, Any]) -> int:
    """Return an upper bound on a chat body's prompt tokens, without the template's fixed part, that needs no tokenizer."""
    # A byte-level BPE tokenizer, which most current models use, emits at least one UTF-8 byte per token, so a text
    # has no more tokens than bytes, and JSON escaping only adds bytes. Each message's JSON keys (28 bytes or more)
    # outweigh the role markup ChatML, Llama 3, Gemma and Mistral templates put around a message, and the template
    # overhead covers the template's fixed part. For English text the bound is about four times the real count.
    rendered = json.dumps([body["messages"], body.get("tools"), body.get("response_format")], ensure_ascii=False)
    return len(rendered.encode())


def count_prompt_tokens(session: requests.Session, *, backend: BackendConfig, body: dict[str, Any], deadline: float) -> int:
    """Return the backend tokenizer's count of the prompt a ``/v1/chat/completions`` body renders to.

    Args:
        session: The HTTP session for the backend calls.
        backend: The backend settings, with an exact ``token_counter``.
        body: The ``/v1/chat/completions`` body to count.
        deadline: The ``time.monotonic()`` value by which every counter call has to finish.

    Returns:
        The prompt's token count.

    Raises:
        requests.RequestException: The backend's counter route did not respond in time or responded with an error.
    """
    if backend.token_counter is TokenCounter.LLAMACPP:
        # /apply-template renders tools as generation does; add_special adds the BOS token the rendered text omits.
        template_body: dict[str, Any] = {key: body[key] for key in ("messages", "tools") if key in body}
        rendered = _post_json(session, f"{backend.backend_url}/apply-template", template_body, deadline=deadline)
        tokenize_body = {"content": rendered["prompt"], "add_special": True}
        tokenized = _post_json(session, f"{backend.backend_url}/tokenize", tokenize_body, deadline=deadline)
        return len(tokenized["tokens"])
    if backend.token_counter is TokenCounter.KOBOLDCPP:
        # koboldcpp's count runs its chat transform, which generates once to choose a tool when tools are present,
        # so the tools stay out of the count and their JSON bytes bound what the template renders of them.
        tokencount_body = {"messages": body["messages"], "special": True}
        token_count = _post_json(session, f"{backend.backend_url}/api/extra/tokencount", tokencount_body, deadline=deadline)
        tools_bytes = len(json.dumps(body["tools"], ensure_ascii=False).encode()) if body.get("tools") else 0
        return int(token_count["value"]) + tools_bytes
    render = _post_json(session, f"{backend.backend_url}/v1/chat/completions/render", body, deadline=deadline)
    return len(render["prompt_token_ids"])


def check_fit_counting(session: requests.Session, config: WorkerConfig) -> int | None:
    """Return the template overhead the fit check adds, or None when the check cannot back the overflow guarantee.

    The backend's prompt count for a one-token generation of one empty user message is the reference: it covers the
    BOS token, any system prompt the template injects and the generation prompt. The byte bound takes it as the
    template's fixed part. An exact counter, plus any ``--template-overhead``, has to reach it for the same body,
    which catches a counter that renders with another template or without the BOS token. The probe has no tools, so
    with ``tools`` declared the byte bound needs ``--template-overhead``, which is less code than probing again with a
    stand-in tool.

    Args:
        session: The HTTP session for the backend calls.
        config: The operator's settings.

    Returns:
        The tokens to add to every prompt count, or None when the worker cannot declare ``prompt_overflow_refused``.

    Raises:
        requests.RequestException: The backend did not respond to the probe or the count, or responded with an error.
    """
    backend = config.backend
    exact = backend.token_counter is not TokenCounter.BYTES
    if backend.template_overhead is not None and not exact:
        return backend.template_overhead
    if ChatFeature.TOOLS in config.features and not exact:
        logging.warning("The byte bound does not measure a tool preamble; tools need --template-overhead or an exact counter.")
        return None
    deadline = time.monotonic() + JOB_DEADLINE_SECONDS
    probe: dict[str, Any] = {"model": backend.model, "messages": [{"role": "user", "content": ""}], "max_tokens": 1}
    probe_completion = _post_json(session, f"{backend.backend_url}/v1/chat/completions", probe, deadline=deadline)
    reference: int | None = (probe_completion.get("usage") or {}).get("prompt_tokens")
    if reference is None:
        logging.warning("The backend reported no usage for the probe, so the worker cannot measure the template.")
        return None
    if not exact:
        return reference
    template_overhead = backend.template_overhead or 0
    counted = count_prompt_tokens(session, backend=backend, body=probe, deadline=deadline) + template_overhead
    if counted < reference:
        logging.warning("The %s counter counts %d prompt tokens where generation counts %d.", backend.token_counter, counted, reference)
        return None
    return template_overhead


def build_declaration(config: WorkerConfig, template_overhead: int | None) -> ChatWorkerDeclaration:
    """Return the chat-only declaration, with ``prompt_overflow_refused`` only when the fit check is sound.

    Args:
        config: The operator's settings.
        template_overhead: The result of ``check_fit_counting``.

    Returns:
        The declaration to send on every pop.
    """
    features = config.features
    backend = ChatBackendFacts(
        honours_seed=ChatFeature.SEED in features,
        applies_presence_penalty=ChatFeature.PRESENCE_PENALTY in features,
        applies_frequency_penalty=ChatFeature.FREQUENCY_PENALTY in features,
        renders_tools=ChatFeature.TOOLS in features,
        tool_strategy=ChatToolStrategy.TEMPLATE if ChatFeature.TOOLS in features else ChatToolStrategy.NONE,
        constrains_json_object=ChatFeature.JSON_OBJECT in features,
        constrains_json_schema=ChatFeature.JSON_SCHEMA in features,
        applies_reasoning_effort=ChatFeature.REASONING_EFFORT in features,
        refuses_prompt_overflow=template_overhead is not None,
    )
    return declare_from_backend(backend, model_id=config.backend.model, limits=config.limits, prompt_jobs=False)


def _prompt_tokens_for(
    session: requests.Session,
    *,
    backend: BackendConfig,
    body: dict[str, Any],
    template_overhead: int,
    deadline: float,
) -> int:
    """Return the prompt count the fit check compares: the counter's exact count or the byte bound, plus the overhead."""
    if backend.token_counter is TokenCounter.BYTES:
        return _byte_bound(body) + template_overhead
    return count_prompt_tokens(session, backend=backend, body=body, deadline=deadline) + template_overhead


def _backend_request(job: ChatJobPayload, model: str) -> dict[str, Any]:
    """Return the ``/v1/chat/completions`` body for a job, every value applied as the horde sent it."""
    # llama.cpp, vLLM and koboldcpp all read top_k and min_p beside the OpenAI fields. Each spells the repetition
    # penalty differently, so rep_pen, neutral unless a native caller set it, is not forwarded.
    body: dict[str, Any] = {
        "model": model,
        "messages": list(job.messages),
        "max_tokens": job.max_length,
        "temperature": job.temperature,
        "top_p": job.top_p,
        "top_k": job.top_k,
        "min_p": job.min_p,
        "frequency_penalty": job.frequency_penalty,
        "presence_penalty": job.presence_penalty,
    }
    optional = {
        "stop": job.stop_sequence,
        "seed": job.seed,
        "tools": job.tools,
        "tool_choice": job.tool_choice,
        "parallel_tool_calls": job.parallel_tool_calls,
        "response_format": job.response_format,
        "reasoning_effort": job.reasoning_effort,
    }
    body.update({key: value for key, value in optional.items() if value is not None})
    return body


def _finish_reason(native: str | None, message: dict[str, Any]) -> ChatFinishReason:
    """Map the backend's finish reason onto the four the horde accepts."""
    if message.get("tool_calls") and native in NATIVE_TOOL_CALL_FINISH_REASONS:
        return ChatFinishReason.TOOL_CALLS
    if native in (ChatFinishReason.LENGTH, ChatFinishReason.CONTENT_FILTER):
        return ChatFinishReason(native)
    return ChatFinishReason.STOP


def _usage(completion: dict[str, Any]) -> ChatUsage:
    """Return the token counts a completion reports, each None when the backend omits it."""
    usage = completion.get("usage") or {}
    return ChatUsage(
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    )


def generate(
    session: requests.Session,
    *,
    config: WorkerConfig,
    job: ChatJobPayload,
    template_overhead: int,
    deadline: float,
) -> dict[str, Any]:
    """Return the submit body for a popped job: the backend's reply, or a fault.

    Args:
        session: The HTTP session for the backend calls.
        config: The operator's settings.
        job: The popped job.
        template_overhead: The tokens the fit check adds to every prompt count.
        deadline: The ``time.monotonic()`` value by which every backend call for the job has to finish.

    Returns:
        A reply submit body, or a fault submit body with ``prompt_overflow`` or ``generation_error``.
    """
    job_id = job.job_id
    assert job_id is not None, "a popped job has its id"
    backend = config.backend
    body = _backend_request(job, backend.model)
    try:
        prompt_tokens = _prompt_tokens_for(session, backend=backend, body=body, template_overhead=template_overhead, deadline=deadline)
        if prompt_tokens + job.max_length > job.max_context_length:
            return build_chat_fault_submit(job_id, backend.model, ChatFaultReason.PROMPT_OVERFLOW)
        completion = _post_json(session, f"{backend.backend_url}/v1/chat/completions", body, deadline=deadline)
        choice = completion["choices"][0]
        native = choice.get("finish_reason")
        return build_chat_reply_submit(
            job_id,
            backend.model,
            choice["message"],
            _finish_reason(native, choice["message"]),
            _usage(completion),
            native_finish_reason=native[:NATIVE_FINISH_REASON_LIMIT] if native else None,
        )
    except (requests.RequestException, KeyError, IndexError, ValueError) as error:
        logging.warning("The backend failed on job %s: %r", job_id, error)
        return build_chat_fault_submit(job_id, backend.model, ChatFaultReason.GENERATION_ERROR)


def _refusal_rc(response: requests.Response) -> str | None:
    """Return the ``rc`` of a horde error response, or None when the body is not a JSON object with one."""
    try:
        response_body = response.json()
    except ValueError:
        return None
    return response_body.get("rc") if isinstance(response_body, dict) else None


def serve_one_pop(
    session: requests.Session,
    *,
    config: WorkerConfig,
    declaration: ChatWorkerDeclaration,
    template_overhead: int,
) -> bool:
    """Pop once and submit the job's reply or fault.

    Args:
        session: The HTTP session for the horde and backend calls.
        config: The operator's settings.
        declaration: The declaration to send on the pop.
        template_overhead: The tokens the fit check adds to every prompt count.

    Returns:
        Whether the pop returned a job.

    Raises:
        requests.RequestException: The horde did not respond to the pop or the submit, or refused it.
    """
    # The lease starts when the horde assigns the job during the pop, so the deadline starts before the pop is sent.
    deadline = time.monotonic() + JOB_DEADLINE_SECONDS
    headers = {"apikey": config.api_key, "Client-Agent": CLIENT_AGENT}
    pop_body = {
        "name": config.worker_name,
        "models": [config.backend.model],
        "bridge_agent": CLIENT_AGENT,
        **declaration.to_pop_fields(),
    }
    pop = session.post(f"{config.horde_url}/api/v2/generate/text/pop", json=pop_body, headers=headers, timeout=HORDE_TIMEOUT_SECONDS)
    if HTTPStatus.BAD_REQUEST <= pop.status_code < HTTPStatus.INTERNAL_SERVER_ERROR:
        # A refused declaration is a 400 with ChatDeclarationInvalid, and the message in the body states which field is wrong.
        logging.warning("The horde refused the pop: %s", pop.text)
    pop.raise_for_status()
    pop_result = pop.json()
    if pop_result.get("id") is None:
        return False
    job = ChatJobPayload.from_pop(pop_result)
    submit_url = f"{config.horde_url}/api/v2/generate/text/submit"
    submit_body = generate(session, config=config, job=job, template_overhead=template_overhead, deadline=deadline)
    submit = session.post(submit_url, json=submit_body, headers=headers, timeout=HORDE_TIMEOUT_SECONDS)
    is_reply = "message" in submit_body
    if is_reply and submit.status_code == HTTPStatus.BAD_REQUEST and _refusal_rc(submit) in REPLY_REFUSAL_RCS:
        # A refusal is definitive: the same reply would be refused again, so the job goes back to the horde.
        logging.warning("The horde refused the reply to job %s: %s", submit_body["id"], submit.text)
        fault = build_chat_fault_submit(submit_body["id"], config.backend.model, ChatFaultReason.REPLY_REFUSED)
        submit = session.post(submit_url, json=fault, headers=headers, timeout=HORDE_TIMEOUT_SECONDS)
    elif submit.status_code >= HTTPStatus.BAD_REQUEST:
        logging.warning("The horde refused the submit of job %s: %s", submit_body["id"], submit.text)
    submit.raise_for_status()
    logging.info("Job %s: %s", submit_body["id"], submit_body.get("fault_reason") or submit_body.get("finish_reason"))
    return True


def main() -> None:
    """Declare the worker and serve chat jobs until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = parse_config()
    session = requests.Session()
    try:
        template_overhead = check_fit_counting(session, config)
    except requests.RequestException as error:
        raise SystemExit(f"The backend did not respond to the {config.backend.token_counter} fit check at startup: {error!r}") from error
    if template_overhead is None:
        logging.warning("The worker does not declare prompt_overflow_refused and serves only best_effort requests.")
    declaration = build_declaration(config, template_overhead)
    logging.info("Declaring %s", declaration.to_pop_fields())
    while True:
        try:
            served = serve_one_pop(session, config=config, declaration=declaration, template_overhead=template_overhead or 0)
        except requests.RequestException as error:
            logging.warning("A pop or submit failed: %r", error)
            served = False
        if not served:
            time.sleep(EMPTY_POP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
