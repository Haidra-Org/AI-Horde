# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OpenAPI description of the chat front, served at ``/docs``, ``/redoc`` and ``/openapi.json``.

The route handlers read and validate their bodies by hand (see [`horde.oai.app`][horde.oai.app]), so FastAPI does not
derive request or response schemas from them. This module generates the component schemas from the models in
[`horde.oai.schema`][horde.oai.schema] and supplies the request bodies, responses and summaries of each route, which
point at those schemas. The documented schemas are therefore the models used by the handlers for validation and
serialization. The handlers' runtime behavior does not depend on anything here.

The field and model descriptions are the models' docstrings, which also feed the mkdocs reference. Their mkdocstrings
cross-references are rewritten for the API pages, where the referenced code is not reachable.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from http import HTTPStatus
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_MAX_TOKENS, MIN_CHAT_MAX_TOKENS
from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue, models_json_schema
from pydantic_core import core_schema

from horde.chat_contract import (
    DEFAULT_CHAT_MAX_TOKENS,
    MAX_CHAT_MODEL_COUNT,
    ChatFunctionCall,
    ChatFunctionDefinition,
    ChatNamedFunction,
    ChatNamedToolChoice,
    ChatTool,
    ChatToolCall,
)
from horde.consts import ANONYMOUS_API_KEY
from horde.oai.schema import (
    DEFAULT_NO_OP_PARAMETERS,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ImageContentPart,
    ImageUrl,
    ModelCard,
    ModelList,
    OpenAIErrorEnvelope,
    TextContentPart,
)

AIHORDE_URL = "https://aihorde.net"
"""The public AI Horde site, linked from the description for registration, terms and the native API."""

DOCS_URL = "https://haidra-org.github.io/AI-Horde"
"""The base of the published documentation site linked from the description. The site is built from ``docs/`` with
directory URLs, so a page is linked by its path without the ``.md`` suffix and with a trailing slash."""

DOCS_PATH = "/docs"
"""The path of the interactive documentation page. ``GET /`` redirects to it."""

COMPONENT_REF_TEMPLATE = "#/components/schemas/{model}"
"""The reference template for schemas moved into the components of the OpenAPI document."""

BEARER_SECURITY_SCHEME = "AIHordeApiKey"
"""The name of the bearer security scheme in the OpenAPI document."""

CHAT_TAG = "Chat"
"""The OpenAPI tag of the chat completions route."""

MODELS_TAG = "Models"
"""The OpenAPI tag of the model list routes."""

OPENAPI_TAGS: tuple[Mapping[str, str], ...] = (
    {
        "name": CHAT_TAG,
        "description": "Chat completions in the OpenAI request and response shape.",
    },
    {
        "name": MODELS_TAG,
        "description": "The text models served by online chat workers.",
    },
)
"""The tags of the documented routes, in their order on the documentation pages."""

SHOULD_RETRY_HEADER = "x-should-retry"
"""The response header read by OpenAI SDKs to decide whether to retry. It equals ``SHOULD_RETRY_HEADER`` in
[`horde.oai.app`][horde.oai.app], which imports this module and so cannot be imported here."""

RETRY_AFTER_HEADER = "Retry-After"
"""The response header with the seconds to wait before a retry. It equals ``RETRY_AFTER_HEADER`` in
[`horde.oai.app`][horde.oai.app]."""

NO_RETRY_STATUS_HEADER: Mapping[str, Any] = MappingProxyType(
    {
        "description": (
            "`false` when the Horde holds the request, or may hold it, so a retry can create a second charged "
            "request. OpenAI SDKs read it and do not retry. An error without it came before the request was queued."
        ),
        "schema": {"type": "string", "enum": ["false"]},
    },
)
"""The OpenAPI header object of ``x-should-retry``."""

RETRY_AFTER_STATUS_HEADER: Mapping[str, Any] = MappingProxyType(
    {
        "description": "Seconds to wait before retrying.",
        "schema": {"type": "integer"},
    },
)
"""The OpenAPI header object of ``Retry-After``."""

FRONT_SUMMARY = "OpenAI-compatible chat completions on the AI Horde's volunteer text workers."
"""The one-line summary shown under the title on the documentation pages."""

FRONT_DESCRIPTION = f"""\
This service is an OpenAI-compatible chat completions API for the [AI Horde]({AIHORDE_URL}), a free and open source
service where volunteers run the models. Software written for the OpenAI API can use the Horde's text models when its
base URL is set to this host with `/v1`.

## Authentication

Send an AI Horde API key as `Authorization: Bearer <key>`. [Register an account]({AIHORDE_URL}/register) to get one.
A request without the header uses the anonymous key `{ANONYMOUS_API_KEY}`. Anonymous requests have the lowest queue
priority. To try a route on this page, choose **Authorize** and enter your key.

## Availability

Volunteer workers serve every request, so the models on offer, the queue and the latency change over time.
`GET /v1/models` lists the models served by online workers now, with the largest context and completion lengths
declared by those workers. A request that cannot be served by any online worker is refused and is not queued
indefinitely.

## Differences from OpenAI

- The service accepts a subset of the OpenAI request keys. Any other top-level key is refused with 400
  `UnsupportedChatParameter` and the key's path in `param`. `logprobs`, `top_logprobs`, `store` and `service_tier`
  are accepted only at their default values, and are then dropped. `user` and `metadata` are accepted and not used.
  An unknown key inside a message, a tool definition or a `tool_choice` object is dropped, as OpenAI ignores it.
- `max_tokens` and `max_completion_tokens` range from {MIN_CHAT_MAX_TOKENS} to {MAX_CHAT_MAX_TOKENS} and cover the
  reasoning and the answer together. When both are omitted, the limit is {DEFAULT_CHAT_MAX_TOKENS}. A limit above
  the largest length declared by an online worker for the model is lowered to that length and reported in
  `x_horde.context_adjustments`.
- The model's reasoning is returned as `reasoning_content` in the message and in stream deltas, as DeepSeek and
  OpenRouter return it. OpenAI does not return reasoning text. Send `"horde": {{"include_reasoning": false}}` to leave
  it out.
- `models`, in place of `model`, lists up to {MAX_CHAT_MODEL_COUNT} models, and a worker running any of them may
  serve the request.
- The `horde` request object holds the Horde's options, such as worker restrictions, `max_kudos` and
  `availability_mode`. The `x_horde` response object holds the Horde's details, such as the request id, the worker
  and the kudos spent.
- `system_fingerprint` is a short hash of the serving worker id and model.
- A stream sends each tool call whole in one chunk. The text of a plain text reply arrives in pieces about once a
  second when the worker reports progress. Other replies arrive whole in one chunk.
- Errors use the OpenAI envelope with the service's codes in `code` and an added `metadata` object.

## Further reading

- [Your first chat completion through the Horde]({DOCS_URL}/tutorial/first_chat_completion/)
- [Send chat completions through the OpenAI-compatible front]({DOCS_URL}/how-to/send_chat_completions/): request
  keys, the `horde` object, streams, errors and retries.
- [Chat front reference]({DOCS_URL}/reference/chat_front/): request keys, response shape, errors and the deliberate
  deviations from OpenAI.
- [Chat requests on the Horde]({DOCS_URL}/explanation/chat_requests/): why the service behaves as it does.

The AI Horde [terms]({AIHORDE_URL}/terms) and [privacy policy]({AIHORDE_URL}/privacy) apply to this service.
"""
"""The Markdown description shown at the top of the documentation pages."""

CHAT_COMPLETIONS_DESCRIPTION = f"""\
Generate the next assistant message of a conversation on a volunteer worker.

With `stream` false or absent, the response is one `chat.completion` object. With `stream` true, the response is
server-sent events. Each `data` line holds a `chat.completion.chunk`, and the stream ends with `data: [DONE]`. Comment
lines keep the connection open while the request waits. The stream's headers are sent after the first status read of
the request, so a refusal at that read is an HTTP error, as for a request without `stream`. An error after the headers
is one event holding the error envelope, followed by `data: [DONE]`. For example, `ChatStreamDiverged` means the
reply streamed so far cannot be completed.

Every error before the response starts is an OpenAI error envelope with the service's `code` and a `metadata` object.
An error with `x-should-retry: false` came after the Horde held the request, or may have held it, so a retry can
create a second charged request. A stream with a 200 status always has the header. An error without it came before
the request was queued, and a retry after `Retry-After` is safe. The
[errors and retries]({DOCS_URL}/how-to/send_chat_completions/#errors-and-retries) section lists every code.
"""
"""The description of ``POST /v1/chat/completions``."""

NO_OP_PARAMETER_SCHEMAS: Mapping[str, JsonSchemaValue] = MappingProxyType(
    {
        "logprobs": {
            "anyOf": [{"const": False}, {"type": "null"}],
            "description": "Accepted only as false or null, and then dropped. Log probabilities are not served.",
        },
        "top_logprobs": {
            "anyOf": [{"const": 0}, {"type": "null"}],
            "description": "Accepted only as 0 or null, and then dropped. Log probabilities are not served.",
        },
        "store": {
            "const": False,
            "description": "Accepted only as false, and then dropped. OpenAI's stored completions are not served.",
        },
        "service_tier": {
            "enum": ["auto", "default"],
            "description": "Accepted only as `auto` or `default`, and then dropped. The service has one tier.",
        },
    },
)
"""The request schema entries of the keys in [`DEFAULT_NO_OP_PARAMETERS`][horde.oai.schema.DEFAULT_NO_OP_PARAMETERS]
that are not fields of the request model. The front accepts such a key only at its default value and drops it, so the
request model does not define it. A key added to the table without an entry here fails the document build."""

KEY_DROPPING_MODELS: tuple[type[BaseModel], ...] = (
    ChatMessage,
    ChatToolCall,
    ChatFunctionCall,
    TextContentPart,
    ImageContentPart,
    ImageUrl,
    ChatTool,
    ChatFunctionDefinition,
    ChatNamedToolChoice,
    ChatNamedFunction,
)
"""The request models whose unknown keys are dropped before validation, as OpenAI ignores them (see
[`drop_unknown_nested_keys`][horde.oai.schema.drop_unknown_nested_keys]). Their documented schemas allow other keys,
although the models forbid them."""

FASTAPI_VALIDATION_SCHEMAS: tuple[str, ...] = (
    "HTTPValidationError",
    "ValidationError",
)
"""The component schemas added by FastAPI for its 422 response to a route with a path parameter."""

SEE_REFERENCE_PATTERN = re.compile(r"\s*\(see\s+\[`[^`]+`\]\[[\w.]+\](?:\s+and\s+\[`[^`]+`\]\[[\w.]+\])*\)")
"""A parenthetical ``(see [`name`][dotted.path])`` cross-reference, optionally with further ``and`` references."""

CROSS_REFERENCE_PATTERN = re.compile(r"\[`([^`]+)`\]\[[\w.]+\]")
"""An inline ``[`name`][dotted.path]`` cross-reference. The first group is the displayed name."""


class DocumentedJsonSchema(GenerateJsonSchema):
    """Represents the JSON schema generator for the documented models.

    Several response models serialize through a wrap ``model_serializer`` that only leaves absent optional keys out and
    is annotated as returning ``dict[str, object]``. Pydantic documents such a model by that return type, an open
    object, which hides every field. This generator documents those models by their fields instead.
    """

    def ser_schema(
        self,
        schema: core_schema.SerSchema | core_schema.IncExSeqSerSchema | core_schema.IncExDictSerSchema,
    ) -> JsonSchemaValue | None:
        """Return None for a wrap serializer returning a plain dict, so the caller falls back to the field schema.

        Args:
            schema: The serialization core schema.

        Returns:
            The JSON schema of the serialized value, or None to use the schema of the fields.
        """
        if schema["type"] == "function-wrap":
            return_schema = schema.get("return_schema")
            if return_schema is not None and return_schema["type"] == "dict":
                values_schema = return_schema.get("values_schema")
                if values_schema is None or values_schema["type"] == "any":
                    return None
        return super().ser_schema(schema)


_DocumentedModel = tuple[type[BaseModel], JsonSchemaMode]

_DOCUMENTED_MODELS: tuple[_DocumentedModel, ...] = (
    (ChatCompletionRequest, "validation"),
    (ChatCompletionResponse, "serialization"),
    (ChatCompletionChunk, "serialization"),
    (OpenAIErrorEnvelope, "serialization"),
    (ModelList, "serialization"),
    (ModelCard, "serialization"),
)
"""The documented models, each with the mode it is documented in: the request as validated, the rest as serialized."""


@functools.cache
def _component_schemas() -> tuple[Mapping[_DocumentedModel, JsonSchemaValue], Mapping[str, JsonSchemaValue]]:
    """Return the reference of each documented model and the component schemas referenced by them.

    Returns:
        The reference object per documented model and mode, and the schemas keyed by component name.
    """
    references, definitions = models_json_schema(
        _DOCUMENTED_MODELS,
        ref_template=COMPONENT_REF_TEMPLATE,
        schema_generator=DocumentedJsonSchema,
    )
    component_schemas: dict[str, JsonSchemaValue] = {
        name: _without_cross_references(schema) for name, schema in definitions.get("$defs", {}).items()
    }
    request_schema = component_schemas[_component_name(references[(ChatCompletionRequest, "validation")])]
    request_properties = request_schema["properties"]
    for no_op_key in DEFAULT_NO_OP_PARAMETERS:
        if no_op_key not in request_properties:
            request_properties[no_op_key] = dict(NO_OP_PARAMETER_SCHEMAS[no_op_key])
    for key_dropping_model in KEY_DROPPING_MODELS:
        component_schemas[key_dropping_model.__name__].pop("additionalProperties", None)
    return {(model, mode): references[(model, mode)] for model, mode in _DOCUMENTED_MODELS}, component_schemas


def _component_name(reference: JsonSchemaValue) -> str:
    """Return the component name of a ``$ref`` object made with ``COMPONENT_REF_TEMPLATE``.

    Args:
        reference: A ``$ref`` object.

    Returns:
        The name after the template's prefix.
    """
    return str(reference["$ref"]).removeprefix(COMPONENT_REF_TEMPLATE.removesuffix("{model}"))


def plain_description(description: str) -> str:
    """Return a docstring description without mkdocstrings cross-reference syntax.

    A parenthetical "see" reference only points a maintainer at code, so it is dropped. An inline reference is part of
    the sentence, so its name is kept as code.

    Args:
        description: A description taken from a docstring.

    Returns:
        The description as plain Markdown.
    """
    return CROSS_REFERENCE_PATTERN.sub(r"`\1`", SEE_REFERENCE_PATTERN.sub("", description))


def _without_cross_references(schema: JsonSchemaValue) -> JsonSchemaValue:
    """Return a copy of a JSON schema whose ``description`` strings are passed through ``plain_description``.

    Args:
        schema: A component schema.

    Returns:
        The rewritten copy.
    """
    rewritten: JsonSchemaValue = {}
    for key, value in schema.items():
        if key == "description" and isinstance(value, str):
            rewritten[key] = plain_description(value)
        elif isinstance(value, dict):
            rewritten[key] = _without_cross_references(value)
        elif isinstance(value, list):
            rewritten[key] = [_without_cross_references(item) if isinstance(item, dict) else item for item in value]
        else:
            rewritten[key] = value
    return rewritten


def _reference(model: type[BaseModel], mode: JsonSchemaMode) -> JsonSchemaValue:
    """Return the component reference of a documented model.

    Args:
        model: One of the documented models.
        mode: The mode of the model's documentation.

    Returns:
        A ``$ref`` object pointing into the document's component schemas.
    """
    references, _ = _component_schemas()
    return dict(references[(model, mode)])


def _error_response(description: str, *, retry_after: bool = False) -> dict[str, Any]:
    """Return an OpenAPI response object for an OpenAI error envelope.

    Args:
        description: When the route returns the status, by error code.
        retry_after: Whether the status always has a ``Retry-After`` header.

    Returns:
        The response object.
    """
    headers: dict[str, Any] = {SHOULD_RETRY_HEADER: dict(NO_RETRY_STATUS_HEADER)}
    if retry_after:
        headers[RETRY_AFTER_HEADER] = dict(RETRY_AFTER_STATUS_HEADER)
    return {
        "description": description,
        "headers": headers,
        "content": {"application/json": {"schema": _reference(OpenAIErrorEnvelope, "serialization")}},
    }


def chat_completions_route_options() -> dict[str, Any]:
    """Return the OpenAPI keyword arguments for ``add_api_route`` of ``POST /v1/chat/completions``.

    Returns:
        The summary, description, tag, request body and responses of the route.
    """
    return {
        "summary": "Create a chat completion",
        "description": CHAT_COMPLETIONS_DESCRIPTION,
        "tags": [CHAT_TAG],
        "openapi_extra": {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": _reference(ChatCompletionRequest, "validation")}},
            },
        },
        "responses": {
            HTTPStatus.OK.value: {
                "description": "The completion, or a stream of completion chunks when `stream` is true.",
                "headers": {SHOULD_RETRY_HEADER: dict(NO_RETRY_STATUS_HEADER)},
                "content": {
                    "application/json": {"schema": _reference(ChatCompletionResponse, "serialization")},
                    "text/event-stream": {"schema": _reference(ChatCompletionChunk, "serialization")},
                },
            },
            HTTPStatus.BAD_REQUEST.value: _error_response(
                "`UnsupportedChatParameter`, `ChatRequestConflict` or `BadRequest`: the body is not a JSON object, or it "
                "holds a key or value that is not served. `max_kudos_exceeded`: the kudos estimate exceeds "
                "`horde.max_kudos`. `no_worker_supports_limit`: the request's `max_tokens` or context length is above "
                "the largest declared by an online worker for the model. The Horde's refusals, such as "
                "`PromptExceedsContext` or an image code, keep their return code.",
            ),
            HTTPStatus.UNAUTHORIZED.value: _error_response(
                "`InvalidAuthorizationHeader`: the `Authorization` header is present but is not `Bearer <key>`. "
                "`InvalidAPIKey`: the Horde does not know the key.",
            ),
            HTTPStatus.FORBIDDEN.value: _error_response(
                "The Horde refused the request for this key. For example, `KudosUpfront` refuses a request above the "
                "free output allowance when the key does not have the kudos for it.",
            ),
            HTTPStatus.NOT_FOUND.value: _error_response(
                "`model_not_found`: the model id contains `:`. `UnknownChatModel`: the Horde does not know the model. "
                "`no_worker_supports_features`: online workers serve the model, but none declares a capability or "
                "guarantee needed by the request.",
            ),
            HTTPStatus.REQUEST_TIMEOUT.value: _error_response(
                "`RequestBodyTimeout`: the body was not received in time. `ChatRequestTimeout`: the request was still "
                "waiting for a worker at its deadline and was cancelled.",
            ),
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE.value: _error_response(
                "`RequestTooLarge`: the body is over the byte limit.",
            ),
            HTTPStatus.TOO_MANY_REQUESTS.value: _error_response(
                "The Horde is rate limiting the key or the client. The code is the Horde's return code, or "
                "`rate_limit_exceeded` when the Horde does not send one.",
                retry_after=True,
            ),
            HTTPStatus.BAD_GATEWAY.value: _error_response(
                "`HordeUnavailable`: the Horde could not be reached, or the submit was sent and no response came "
                "(`metadata.submit_outcome` is `unknown`). `ChatRequestFaulted`: the Horde reports the request as "
                "faulted. `ChatRequestNoGeneration`: the Horde ended the request without a reply.",
            ),
            HTTPStatus.SERVICE_UNAVAILABLE.value: _error_response(
                "`no_eligible_worker`: no online worker serves the model, or none could serve the request within the "
                "grace period. `queue_unavailable`: the request was refused under `horde.availability_mode` "
                "`fail_fast`. `FrontAtCapacity`: this service holds its maximum of open requests for the client or in "
                "total. The Horde's own 503 refusals, such as `ChatAdmissionDisabled`, keep their return code.",
                retry_after=True,
            ),
        },
    }


def list_models_route_options() -> dict[str, Any]:
    """Return the OpenAPI keyword arguments for ``add_api_route`` of ``GET /v1/models``.

    Returns:
        The summary, description, tag and responses of the route.
    """
    return {
        "summary": "List models",
        "description": (
            "List the text models served by at least one online chat worker. Each entry adds OpenRouter's "
            "`context_length`, `architecture`, `top_provider` and `supported_parameters` fields to OpenAI's model "
            "object. They hold the largest context and completion lengths declared by those workers and the request "
            "keys honoured by at least one of them. `x_horde` holds the worker count and the queued work."
        ),
        "tags": [MODELS_TAG],
        "responses": {
            HTTPStatus.OK.value: {
                "description": "The models.",
                "content": {"application/json": {"schema": _reference(ModelList, "serialization")}},
            },
            HTTPStatus.UNAUTHORIZED.value: _error_response(
                "`InvalidAuthorizationHeader`: the `Authorization` header is present but is not `Bearer <key>`.",
            ),
            HTTPStatus.BAD_GATEWAY.value: _error_response("`HordeUnavailable`: the Horde could not be reached."),
        },
    }


def get_model_route_options() -> dict[str, Any]:
    """Return the OpenAPI keyword arguments for ``add_api_route`` of ``GET /v1/models/{model_id}``.

    Returns:
        The summary, description, tag and responses of the route.
    """
    return {
        "summary": "Retrieve a model",
        "description": "Return one entry of the model list by its id. The id may contain slashes.",
        "tags": [MODELS_TAG],
        "responses": {
            HTTPStatus.OK.value: {
                "description": "The model.",
                "content": {"application/json": {"schema": _reference(ModelCard, "serialization")}},
            },
            HTTPStatus.UNAUTHORIZED.value: _error_response(
                "`InvalidAuthorizationHeader`: the `Authorization` header is present but is not `Bearer <key>`.",
            ),
            HTTPStatus.NOT_FOUND.value: _error_response("`model_not_found`: no online chat worker serves a model with this id."),
            HTTPStatus.BAD_GATEWAY.value: _error_response("`HordeUnavailable`: the Horde could not be reached."),
        },
    }


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Return the app's OpenAPI document with the documented models' schemas and the bearer scheme added.

    Set as the app's ``openapi`` method. The document is built once and cached on the app, as FastAPI's default does.

    Args:
        app: The chat front application.

    Returns:
        The OpenAPI document.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema
    document = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    # The path parameter of GET /v1/models/{model_id} accepts any string, so FastAPI's 422 response does not occur.
    for path_item in document.get("paths", {}).values():
        for operation in path_item.values():
            operation.get("responses", {}).pop(str(HTTPStatus.UNPROCESSABLE_ENTITY.value), None)
    components = document.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for validation_schema in FASTAPI_VALIDATION_SCHEMAS:
        schemas.pop(validation_schema, None)
    _, component_schemas = _component_schemas()
    schemas.update(component_schemas)
    components["securitySchemes"] = {
        BEARER_SECURITY_SCHEME: {
            "type": "http",
            "scheme": "bearer",
            "description": f"An AI Horde API key. Omit it to use the anonymous key `{ANONYMOUS_API_KEY}`.",
        },
    }
    # The empty requirement makes the key optional: a request without it is served as anonymous.
    document["security"] = [{BEARER_SECURITY_SCHEME: []}, {}]
    app.openapi_schema = document
    return document
