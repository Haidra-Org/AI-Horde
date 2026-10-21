# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validation and mapping of an OpenAI chat-completions body onto a text request, and the chat job payload."""

from __future__ import annotations

import base64
import gzip
import json
import uuid
from io import BytesIO
from typing import Any

import pytest
from horde_sdk.generation_parameters.text.chat_limits import (
    MAX_CHAT_SCHEMA_NESTING,
    MAX_CHAT_TEXT_BYTES,
    MAX_CHAT_TOOL_COUNT,
    MAX_CHAT_TOOL_SPEC_BYTES,
)
from horde_sdk.generation_parameters.text.chat_policy import (
    ChatConformance,
    ChatFeature,
    ChatGuarantee,
    ChatRequestTraits,
    ChatResponseFormatType,
    ChatToolChoiceMode,
    chat_request_requirements,
)
from horde_sdk.worker.chat_declaration import ChatImageTransport
from PIL import Image
from pydantic import ValidationError

from horde import exceptions as e
from horde.chat_contract import (
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    MAX_CHAT_IMAGE_BYTES,
    MAX_CHAT_IMAGE_COUNT,
    MAX_CHAT_MESSAGE_COUNT,
    ChatHordeOptions,
)
from horde.classes.kobold.chat_request import (
    CHAT_IMAGE_PLACEHOLDER,
    CHAT_IMAGE_REFERENCE_PREFIX,
    CHAT_REQUEST_CONFLICT_RC,
    CHAT_STREAM_PROGRESS_KEY,
    CHAT_SUBMIT_INVALID_RC,
    CHAT_SUBMIT_NONCONFORMANT_RC,
    DEFAULT_CHAT_MAX_TOKENS,
    UNSUPPORTED_CHAT_PARAMETER_RC,
    ChatSubmittedMessage,
    build_chat_extras,
    build_chat_image_key,
    build_chat_job_payload,
    build_chat_messages_key,
    build_chat_messages_offload,
    build_chat_request_traits,
    build_model_names,
    build_operation_fingerprint,
    build_stored_chat_extras,
    build_stored_reply,
    build_text_params,
    build_worker_chat_extras,
    build_worker_targeting,
    chat_reply_characters,
    chat_reply_finish_reason,
    chat_tools_temperature,
    flatten_messages,
    has_image_parts,
    inline_chat_images,
    list_chat_image_keys,
    list_image_urls,
    list_stored_chat_object_keys,
    parse_chat_request,
    replace_image_urls,
    required_chat_features,
    resolve_image_references,
    rewrite_message_texts,
    select_chat_transport,
    serialize_messages,
    streams_progress,
    verify_chat_reply,
)
from horde.classes.kobold.processing_generation import is_empty_submission
from horde.exceptions import KNOWN_RC
from horde.image import MAX_CHAT_IMAGE_SIDE, load_chat_image
from horde.r2 import is_chat_messages_key

CHAT_MODEL = "koboldcpp/LLaMA2-13B-Tiefighter"


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hello?"},
        ],
    }
    body.update(overrides)
    return body


def _rejection(body: object) -> e.BadRequest:
    with pytest.raises(e.BadRequest) as raised:
        parse_chat_request(body)
    return raised.value


class TestUnsupportedParameters:
    @pytest.mark.parametrize(
        "unsupported_key",
        [
            "logprobs",
            "top_logprobs",
            "logit_bias",
            "modalities",
            "audio",
            "prediction",
            "reasoning_effort",
            "an_unknown_key",
        ],
    )
    def test_a_top_level_key_outside_the_subset_is_refused_by_name(self, unsupported_key: str) -> None:
        rejection = _rejection(_body(**{unsupported_key: None}))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"'{unsupported_key}'" in rejection.specific

    def test_an_audio_content_part_is_refused_with_its_location(self) -> None:
        audio_part = {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}
        messages = [{"role": "user", "content": [{"type": "text", "text": "Transcribe this."}, audio_part]}]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "messages[0].content[1].type=input_audio" in rejection.specific

    @pytest.mark.parametrize("role", ["system", "developer", "assistant", "tool"])
    def test_an_image_part_outside_a_user_message_is_refused_with_its_location(self, role: str) -> None:
        image_part = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        messages = [{"role": role, "content": [{"type": "text", "text": "Look."}, image_part]}]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "messages[0].content[1].type=image_url" in rejection.specific

    def test_image_parts_on_a_user_message_are_accepted_and_stored_in_the_shape_sent(self) -> None:
        image_part = {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "low"}}
        messages = [{"role": "user", "content": [{"type": "text", "text": "Look."}, image_part]}]

        chat_request = parse_chat_request(_body(messages=messages))

        assert serialize_messages(chat_request) == messages

    def test_a_role_outside_the_served_roles_is_refused(self) -> None:
        rejection = _rejection(_body(messages=[{"role": "function", "content": "x"}]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "messages[0].role=function" in rejection.specific

    @pytest.mark.parametrize(
        ("message", "refused_key"),
        [
            ({"role": "user", "content": "x", "tool_calls": [{"id": "c", "type": "function"}]}, "tool_calls"),
            ({"role": "system", "content": "x", "tool_calls": []}, "tool_calls"),
            ({"role": "user", "content": "x", "tool_call_id": "call_1"}, "tool_call_id"),
            ({"role": "assistant", "content": "x", "tool_call_id": "call_1"}, "tool_call_id"),
            ({"role": "tool", "content": "x", "tool_call_id": "call_1", "name": "lookup"}, "name"),
            ({"role": "tool", "content": "x", "tool_call_id": "call_1", "tool_calls": []}, "tool_calls"),
        ],
    )
    def test_a_tool_key_on_a_role_that_does_not_take_it_is_refused_by_name(self, message: dict[str, Any], refused_key: str) -> None:
        rejection = _rejection(_body(messages=[message]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"'messages[0].{refused_key}'" in rejection.specific

    @pytest.mark.parametrize(
        "horde_option",
        [
            "style",
            "models",
            "workers",
            "worker_blacklist",
            "extra_slow_workers",
            "allow_downgrade",
            "proxied_account",
            "disable_batching",
            "image_transport",
        ],
    )
    def test_a_horde_option_outside_the_chat_vocabulary_is_refused(self, horde_option: str) -> None:
        rejection = _rejection(_body(horde={horde_option: None}))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"horde.{horde_option}" in rejection.specific

    @pytest.mark.parametrize("choice_count", [0, 2])
    def test_more_or_fewer_than_one_choice_is_refused(self, choice_count: int) -> None:
        rejection = _rejection(_body(n=choice_count))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"n={choice_count}" in rejection.specific

    def test_one_choice_is_accepted(self) -> None:
        assert parse_chat_request(_body(n=1)).n == 1

    def test_ignoring_the_context_fit_is_refused(self) -> None:
        assert _rejection(_body(horde={"context_fit": "ignore"})).rc == "BadRequest"

    def test_the_return_codes_are_documented(self) -> None:
        assert UNSUPPORTED_CHAT_PARAMETER_RC in KNOWN_RC
        assert CHAT_REQUEST_CONFLICT_RC in KNOWN_RC


class TestMalformedRequests:
    def test_a_body_that_is_not_an_object_is_refused(self) -> None:
        rejection = _rejection(["not", "an", "object"])

        assert rejection.rc == "BadRequest"

    def test_a_missing_model_is_refused(self) -> None:
        body = _body()
        del body["model"]

        rejection = _rejection(body)

        assert rejection.rc == "BadRequest"
        assert "model" in rejection.specific

    def test_an_empty_message_list_is_refused(self) -> None:
        rejection = _rejection(_body(messages=[]))

        assert rejection.rc == "BadRequest"

    def test_a_string_token_limit_is_refused(self) -> None:
        rejection = _rejection(_body(max_tokens="512"))

        assert rejection.rc == "BadRequest"
        assert "max_tokens" in rejection.specific

    @pytest.mark.parametrize("penalty_key", ["frequency_penalty", "presence_penalty"])
    def test_a_penalty_outside_the_openai_range_is_refused(self, penalty_key: str) -> None:
        rejection = _rejection(_body(**{penalty_key: 2.5}))

        assert rejection.rc == "BadRequest"
        assert penalty_key in rejection.specific

    def test_an_operation_key_that_is_not_a_uuid_is_refused(self) -> None:
        rejection = _rejection(_body(horde={"operation_key": "not-a-uuid"}))

        assert rejection.rc == "BadRequest"
        assert "operation_key" in rejection.specific

    def test_an_operation_key_that_is_a_uuid_is_accepted(self) -> None:
        operation_key = uuid.uuid4()

        assert parse_chat_request(_body(horde={"operation_key": str(operation_key)})).horde.operation_key == operation_key


class TestConflictingValues:
    def test_differing_token_limits_are_refused(self) -> None:
        rejection = _rejection(_body(max_tokens=100, max_completion_tokens=200))

        assert rejection.rc == CHAT_REQUEST_CONFLICT_RC

    def test_equal_token_limits_are_accepted(self) -> None:
        text_params = build_text_params(parse_chat_request(_body(max_tokens=200, max_completion_tokens=200)))

        assert text_params["max_length"] == 200

    def test_only_and_ignore_together_are_refused(self) -> None:
        rejection = _rejection(_body(horde={"only": ["a"], "ignore": ["b"]}))

        assert rejection.rc == CHAT_REQUEST_CONFLICT_RC


class TestSharedHordeOptions:
    """The native route validates the ``horde`` object with the model the front uses, so both refuse alike."""

    def test_an_unknown_horde_key_is_refused_by_name_on_both_sides(self) -> None:
        with pytest.raises(ValidationError) as shared_refusal:
            ChatHordeOptions.model_validate({"style": "some-style"})
        native_refusal = _rejection(_body(horde={"style": "some-style"}))

        assert [(detail["type"], detail["loc"]) for detail in shared_refusal.value.errors()] == [("extra_forbidden", ("style",))]
        assert native_refusal.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "horde.style" in native_refusal.specific

    def test_only_and_ignore_are_a_conflict_on_both_sides(self) -> None:
        with pytest.raises(ValidationError) as shared_refusal:
            ChatHordeOptions.model_validate({"only": ["a"], "ignore": ["b"]})
        native_refusal = _rejection(_body(horde={"only": ["a"], "ignore": ["b"]}))

        assert [detail["type"] for detail in shared_refusal.value.errors()] == [CHAT_REQUEST_CONFLICT_ERROR_TYPE]
        assert native_refusal.rc == CHAT_REQUEST_CONFLICT_RC

    def test_the_native_vocabulary_is_the_shared_models_fields(self) -> None:
        assert parse_chat_request(_body()).horde == ChatHordeOptions()

    def test_conformance_defaults_to_strict_and_accepts_best_effort(self) -> None:
        assert parse_chat_request(_body()).horde.conformance is ChatConformance.STRICT
        assert parse_chat_request(_body(horde={"conformance": "best_effort"})).horde.conformance is ChatConformance.BEST_EFFORT

    def test_an_unknown_conformance_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ChatHordeOptions.model_validate({"conformance": "lenient"})


class TestSizeLimits:
    def test_more_messages_than_the_shared_cap_are_refused(self) -> None:
        messages = [{"role": "user", "content": "x"}] * (MAX_CHAT_MESSAGE_COUNT + 1)

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert str(MAX_CHAT_MESSAGE_COUNT) in rejection.specific

    def test_the_message_cap_itself_is_accepted(self) -> None:
        messages = [{"role": "user", "content": "x"}] * MAX_CHAT_MESSAGE_COUNT

        assert len(parse_chat_request(_body(messages=messages)).messages) == MAX_CHAT_MESSAGE_COUNT

    def test_message_text_over_the_shared_byte_cap_is_refused(self) -> None:
        # Two-byte characters, so the cap is counted in UTF-8 bytes rather than characters.
        oversized_text = "é" * (MAX_CHAT_TEXT_BYTES // 2 + 1)

        rejection = _rejection(_body(messages=[{"role": "user", "content": [{"type": "text", "text": oversized_text}]}]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert str(MAX_CHAT_TEXT_BYTES) in rejection.specific

    @staticmethod
    def _tool_history_with_text_bytes(total_bytes: int) -> list[dict[str, Any]]:
        """Return a one-round tool history whose texts, function name and arguments hold ``total_bytes`` together."""
        fixed_bytes = len(b"Q") + len(b"lookup_weather") + len(b"A")
        tool_call = {
            "id": "call_big",
            "type": "function",
            "function": {"name": "lookup_weather", "arguments": "x" * (total_bytes - fixed_bytes)},
        }
        return [
            {"role": "user", "content": "Q"},
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
            {"role": "tool", "content": "A", "tool_call_id": "call_big"},
        ]

    def test_tool_call_names_and_arguments_count_towards_the_text_byte_cap(self) -> None:
        rejection = _rejection(_body(messages=self._tool_history_with_text_bytes(MAX_CHAT_TEXT_BYTES + 1)))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert str(MAX_CHAT_TEXT_BYTES) in rejection.specific

    def test_a_tool_history_at_the_text_byte_cap_is_accepted(self) -> None:
        chat_request = parse_chat_request(_body(messages=self._tool_history_with_text_bytes(MAX_CHAT_TEXT_BYTES)))

        assert len(chat_request.messages) == 3


class TestParameterMapping:
    def test_max_length_defaults_for_chat_when_no_token_limit_is_set(self) -> None:
        text_params = build_text_params(parse_chat_request(_body()))

        assert text_params == {"max_length": DEFAULT_CHAT_MAX_TOKENS}

    @pytest.mark.parametrize("token_limit_key", ["max_tokens", "max_completion_tokens"])
    def test_either_token_limit_maps_to_max_length(self, token_limit_key: str) -> None:
        text_params = build_text_params(parse_chat_request(_body(**{token_limit_key: 100})))

        assert text_params["max_length"] == 100

    def test_sampling_values_map_onto_the_text_params(self) -> None:
        chat_request = parse_chat_request(
            _body(
                temperature=0.7,
                top_p=0.9,
                n=1,
                seed=123,
                frequency_penalty=0.5,
                presence_penalty=-0.5,
                horde={"max_context_length": 4096},
            ),
        )

        text_params = build_text_params(chat_request)

        assert text_params == {
            "max_length": DEFAULT_CHAT_MAX_TOKENS,
            "max_context_length": 4096,
            "temperature": 0.7,
            "top_p": 0.9,
            "frequency_penalty": 0.5,
            "presence_penalty": -0.5,
            "seed": 123,
        }

    def test_a_single_stop_string_becomes_a_one_element_stop_sequence(self) -> None:
        text_params = build_text_params(parse_chat_request(_body(stop="###")))

        assert text_params["stop_sequence"] == ["###"]

    def test_a_stop_list_becomes_the_stop_sequence(self) -> None:
        text_params = build_text_params(parse_chat_request(_body(stop=["a", "b"])))

        assert text_params["stop_sequence"] == ["a", "b"]

    def test_accepted_but_unapplied_keys_leave_no_trace_in_the_params(self) -> None:
        chat_request = parse_chat_request(
            _body(
                stream=True,
                stream_options={"include_usage": True},
                user="someone",
                metadata={"k": "v"},
                horde={"operation_key": str(uuid.uuid4())},
            ),
        )

        assert build_text_params(chat_request) == {"max_length": DEFAULT_CHAT_MAX_TOKENS}

    def test_the_request_accepts_only_its_model(self) -> None:
        assert build_model_names(parse_chat_request(_body())) == [CHAT_MODEL]

    def test_only_limits_the_request_to_the_listed_workers(self) -> None:
        assert build_worker_targeting(parse_chat_request(_body(horde={"only": ["worker-a"]}))) == (["worker-a"], False)

    def test_ignore_excludes_the_listed_workers(self) -> None:
        assert build_worker_targeting(parse_chat_request(_body(horde={"ignore": ["worker-b"]}))) == (["worker-b"], True)

    def test_no_targeting_leaves_every_worker_eligible(self) -> None:
        assert build_worker_targeting(parse_chat_request(_body())) == ([], False)

    def test_the_horde_options_default_to_growing_the_context(self) -> None:
        chat_request = parse_chat_request(_body())

        assert chat_request.horde.context_fit == "grow"
        assert chat_request.horde.slow_workers is True


LOOKUP_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_weather",
        "description": "Look up the weather.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}
"""A function tool as an OpenAI client sends it."""

LOOKUP_CALL: dict[str, Any] = {
    "id": "call_abc",
    "type": "function",
    "function": {"name": "lookup_weather", "arguments": '{"city":"Oslo"}'},
}
"""A tool call of ``LOOKUP_TOOL`` as a model makes it."""

TOOL_HISTORY: list[dict[str, Any]] = [
    {"role": "user", "content": "Weather in Oslo?"},
    {"role": "assistant", "content": None, "tool_calls": [LOOKUP_CALL]},
    {"role": "tool", "content": "Rain, 7C", "tool_call_id": "call_abc"},
]
"""A conversation in which the model called a tool and the client answered it."""

JSON_SCHEMA_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "colour",
        "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False},
        "strict": True,
    },
}
"""A ``json_schema`` response format as an OpenAI client sends it."""


def _required_features(
    body: dict[str, Any],
    conformance: ChatConformance = ChatConformance.BEST_EFFORT,
) -> set[ChatFeature | ChatGuarantee]:
    chat_request = parse_chat_request(body)
    return required_chat_features(
        build_text_params(chat_request),
        serialize_messages(chat_request),
        build_chat_extras(chat_request),
        conformance,
    )


def _traits(body: dict[str, Any]) -> ChatRequestTraits:
    chat_request = parse_chat_request(body)
    return build_chat_request_traits(build_text_params(chat_request), build_chat_extras(chat_request))


NAMED_LOOKUP_CHOICE: dict[str, Any] = {"type": "function", "function": {"name": "lookup_weather"}}
"""A ``tool_choice`` forcing ``LOOKUP_TOOL``."""

OTHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {"name": "send_email", "parameters": {"type": "object"}},
}
"""A second function tool, beside ``LOOKUP_TOOL``."""


class TestRequestTraits:
    def test_a_request_without_extras_has_the_api_defaults(self) -> None:
        assert _traits(_body()) == ChatRequestTraits(
            offers_tools=False,
            tool_choice_mode=ChatToolChoiceMode.AUTO,
            parallel_tool_calls=True,
            response_format_type=None,
            strict_schema=False,
            temperature=1.0,
        )

    def test_the_sent_values_are_read_from_params_and_extras(self) -> None:
        assert _traits(
            _body(
                temperature=0.4,
                tools=[LOOKUP_TOOL],
                tool_choice=NAMED_LOOKUP_CHOICE,
                parallel_tool_calls=False,
                response_format=JSON_SCHEMA_FORMAT,
            ),
        ) == ChatRequestTraits(
            offers_tools=True,
            tool_choice_mode=ChatToolChoiceMode.FUNCTION,
            parallel_tool_calls=False,
            response_format_type=ChatResponseFormatType.JSON_SCHEMA,
            strict_schema=True,
            temperature=0.4,
        )

    @pytest.mark.parametrize(
        ("tool_choice", "expected"),
        [
            ("none", ChatToolChoiceMode.NONE),
            ("auto", ChatToolChoiceMode.AUTO),
            ("required", ChatToolChoiceMode.REQUIRED),
        ],
    )
    def test_a_mode_tool_choice_is_its_policy_mode(self, tool_choice: str, expected: ChatToolChoiceMode) -> None:
        assert _traits(_body(tools=[LOOKUP_TOOL], tool_choice=tool_choice)).tool_choice_mode is expected

    def test_a_json_object_format_is_not_a_strict_schema(self) -> None:
        traits = _traits(_body(response_format={"type": "json_object"}))

        assert traits.response_format_type is ChatResponseFormatType.JSON_OBJECT
        assert traits.strict_schema is False


class TestToolsTemperature:
    def test_tools_under_auto_store_the_forwarded_temperature(self) -> None:
        assert chat_tools_temperature(_traits(_body(tools=[LOOKUP_TOOL], tool_choice="auto", temperature=1.5))) == 1.5

    def test_tools_without_a_temperature_store_the_default(self) -> None:
        assert chat_tools_temperature(_traits(_body(tools=[LOOKUP_TOOL]))) == 1.0

    def test_tools_under_none_store_nothing(self) -> None:
        assert chat_tools_temperature(_traits(_body(tools=[LOOKUP_TOOL], tool_choice="none", temperature=1.5))) is None

    def test_a_request_without_tools_stores_nothing(self) -> None:
        assert chat_tools_temperature(_traits(_body(temperature=1.5))) is None


class TestRequiredGuarantees:
    """Under strict conformance a request also needs the guarantees its traits call for (ADR 0036)."""

    def test_every_strict_request_needs_the_prompt_overflow_guarantee(self) -> None:
        assert _required_features(_body(), ChatConformance.STRICT) == {ChatGuarantee.PROMPT_OVERFLOW_REFUSED}

    def test_best_effort_needs_no_guarantee(self) -> None:
        assert _required_features(
            _body(tools=[LOOKUP_TOOL], tool_choice="required", parallel_tool_calls=False, response_format=JSON_SCHEMA_FORMAT),
        ) == {ChatFeature.TOOLS, ChatFeature.JSON_SCHEMA, ChatFeature.TOOLS_WITH_RESPONSE_FORMAT}

    def test_tools_under_auto_need_no_tool_guarantee(self) -> None:
        assert _required_features(_body(tools=[LOOKUP_TOOL]), ChatConformance.STRICT) == {
            ChatFeature.TOOLS,
            ChatGuarantee.PROMPT_OVERFLOW_REFUSED,
        }

    @pytest.mark.parametrize("tool_choice", ["required", NAMED_LOOKUP_CHOICE])
    def test_a_forcing_tool_choice_needs_the_tool_choice_guarantee(self, tool_choice: str | dict[str, Any]) -> None:
        assert _required_features(_body(tools=[LOOKUP_TOOL], tool_choice=tool_choice), ChatConformance.STRICT) == {
            ChatFeature.TOOLS,
            ChatGuarantee.TOOL_CHOICE_ENFORCED,
            ChatGuarantee.PROMPT_OVERFLOW_REFUSED,
        }

    def test_tool_choice_none_needs_no_tool_guarantee(self) -> None:
        assert _required_features(
            _body(tools=[LOOKUP_TOOL], tool_choice="none", parallel_tool_calls=False),
            ChatConformance.STRICT,
        ) == {ChatGuarantee.PROMPT_OVERFLOW_REFUSED}

    def test_tool_choice_none_with_a_tool_history_needs_the_tools_feature_and_no_tool_guarantee(self) -> None:
        assert _required_features(
            _body(messages=TOOL_HISTORY, tools=[LOOKUP_TOOL], tool_choice="none"),
            ChatConformance.STRICT,
        ) == {ChatFeature.TOOLS, ChatGuarantee.PROMPT_OVERFLOW_REFUSED}

    def test_tools_without_parallel_calls_need_the_single_call_guarantee(self) -> None:
        assert _required_features(_body(tools=[LOOKUP_TOOL], parallel_tool_calls=False), ChatConformance.STRICT) == {
            ChatFeature.TOOLS,
            ChatGuarantee.SINGLE_TOOL_CALL_ENFORCED,
            ChatGuarantee.PROMPT_OVERFLOW_REFUSED,
        }

    def test_a_strict_json_schema_needs_the_structured_output_guarantee(self) -> None:
        assert _required_features(_body(response_format=JSON_SCHEMA_FORMAT), ChatConformance.STRICT) == {
            ChatFeature.JSON_SCHEMA,
            ChatGuarantee.STRUCTURED_OUTPUT_ENFORCED,
            ChatGuarantee.PROMPT_OVERFLOW_REFUSED,
        }

    def test_a_non_strict_json_schema_needs_no_structured_output_guarantee(self) -> None:
        loose_format = {**JSON_SCHEMA_FORMAT, "json_schema": {**JSON_SCHEMA_FORMAT["json_schema"], "strict": False}}

        assert _required_features(_body(response_format=loose_format), ChatConformance.STRICT) == {
            ChatFeature.JSON_SCHEMA,
            ChatGuarantee.PROMPT_OVERFLOW_REFUSED,
        }


class TestWorkerChatExtras:
    """The worker's copy of the extras is narrowed to what the tool choice lets the model call."""

    def test_tool_choice_none_forwards_no_tool_keys(self) -> None:
        chat_extras = build_chat_extras(
            parse_chat_request(
                _body(tools=[LOOKUP_TOOL], tool_choice="none", parallel_tool_calls=False, response_format={"type": "json_object"}),
            ),
        )

        assert build_worker_chat_extras(chat_extras) == {"response_format": {"type": "json_object"}}

    def test_a_named_choice_forwards_only_that_function_under_required(self) -> None:
        chat_extras = build_chat_extras(
            parse_chat_request(_body(tools=[OTHER_TOOL, LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE, parallel_tool_calls=False)),
        )

        assert build_worker_chat_extras(chat_extras) == {
            "tools": [LOOKUP_TOOL],
            "tool_choice": "required",
            "parallel_tool_calls": False,
        }

    @pytest.mark.parametrize("tool_choice", ["auto", "required"])
    def test_auto_and_required_forward_the_extras_as_stored(self, tool_choice: str) -> None:
        chat_extras = build_chat_extras(parse_chat_request(_body(tools=[OTHER_TOOL, LOOKUP_TOOL], tool_choice=tool_choice)))

        assert build_worker_chat_extras(chat_extras) == chat_extras

    def test_narrowing_leaves_the_stored_extras_unchanged(self) -> None:
        chat_extras = build_chat_extras(parse_chat_request(_body(tools=[OTHER_TOOL, LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE)))
        stored_copy = {**chat_extras, "tools": list(chat_extras["tools"])}

        build_worker_chat_extras(chat_extras)

        assert chat_extras == stored_copy

    def test_a_request_without_extras_forwards_nothing(self) -> None:
        assert build_worker_chat_extras(None) == {}


class TestStreamHint:
    """``stream: true`` is stored as the stream hint and reaches the job payload, without changing the request."""

    def test_a_streamed_request_stores_the_hint(self) -> None:
        chat_extras = build_stored_chat_extras(parse_chat_request(_body(stream=True)))

        assert chat_extras == {CHAT_STREAM_PROGRESS_KEY: True}
        assert streams_progress(chat_extras)

    @pytest.mark.parametrize("stream", [False, None])
    def test_a_request_that_does_not_stream_stores_no_hint(self, stream: bool | None) -> None:
        chat_extras = build_stored_chat_extras(parse_chat_request(_body(stream=stream)))

        assert chat_extras is None
        assert not streams_progress(chat_extras)

    def test_the_hint_is_stored_beside_the_applied_keys(self) -> None:
        chat_request = parse_chat_request(_body(stream=True, response_format={"type": "json_object"}))

        assert build_stored_chat_extras(chat_request) == {
            "response_format": {"type": "json_object"},
            CHAT_STREAM_PROGRESS_KEY: True,
        }

    def test_the_hint_reaches_the_job_payload(self) -> None:
        chat_extras = build_stored_chat_extras(parse_chat_request(_body(stream=True)))

        assert build_worker_chat_extras(chat_extras) == {"stream_progress": True}

    def test_the_hint_leaves_the_operation_fingerprint_unchanged(self) -> None:
        streamed = build_operation_fingerprint(parse_chat_request(_body(stream=True)))

        assert streamed == build_operation_fingerprint(parse_chat_request(_body()))

    def test_asking_for_reasoning_leaves_the_operation_fingerprint_unchanged(self) -> None:
        """``horde.include_reasoning`` changes what the front delivers and leaves the work unchanged."""
        with_reasoning = build_operation_fingerprint(parse_chat_request(_body(horde={"include_reasoning": True})))

        assert with_reasoning == build_operation_fingerprint(parse_chat_request(_body()))


class TestRequiredFeatures:
    """The capabilities a request needs, which apply under every conformance mode."""

    def test_a_request_at_default_values_needs_no_feature(self) -> None:
        assert _required_features(_body(frequency_penalty=0, presence_penalty=0.0)) == set()

    def test_each_non_default_optional_value_needs_its_feature(self) -> None:
        assert _required_features(_body(seed=7, frequency_penalty=0.2, presence_penalty=0.3)) == {
            ChatFeature.SEED,
            ChatFeature.FREQUENCY_PENALTY,
            ChatFeature.PRESENCE_PENALTY,
        }

    def test_a_zero_seed_still_needs_the_seed_feature(self) -> None:
        assert _required_features(_body(seed=0)) == {ChatFeature.SEED}

    def test_image_parts_need_the_vision_feature(self) -> None:
        image_part = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}

        assert _required_features(_body(messages=[{"role": "user", "content": [image_part]}])) == {ChatFeature.VISION}

    def test_offered_tools_need_the_tools_feature(self) -> None:
        assert _required_features(_body(tools=[LOOKUP_TOOL])) == {ChatFeature.TOOLS}
        assert _required_features(_body(tools=[LOOKUP_TOOL], tool_choice="required")) == {ChatFeature.TOOLS}

    def test_tools_under_tool_choice_none_need_no_feature(self) -> None:
        assert _required_features(_body(tools=[LOOKUP_TOOL], tool_choice="none")) == set()

    def test_tools_under_tool_choice_none_with_a_tool_history_need_the_tools_feature(self) -> None:
        assert _required_features(_body(messages=TOOL_HISTORY, tools=[LOOKUP_TOOL], tool_choice="none")) == {ChatFeature.TOOLS}

    def test_a_tool_history_needs_the_tools_feature_without_offered_tools(self) -> None:
        assert _required_features(_body(messages=TOOL_HISTORY)) == {ChatFeature.TOOLS}

    @pytest.mark.parametrize(
        ("response_format", "format_feature"),
        [
            ({"type": "json_object"}, ChatFeature.JSON_OBJECT),
            (JSON_SCHEMA_FORMAT, ChatFeature.JSON_SCHEMA),
        ],
    )
    @pytest.mark.parametrize("conformance", list(ChatConformance))
    def test_forwarded_tools_beside_a_response_format_need_the_combination_feature_under_every_mode(
        self,
        response_format: dict[str, Any],
        format_feature: ChatFeature,
        conformance: ChatConformance,
    ) -> None:
        needed = _required_features(_body(tools=[LOOKUP_TOOL], response_format=response_format), conformance)

        assert {ChatFeature.TOOLS, format_feature, ChatFeature.TOOLS_WITH_RESPONSE_FORMAT} <= needed

    def test_tools_under_tool_choice_none_beside_a_response_format_need_no_combination_feature(self) -> None:
        needed = _required_features(_body(tools=[LOOKUP_TOOL], tool_choice="none", response_format={"type": "json_object"}))

        assert needed == {ChatFeature.JSON_OBJECT}

    def test_a_tool_history_beside_a_response_format_needs_no_combination_feature(self) -> None:
        needed = _required_features(_body(messages=TOOL_HISTORY, response_format={"type": "json_object"}))

        assert needed == {ChatFeature.TOOLS, ChatFeature.JSON_OBJECT}

    @pytest.mark.parametrize(
        ("response_format", "expected"),
        [
            ({"type": "text"}, set()),
            ({"type": "json_object"}, {ChatFeature.JSON_OBJECT}),
            (JSON_SCHEMA_FORMAT, {ChatFeature.JSON_SCHEMA}),
        ],
    )
    def test_a_structured_response_format_needs_the_feature_of_its_type(
        self,
        response_format: dict[str, Any],
        expected: set[ChatFeature],
    ) -> None:
        assert _required_features(_body(response_format=response_format)) == expected


REQUIREMENT_PARITY_BODIES: dict[str, dict[str, Any]] = {
    "plain": _body(),
    "default_values": _body(frequency_penalty=0, presence_penalty=0.0, response_format={"type": "text"}),
    "sampling": _body(seed=0, frequency_penalty=0.2, presence_penalty=-0.3, temperature=0.4, top_p=0.9),
    "image": _body(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this."},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                ],
            },
        ],
    ),
    "tools_auto": _body(tools=[LOOKUP_TOOL]),
    "tools_required_serial": _body(tools=[LOOKUP_TOOL, OTHER_TOOL], tool_choice="required", parallel_tool_calls=False),
    "tools_named": _body(tools=[LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE, temperature=1.5),
    "tools_none_with_history": _body(messages=TOOL_HISTORY, tools=[LOOKUP_TOOL], tool_choice="none"),
    "history_without_tools": _body(messages=TOOL_HISTORY),
    "json_object": _body(response_format={"type": "json_object"}),
    "strict_schema_with_tools": _body(tools=[LOOKUP_TOOL], response_format=JSON_SCHEMA_FORMAT),
    "best_effort": _body(
        tools=[LOOKUP_TOOL], tool_choice="required", response_format=JSON_SCHEMA_FORMAT, horde={"conformance": "best_effort"}
    ),
    "explicit_strict": _body(seed=3, horde={"conformance": "strict"}),
}
"""Request bodies covering every requirement rule, for comparing the server's derivation with the SDK's."""


class TestRequirementParity:
    """The server derives requirements from the stored shapes and the SDK from the request body; both agree, so a
    client's local check and the server's routing cannot drift apart."""

    @pytest.mark.parametrize("body", list(REQUIREMENT_PARITY_BODIES.values()), ids=list(REQUIREMENT_PARITY_BODIES))
    def test_server_and_sdk_derive_the_same_requirements(self, body: dict[str, Any]) -> None:
        chat_request = parse_chat_request(body)
        text_params = build_text_params(chat_request)
        chat_extras = build_chat_extras(chat_request)
        sdk_requirements = chat_request_requirements(body)

        server_requirements = required_chat_features(
            text_params,
            serialize_messages(chat_request),
            chat_extras,
            chat_request.horde.conformance,
        )

        assert server_requirements == set(sdk_requirements.features | sdk_requirements.guarantees)
        assert build_chat_request_traits(text_params, chat_extras) == sdk_requirements.traits
        assert chat_request.horde.conformance is sdk_requirements.conformance


class TestTools:
    def test_tools_and_a_response_format_are_stored_as_sent(self) -> None:
        named_choice = {"type": "function", "function": {"name": "lookup_weather"}}
        chat_request = parse_chat_request(
            _body(tools=[LOOKUP_TOOL], tool_choice=named_choice, parallel_tool_calls=False, response_format=JSON_SCHEMA_FORMAT),
        )

        assert build_chat_extras(chat_request) == {
            "tools": [LOOKUP_TOOL],
            "tool_choice": named_choice,
            "parallel_tool_calls": False,
            "response_format": JSON_SCHEMA_FORMAT,
        }

    def test_a_mode_tool_choice_is_stored_as_its_name(self) -> None:
        chat_request = parse_chat_request(_body(tools=[LOOKUP_TOOL], tool_choice="auto"))

        assert build_chat_extras(chat_request) == {"tools": [LOOKUP_TOOL], "tool_choice": "auto"}

    @pytest.mark.parametrize(
        "default_values",
        [
            {},
            {"tools": [], "tool_choice": None, "response_format": None, "parallel_tool_calls": None},
            {"response_format": {"type": "text"}},
        ],
    )
    def test_values_meaning_the_default_store_nothing(self, default_values: dict[str, Any]) -> None:
        assert build_chat_extras(parse_chat_request(_body(**default_values))) is None

    def test_tool_choice_none_beside_tools_is_stored_as_sent(self) -> None:
        chat_request = parse_chat_request(_body(tools=[LOOKUP_TOOL], tool_choice="none", parallel_tool_calls=True))

        assert build_chat_extras(chat_request) == {"tools": [LOOKUP_TOOL], "tool_choice": "none", "parallel_tool_calls": True}

    def test_a_json_object_format_is_stored_without_tools(self) -> None:
        chat_request = parse_chat_request(_body(response_format={"type": "json_object"}))

        assert build_chat_extras(chat_request) == {"response_format": {"type": "json_object"}}

    @pytest.mark.parametrize(
        "tool_choice",
        [
            "none",
            "auto",
            "required",
            {"type": "function", "function": {"name": "lookup_weather"}},
        ],
    )
    def test_a_tool_choice_without_tools_is_a_conflict(self, tool_choice: Any) -> None:
        for tools in (None, []):
            rejection = _rejection(_body(tools=tools, tool_choice=tool_choice))

            assert rejection.rc == CHAT_REQUEST_CONFLICT_RC
            assert "'tool_choice' is only allowed when 'tools' are specified." in rejection.specific

    @pytest.mark.parametrize("parallel_tool_calls", [True, False])
    def test_parallel_tool_calls_without_tools_is_a_conflict(self, parallel_tool_calls: bool) -> None:
        rejection = _rejection(_body(parallel_tool_calls=parallel_tool_calls))

        assert rejection.rc == CHAT_REQUEST_CONFLICT_RC
        assert "'parallel_tool_calls' is only allowed when 'tools' are specified." in rejection.specific

    def test_null_tool_choice_and_parallel_tool_calls_without_tools_are_accepted(self) -> None:
        assert build_chat_extras(parse_chat_request(_body(tool_choice=None, parallel_tool_calls=None))) is None

    def test_forcing_a_function_the_tools_do_not_define_is_a_conflict(self) -> None:
        rejection = _rejection(_body(tools=[LOOKUP_TOOL], tool_choice={"type": "function", "function": {"name": "send_email"}}))

        assert rejection.rc == CHAT_REQUEST_CONFLICT_RC
        assert "send_email" in rejection.specific

    def test_more_tools_than_the_cap_are_refused(self) -> None:
        tools = [{"type": "function", "function": {"name": f"tool_{index}"}} for index in range(MAX_CHAT_TOOL_COUNT + 1)]

        rejection = _rejection(_body(tools=tools))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert str(MAX_CHAT_TOOL_COUNT) in rejection.specific

    def test_the_tool_cap_itself_is_accepted(self) -> None:
        tools = [{"type": "function", "function": {"name": f"tool_{index}"}} for index in range(MAX_CHAT_TOOL_COUNT)]

        assert len(build_chat_extras(parse_chat_request(_body(tools=tools)))["tools"]) == MAX_CHAT_TOOL_COUNT

    def test_a_tool_spec_over_the_byte_cap_is_refused(self) -> None:
        large_tool = {"type": "function", "function": {"name": "big", "description": "x" * MAX_CHAT_TOOL_SPEC_BYTES}}

        rejection = _rejection(_body(tools=[large_tool]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert str(MAX_CHAT_TOOL_SPEC_BYTES) in rejection.specific

    def test_the_response_format_counts_towards_the_tool_spec_bound(self) -> None:
        large_format = {"type": "json_schema", "json_schema": {"name": "big", "schema": {"description": "x" * MAX_CHAT_TOOL_SPEC_BYTES}}}

        assert _rejection(_body(response_format=large_format)).rc == UNSUPPORTED_CHAT_PARAMETER_RC

    @pytest.mark.parametrize(
        "malformed_tools",
        [
            [{"type": "function", "function": {"name": "has space"}}],
            [{"type": "function", "function": {"name": ""}}],
            [{"type": "retrieval", "function": {"name": "lookup"}}],
            [{"type": "function", "function": {"name": "lookup", "unknown": 1}}],
        ],
    )
    def test_a_malformed_tool_is_refused(self, malformed_tools: list[dict[str, Any]]) -> None:
        assert _rejection(_body(tools=malformed_tools)).rc == "BadRequest"

    @pytest.mark.parametrize(
        "malformed_format",
        [
            {"type": "json_schema", "json_schema": {"name": "colour"}},
            {"type": "json_schema"},
            {"type": "yaml"},
        ],
    )
    def test_a_malformed_response_format_is_refused(self, malformed_format: dict[str, Any]) -> None:
        assert "response_format" in _rejection(_body(response_format=malformed_format)).specific

    @pytest.mark.parametrize(
        "overrides",
        [
            {"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"properties": {"bad\x00key": {}}}}}]},
            {"response_format": {"type": "json_schema", "json_schema": {"name": "n", "schema": {"properties": {"bad\x00key": {}}}}}},
        ],
        ids=["tool-parameters", "response-schema"],
    )
    def test_a_nul_in_a_schema_key_is_refused(self, overrides: dict[str, Any]) -> None:
        assert _rejection(_body(**overrides)).rc == "NulByteInPayload"


class TestToolMessages:
    def test_a_tool_history_is_stored_unchanged(self) -> None:
        assert serialize_messages(parse_chat_request(_body(messages=TOOL_HISTORY))) == TOOL_HISTORY

    def test_an_assistant_message_omitting_content_with_tool_calls_is_stored_with_null_content(self) -> None:
        messages = [TOOL_HISTORY[0], {"role": "assistant", "tool_calls": [LOOKUP_CALL]}, TOOL_HISTORY[2]]

        assert serialize_messages(parse_chat_request(_body(messages=messages)))[1] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [LOOKUP_CALL],
        }

    def test_a_tool_message_answering_no_earlier_call_is_refused_by_path(self) -> None:
        messages = [*TOOL_HISTORY, {"role": "tool", "content": "Sunny", "tool_call_id": "call_unknown"}]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "'messages[3].tool_call_id'" in rejection.specific
        assert "'tool_call_id' of 'call_unknown' not found in 'tool_calls' of previous message." in rejection.specific

    def test_a_tool_message_answering_a_later_call_is_refused(self) -> None:
        messages = [TOOL_HISTORY[0], TOOL_HISTORY[2], TOOL_HISTORY[1]]

        assert "'messages[1].role'" in _rejection(_body(messages=messages)).specific

    def test_a_tool_message_needs_its_tool_call_id(self) -> None:
        rejection = _rejection(_body(messages=[*TOOL_HISTORY[:2], {"role": "tool", "content": "Rain"}]))

        assert "tool_call_id" in rejection.specific

    @pytest.mark.parametrize("role", ["user", "assistant", "system"])
    def test_null_content_without_tool_calls_is_refused(self, role: str) -> None:
        rejection = _rejection(_body(messages=[{"role": role, "content": None}]))

        assert "content" in rejection.specific

    def test_an_empty_tool_calls_list_is_refused(self) -> None:
        rejection = _rejection(_body(messages=[{"role": "assistant", "content": "x", "tool_calls": []}]))

        assert "tool_calls" in rejection.specific

    def test_flattening_renders_tool_calls_tool_results_and_the_tools_but_not_the_response_schema(self) -> None:
        chat_request = parse_chat_request(_body(messages=TOOL_HISTORY, tools=[LOOKUP_TOOL], response_format=JSON_SCHEMA_FORMAT))

        flattened = flatten_messages(serialize_messages(chat_request), build_chat_extras(chat_request))

        assert flattened.split("\n") == [
            "user: Weather in Oslo?",
            "assistant: ",
            '[{"id":"call_abc","type":"function","function":{"name":"lookup_weather","arguments":"{\\"city\\":\\"Oslo\\"}"}}]',
            "tool: Rain, 7C",
            '[{"type":"function","function":{"name":"lookup_weather","description":"Look up the weather.",'
            '"parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}]',
        ]

    def test_flattening_leaves_a_json_object_format_out(self) -> None:
        chat_request = parse_chat_request(_body(response_format={"type": "json_object"}))

        assert flatten_messages(serialize_messages(chat_request), build_chat_extras(chat_request)) == "system: Be brief.\nuser: Hello?"

    def test_rewriting_leaves_a_null_content_and_the_tool_calls_alone(self) -> None:
        assert rewrite_message_texts(TOOL_HISTORY, str.upper) == [
            {"role": "user", "content": "WEATHER IN OSLO?"},
            TOOL_HISTORY[1],
            {"role": "tool", "content": "RAIN, 7C", "tool_call_id": "call_abc"},
        ]


def _assistant_calls(*call_ids: str) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [{**LOOKUP_CALL, "id": call_id} for call_id in call_ids]}


def _tool_result(call_id: str) -> dict[str, Any]:
    return {"role": "tool", "content": "Rain, 7C", "tool_call_id": call_id}


QUESTION: dict[str, Any] = {"role": "user", "content": "Weather in Oslo and Bergen?"}
"""The user turn that opens each tool history below."""


class TestToolHistoryRule:
    """An assistant message's tool calls are answered right after it, each exactly once, as OpenAI requires."""

    def test_results_answering_every_call_in_any_order_are_accepted(self) -> None:
        messages = [QUESTION, _assistant_calls("call_a", "call_b"), _tool_result("call_b"), _tool_result("call_a")]

        assert len(parse_chat_request(_body(messages=messages)).messages) == 4

    def test_an_id_may_recur_in_a_later_round(self) -> None:
        messages = [QUESTION, _assistant_calls("call_a"), _tool_result("call_a"), _assistant_calls("call_a"), _tool_result("call_a")]

        assert len(parse_chat_request(_body(messages=messages)).messages) == 5

    def test_an_unanswered_call_followed_by_another_message_is_refused(self) -> None:
        messages = [QUESTION, _assistant_calls("call_a", "call_b"), _tool_result("call_a"), {"role": "user", "content": "Well?"}]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific == (
            "'messages[3].role': An assistant message with 'tool_calls' must be followed by tool messages responding to "
            "each 'tool_call_id'. The following tool_call_ids did not have response messages: call_b"
        )

    def test_a_history_ending_with_unanswered_calls_is_refused(self) -> None:
        rejection = _rejection(_body(messages=[QUESTION, _assistant_calls("call_a")]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific.startswith("'messages': An assistant message with 'tool_calls' must be followed")

    def test_a_second_answer_to_one_call_is_refused(self) -> None:
        messages = [QUESTION, _assistant_calls("call_a"), _tool_result("call_a"), _tool_result("call_a")]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific == (
            "'messages[3].role': Invalid parameter: messages with role 'tool' must be a response to a preceding message with 'tool_calls'."
        )

    def test_an_answer_to_a_call_of_an_earlier_round_is_refused(self) -> None:
        messages = [
            QUESTION,
            _assistant_calls("call_a"),
            _tool_result("call_a"),
            {"role": "user", "content": "And?"},
            _tool_result("call_a"),
        ]

        assert "'messages[4].role'" in _rejection(_body(messages=messages)).specific

    def test_ids_repeated_within_one_assistant_message_are_refused(self) -> None:
        messages = [QUESTION, _assistant_calls("call_a", "call_a"), _tool_result("call_a")]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific.startswith("'messages[1].tool_calls': ")
        assert "'call_a'" in rejection.specific


def _strict_format(schema: dict[str, Any], *, strict: bool | None = True) -> dict[str, Any]:
    json_schema: dict[str, Any] = {"name": "n", "schema": schema}
    if strict is not None:
        json_schema["strict"] = strict
    return {"type": "json_schema", "json_schema": json_schema}


def _closed_object(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


class TestStrictSchemaRules:
    """Under ``strict: true`` every object schema is closed and requires every property, as OpenAI requires."""

    def test_a_closed_schema_with_every_keyword_openai_applies_is_accepted(self) -> None:
        schema = _closed_object(
            x={"type": "number", "minimum": 2},
            tags={"type": "array", "items": _closed_object(label={"type": "string"})},
            ref={"$ref": "#/$defs/point"},
        )
        schema["$defs"] = {"point": _closed_object(x={"type": "number"})}

        assert parse_chat_request(_body(response_format=_strict_format(schema))).response_format is not None

    def test_a_root_object_without_closed_additional_properties_is_refused_with_openais_message(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}

        rejection = _rejection(_body(response_format=_strict_format(schema)))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific == (
            "'response_format': Invalid schema for response_format 'n': In context=(), 'additionalProperties' is "
            "required to be supplied and to be false."
        )

    def test_a_property_missing_from_required_is_refused_with_openais_message(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x"],
            "additionalProperties": False,
        }

        rejection = _rejection(_body(response_format=_strict_format(schema)))

        assert rejection.specific == (
            "'response_format': Invalid schema for response_format 'n': In context=(), 'required' is required to be "
            "supplied and to be an array including every key in properties. Missing 'y'."
        )

    @pytest.mark.parametrize(
        ("schema", "context"),
        [
            (
                _closed_object(inner={"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}),
                "('properties', 'inner')",
            ),
            (_closed_object(items={"type": "array", "items": {"type": "object", "properties": {}}}), "('properties', 'items', 'items')"),
            (
                {**_closed_object(), "$defs": {"point": {"type": "object", "properties": {"x": {}}, "additionalProperties": False}}},
                "('$defs', 'point')",
            ),
            (
                _closed_object(choice={"anyOf": [{"type": "string"}, {"type": "object", "properties": {}}]}),
                "('properties', 'choice', 'anyOf', 1)",
            ),
        ],
        ids=["nested-property", "array-items", "defs", "any-of"],
    )
    def test_the_rules_apply_to_every_subschema(self, schema: dict[str, Any], context: str) -> None:
        rejection = _rejection(_body(response_format=_strict_format(schema)))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"In context={context}, " in rejection.specific

    @pytest.mark.parametrize("strict", [False, None], ids=["strict-false", "strict-unset"])
    def test_a_non_strict_schema_is_not_checked_structurally(self, strict: bool | None) -> None:
        open_schema = {"type": "object", "properties": {"x": {"type": "number"}}}

        assert parse_chat_request(_body(response_format=_strict_format(open_schema, strict=strict))).response_format is not None


def _weather_tool(parameters: dict[str, Any], *, strict: bool | None = True) -> dict[str, Any]:
    function: dict[str, Any] = {"name": "get_weather", "parameters": parameters}
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


class TestStrictToolSchemaRules:
    """A tool definition with ``strict: true`` follows the strict schema rules a ``strict`` response format follows."""

    def test_a_closed_strict_tool_is_accepted_and_forwarded_with_its_strict_flag(self) -> None:
        tool = _weather_tool(_closed_object(location={"type": "string"}, unit={"type": "string"}))

        chat_extras = build_chat_extras(parse_chat_request(_body(tools=[tool])))

        assert chat_extras is not None
        assert chat_extras["tools"][0]["function"]["strict"] is True

    def test_a_strict_tool_without_closed_additional_properties_is_refused_with_openais_message(self) -> None:
        parameters = {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}

        rejection = _rejection(_body(tools=[_weather_tool(parameters)]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific == (
            "'tools[0].function.parameters': Invalid schema for function 'get_weather': In context=(), "
            "'additionalProperties' is required to be supplied and to be false."
        )

    def test_a_strict_tool_property_missing_from_required_is_refused_with_openais_message(self) -> None:
        parameters = {
            "type": "object",
            "properties": {"location": {"type": "string"}, "unit": {"type": "string"}},
            "required": ["location"],
            "additionalProperties": False,
        }
        other_tool = {"type": "function", "function": {"name": "other"}}

        rejection = _rejection(_body(tools=[other_tool, _weather_tool(parameters)]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert rejection.specific == (
            "'tools[1].function.parameters': Invalid schema for function 'get_weather': In context=(), 'required' is "
            "required to be supplied and to be an array including every key in properties. Missing 'unit'."
        )

    @pytest.mark.parametrize("strict", [False, None], ids=["strict-false", "strict-unset"])
    def test_a_non_strict_tool_with_an_optional_property_is_accepted(self, strict: bool | None) -> None:
        parameters = {"type": "object", "properties": {"location": {"type": "string"}, "unit": {"type": "string"}}}

        assert parse_chat_request(_body(tools=[_weather_tool(parameters, strict=strict)])).tools is not None


def _nested_schema(levels: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(levels):
        schema = {"type": "object", "properties": {"x": schema}, "required": ["x"], "additionalProperties": False}
    return schema


class TestSchemaNesting:
    """Tool parameters and response schemas nest at most ``MAX_CHAT_SCHEMA_NESTING`` levels below their root."""

    def test_a_strict_schema_at_the_nesting_limit_is_accepted(self) -> None:
        response_format = _strict_format(_nested_schema(MAX_CHAT_SCHEMA_NESTING))
        tool = {"type": "function", "function": {"name": "deep", "parameters": _nested_schema(MAX_CHAT_SCHEMA_NESTING)}}

        chat_request = parse_chat_request(_body(tools=[tool], response_format=response_format))

        assert build_chat_extras(chat_request) is not None

    @pytest.mark.parametrize("levels", [MAX_CHAT_SCHEMA_NESTING + 1, 300])
    def test_a_response_schema_nested_deeper_is_refused_as_unsupported(self, levels: int) -> None:
        rejection = _rejection(_body(response_format=_strict_format(_nested_schema(levels), strict=None)))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"at most {MAX_CHAT_SCHEMA_NESTING} levels" in rejection.specific

    def test_tool_parameters_nested_deeper_are_refused_as_unsupported(self) -> None:
        tool = {"type": "function", "function": {"name": "deep", "parameters": _nested_schema(300)}}

        rejection = _rejection(_body(tools=[tool]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"at most {MAX_CHAT_SCHEMA_NESTING} levels" in rejection.specific


class TestOpenAIGuards:
    """``horde.openai_guards`` restores the refusals OpenAI applies and the Horde relaxes."""

    def test_a_json_object_request_without_the_word_json_is_served_by_default(self) -> None:
        assert parse_chat_request(_body(response_format={"type": "json_object"})).horde.openai_guards is False

    def test_the_guard_refuses_a_json_object_request_without_the_word_json(self) -> None:
        rejection = _rejection(_body(response_format={"type": "json_object"}, horde={"openai_guards": True}))

        assert rejection.rc == CHAT_REQUEST_CONFLICT_RC
        assert rejection.specific == (
            "'messages': 'messages' must contain the word 'json' in some form, to use 'response_format' of type 'json_object'."
        )

    def test_the_guard_accepts_the_word_in_any_case_in_a_text_part(self) -> None:
        messages = [{"role": "user", "content": [{"type": "text", "text": "Answer as JSON."}]}]

        chat_request = parse_chat_request(_body(messages=messages, response_format={"type": "json_object"}, horde={"openai_guards": True}))

        assert chat_request.horde.openai_guards is True

    def test_the_guard_leaves_a_json_schema_request_alone(self) -> None:
        assert parse_chat_request(_body(response_format=JSON_SCHEMA_FORMAT, horde={"openai_guards": True})).response_format is not None


class TestSubmittedReply:
    def test_null_content_is_accepted_with_tool_calls(self) -> None:
        reply = ChatSubmittedMessage.model_validate({"role": "assistant", "content": None, "tool_calls": [LOOKUP_CALL]})

        assert build_stored_reply(reply) == {"role": "assistant", "content": None, "tool_calls": [LOOKUP_CALL]}

    @pytest.mark.parametrize(
        "submitted",
        [
            {"role": "assistant", "content": None},
            {"role": "assistant"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "assistant", "content": "x", "tool_calls": [{**LOOKUP_CALL, "id": ""}]},
        ],
    )
    def test_a_reply_without_content_or_with_malformed_tool_calls_is_refused(self, submitted: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ChatSubmittedMessage.model_validate(submitted)

    def test_a_text_reply_is_stored_without_a_tool_calls_key_and_with_nul_replaced(self) -> None:
        reply = ChatSubmittedMessage.model_validate({"role": "assistant", "content": "a\x00b"})

        assert build_stored_reply(reply) == {"role": "assistant", "content": "a\ufffdb"}

    def test_reasoning_is_stored_beside_the_content_with_nul_replaced(self) -> None:
        reply = ChatSubmittedMessage.model_validate({"role": "assistant", "content": "Hi.", "reasoning_content": "Greet\x00."})

        assert build_stored_reply(reply) == {"role": "assistant", "content": "Hi.", "reasoning_content": "Greet\ufffd."}

    def test_a_reply_without_reasoning_is_stored_without_a_reasoning_key(self) -> None:
        reply = ChatSubmittedMessage.model_validate({"role": "assistant", "content": "Hi."})

        assert "reasoning_content" not in build_stored_reply(reply)

    def test_reasoning_alone_does_not_make_a_reply(self) -> None:
        """Reasoning is never the reply, so a message with neither content nor tool calls is refused with it."""
        with pytest.raises(ValidationError):
            ChatSubmittedMessage.model_validate({"role": "assistant", "content": None, "reasoning_content": "Thinking."})

    def test_reasoning_is_not_charged(self) -> None:
        reply = build_stored_reply(
            ChatSubmittedMessage.model_validate({"role": "assistant", "content": "four", "reasoning_content": "x" * 400})
        )

        assert chat_reply_characters(reply) == 4

    def test_the_charged_characters_include_each_tool_call_name_and_arguments(self) -> None:
        reply = {"role": "assistant", "content": "Checking.", "tool_calls": [LOOKUP_CALL, LOOKUP_CALL]}

        assert chat_reply_characters(reply) == len("Checking.") + 2 * (len("lookup_weather") + len('{"city":"Oslo"}'))
        assert chat_reply_characters({"role": "assistant", "content": None, "tool_calls": [LOOKUP_CALL]}) == 29
        assert chat_reply_characters({"role": "assistant", "content": "four"}) == 4

    def test_a_reply_that_calls_tools_is_not_an_empty_generation(self) -> None:
        assert is_empty_submission("", {"role": "assistant", "content": None, "tool_calls": [LOOKUP_CALL]}) is False
        assert is_empty_submission("", {"role": "assistant", "content": ""}) is True
        assert is_empty_submission("", None) is True


COLOUR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}
"""A JSON Schema for one named colour."""


def _stored_extras(**request_overrides: Any) -> dict[str, Any] | None:
    return build_chat_extras(parse_chat_request(_body(**request_overrides)))


def _schema_format(schema: dict[str, Any], *, strict: bool | None = True) -> dict[str, Any]:
    json_schema: dict[str, Any] = {"name": "colour", "schema": schema}
    if strict is not None:
        json_schema["strict"] = strict
    return {"type": "json_schema", "json_schema": json_schema}


def _call(name: str = "lookup_weather", call_id: str = "call_abc") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _reply(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None) -> ChatSubmittedMessage:
    return ChatSubmittedMessage.model_validate({"role": "assistant", "content": content, "tool_calls": tool_calls})


def _verification_refusal(
    reply: ChatSubmittedMessage,
    chat_extras: dict[str, Any] | None,
    conformance: ChatConformance = ChatConformance.STRICT,
) -> e.BadRequest:
    with pytest.raises(e.BadRequest) as raised:
        verify_chat_reply(reply, chat_extras, conformance)
    return raised.value


class TestReplyVerification:
    """A submitted reply is checked against the client's stored extras under the request's conformance mode."""

    def test_reasoning_is_not_checked_against_the_response_format(self) -> None:
        """A ``strict`` JSON request accepts JSON content whatever the reasoning before it holds."""
        reply = ChatSubmittedMessage.model_validate({"role": "assistant", "content": "{}", "reasoning_content": "not json"})

        assert verify_chat_reply(reply, _stored_extras(response_format={"type": "json_object"}), ChatConformance.STRICT) == reply

    @pytest.mark.parametrize("conformance", [ChatConformance.STRICT, ChatConformance.BEST_EFFORT])
    def test_reasoning_over_the_text_cap_is_refused_as_invalid(self, conformance: ChatConformance) -> None:
        reply = ChatSubmittedMessage.model_validate(
            {"role": "assistant", "content": "Hi.", "reasoning_content": "x" * (MAX_CHAT_TEXT_BYTES + 1)},
        )

        assert _verification_refusal(reply, None, conformance).rc == "ChatSubmitInvalid"

    def test_reasoning_at_the_text_cap_is_accepted(self) -> None:
        reply = ChatSubmittedMessage.model_validate({"role": "assistant", "content": "Hi.", "reasoning_content": "x" * MAX_CHAT_TEXT_BYTES})

        assert verify_chat_reply(reply, None, ChatConformance.STRICT) == reply

    def test_both_submit_return_codes_are_known(self) -> None:
        assert {CHAT_SUBMIT_INVALID_RC, CHAT_SUBMIT_NONCONFORMANT_RC} <= set(KNOWN_RC)

    def test_a_reply_honouring_the_request_is_returned_unchanged(self) -> None:
        reply = _reply(tool_calls=[_call()])

        assert verify_chat_reply(reply, _stored_extras(tools=[LOOKUP_TOOL], tool_choice="required"), ChatConformance.STRICT) == reply

    def test_a_plain_reply_to_a_request_without_extras_passes(self) -> None:
        reply = _reply("Blue.")

        assert verify_chat_reply(reply, None, ChatConformance.STRICT) == reply

    def test_a_strict_tool_choice_none_refuses_a_call(self) -> None:
        refusal = _verification_refusal(_reply(tool_calls=[_call()]), _stored_extras(tools=[LOOKUP_TOOL], tool_choice="none"))

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert "'none'" in refusal.specific

    def test_a_strict_tool_choice_required_refuses_a_reply_without_a_call(self) -> None:
        refusal = _verification_refusal(_reply("Sunny."), _stored_extras(tools=[LOOKUP_TOOL], tool_choice="required"))

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert "'required'" in refusal.specific

    @pytest.mark.parametrize(
        "tool_calls",
        [None, [_call("send_email")], [_call(), _call("send_email", "call_def")]],
        ids=["no-call", "other-function", "named-and-other-function"],
    )
    def test_a_strict_named_tool_choice_refuses_a_reply_not_calling_only_that_function(
        self, tool_calls: list[dict[str, Any]] | None
    ) -> None:
        chat_extras = _stored_extras(tools=[OTHER_TOOL, LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE)

        refusal = _verification_refusal(_reply("Sunny." if tool_calls is None else None, tool_calls), chat_extras)

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert "forces 'lookup_weather'" in refusal.specific

    @pytest.mark.parametrize(
        ("request_overrides", "reply"),
        [
            ({"tools": [LOOKUP_TOOL], "tool_choice": "none"}, _reply(tool_calls=[_call()])),
            ({"tools": [LOOKUP_TOOL], "tool_choice": "required"}, _reply("Sunny.")),
            ({"tools": [OTHER_TOOL, LOOKUP_TOOL], "tool_choice": NAMED_LOOKUP_CHOICE}, _reply(tool_calls=[_call("send_email")])),
            ({"response_format": {"type": "json_object"}}, _reply("not json")),
            ({"response_format": _schema_format(COLOUR_SCHEMA)}, _reply('{"name": 7}')),
        ],
        ids=["none-with-call", "required-without-call", "named-other-function", "json-object-not-json", "strict-schema-mismatch"],
    )
    def test_best_effort_passes_a_reply_strict_would_refuse(self, request_overrides: dict[str, Any], reply: ChatSubmittedMessage) -> None:
        chat_extras = _stored_extras(**request_overrides)

        assert _verification_refusal(reply, chat_extras).rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert verify_chat_reply(reply, chat_extras, ChatConformance.BEST_EFFORT) == reply

    @pytest.mark.parametrize("conformance", list(ChatConformance))
    def test_a_call_to_a_function_the_request_did_not_offer_is_refused_under_every_mode(self, conformance: ChatConformance) -> None:
        refusal = _verification_refusal(_reply(tool_calls=[_call("send_email")]), _stored_extras(tools=[LOOKUP_TOOL]), conformance)

        assert refusal.rc == CHAT_SUBMIT_INVALID_RC
        assert "'send_email'" in refusal.specific

    @pytest.mark.parametrize("conformance", list(ChatConformance))
    def test_a_call_to_a_request_without_tools_is_refused_under_every_mode(self, conformance: ChatConformance) -> None:
        refusal = _verification_refusal(_reply(tool_calls=[_call()]), None, conformance)

        assert refusal.rc == CHAT_SUBMIT_INVALID_RC
        assert "offered no tools" in refusal.specific

    @pytest.mark.parametrize("conformance", list(ChatConformance))
    def test_two_calls_sharing_an_id_are_refused_under_every_mode(self, conformance: ChatConformance) -> None:
        refusal = _verification_refusal(_reply(tool_calls=[_call(), _call()]), _stored_extras(tools=[LOOKUP_TOOL]), conformance)

        assert refusal.rc == CHAT_SUBMIT_INVALID_RC
        assert "'id'" in refusal.specific

    def test_a_strict_single_call_request_keeps_only_the_first_call(self) -> None:
        first_call, second_call = _call(call_id="call_one"), _call("send_email", "call_two")
        chat_extras = _stored_extras(tools=[OTHER_TOOL, LOOKUP_TOOL], parallel_tool_calls=False)

        verified_reply = verify_chat_reply(_reply(tool_calls=[first_call, second_call]), chat_extras, ChatConformance.STRICT)

        assert build_stored_reply(verified_reply) == {"role": "assistant", "content": None, "tool_calls": [first_call]}

    def test_a_call_the_single_call_reduction_drops_is_not_checked(self) -> None:
        chat_extras = _stored_extras(tools=[LOOKUP_TOOL], parallel_tool_calls=False)

        verified_reply = verify_chat_reply(_reply(tool_calls=[_call(), _call("undeclared")]), chat_extras, ChatConformance.STRICT)

        assert verified_reply.tool_calls is not None
        assert [tool_call.function.name for tool_call in verified_reply.tool_calls] == ["lookup_weather"]

    @pytest.mark.parametrize(
        ("parallel_tool_calls", "conformance"),
        [(True, ChatConformance.STRICT), (False, ChatConformance.BEST_EFFORT)],
        ids=["parallel-strict", "single-best-effort"],
    )
    def test_several_calls_are_kept_when_the_reduction_does_not_apply(
        self, parallel_tool_calls: bool, conformance: ChatConformance
    ) -> None:
        reply = _reply(tool_calls=[_call(call_id="call_one"), _call(call_id="call_two")])
        chat_extras = _stored_extras(tools=[LOOKUP_TOOL], parallel_tool_calls=parallel_tool_calls)

        assert verify_chat_reply(reply, chat_extras, conformance) == reply

    @pytest.mark.parametrize(
        "content",
        ["Blue.", "", "[1, 2]", '"blue"', '{"name": NaN}'],
        ids=["text", "empty", "array", "string", "nan"],
    )
    def test_a_strict_json_object_format_refuses_content_that_is_not_a_json_object(self, content: str) -> None:
        refusal = _verification_refusal(_reply(content), _stored_extras(response_format={"type": "json_object"}))

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert "'json_object'" in refusal.specific

    def test_a_strict_json_object_format_accepts_a_json_object(self) -> None:
        reply = _reply('{"name": "blue"}')

        assert verify_chat_reply(reply, _stored_extras(response_format={"type": "json_object"}), ChatConformance.STRICT) == reply

    def test_tool_calls_without_content_are_exempt_from_the_format(self) -> None:
        reply = _reply(tool_calls=[_call()])
        chat_extras = _stored_extras(tools=[LOOKUP_TOOL], response_format=_schema_format(COLOUR_SCHEMA))

        assert verify_chat_reply(reply, chat_extras, ChatConformance.STRICT) == reply

    def test_a_strict_schema_accepts_a_matching_reply(self) -> None:
        reply = _reply('{"name": "blue"}')

        assert verify_chat_reply(reply, _stored_extras(response_format=_schema_format(COLOUR_SCHEMA)), ChatConformance.STRICT) == reply

    @pytest.mark.parametrize(
        ("content", "failed_check"),
        [
            ('{"name": 7}', "'type' fails at $.name"),
            ('{"name": "blue", "hue": 1}', "'additionalProperties' fails at $"),
            ("{}", "'required'"),
        ],
        ids=["wrong-type", "extra-property", "missing-property"],
    )
    def test_a_strict_schema_refuses_a_reply_that_does_not_match(self, content: str, failed_check: str) -> None:
        refusal = _verification_refusal(_reply(content), _stored_extras(response_format=_schema_format(COLOUR_SCHEMA)))

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert "does not match the request's JSON Schema 'colour'" in refusal.specific
        assert failed_check in refusal.specific

    @pytest.mark.parametrize(
        "schema",
        [{"type": "not-a-type"}, {"$ref": "#/$defs/missing"}, {"$ref": "https://example.com/colour.json"}],
        ids=["invalid-schema", "missing-local-reference", "remote-reference"],
    )
    def test_a_strict_schema_that_cannot_be_compiled_refuses_the_reply_with_its_own_message(self, schema: dict[str, Any]) -> None:
        refusal = _verification_refusal(_reply('{"name": "blue"}'), _stored_extras(response_format=_schema_format(schema)))

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC
        assert "cannot be compiled" in refusal.specific

    def test_a_strict_schema_is_read_by_the_draft_its_schema_key_declares(self) -> None:
        # An array under 'items' is a tuple schema in draft 7 and an invalid schema in draft 2020-12.
        tuple_schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "array", "items": [{"type": "string"}]}
        chat_extras = _stored_extras(response_format=_schema_format(tuple_schema))

        assert verify_chat_reply(_reply('["blue"]'), chat_extras, ChatConformance.STRICT) == _reply('["blue"]')
        assert "'type' fails" in _verification_refusal(_reply("[7]"), chat_extras).specific

    @pytest.mark.parametrize("strict", [False, None], ids=["strict-false", "strict-unset"])
    def test_a_non_strict_schema_needs_only_a_json_object(self, strict: bool | None) -> None:
        chat_extras = _stored_extras(response_format=_schema_format(COLOUR_SCHEMA, strict=strict))
        mismatching_object = _reply('{"name": 7}')

        assert verify_chat_reply(mismatching_object, chat_extras, ChatConformance.STRICT) == mismatching_object
        assert _verification_refusal(_reply("[7]"), chat_extras).rc == CHAT_SUBMIT_NONCONFORMANT_RC

    def test_content_nested_too_deeply_to_parse_is_refused_as_not_json(self) -> None:
        deeply_nested = "[" * 100_000 + "]" * 100_000

        refusal = _verification_refusal(_reply(deeply_nested), _stored_extras(response_format={"type": "json_object"}))

        assert refusal.rc == CHAT_SUBMIT_NONCONFORMANT_RC


class TestReplyFinishReason:
    """The finish reason a client receives follows OpenAI's shape for the request's ``tool_choice``."""

    def test_a_named_choice_answered_with_its_call_finishes_with_stop(self) -> None:
        chat_extras = _stored_extras(tools=[OTHER_TOOL, LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE)

        assert chat_reply_finish_reason("tool_calls", _reply(tool_calls=[_call()]), chat_extras) == "stop"

    @pytest.mark.parametrize("tool_choice", [None, "auto", "required"])
    def test_a_mode_choice_keeps_the_tool_calls_finish(self, tool_choice: str | None) -> None:
        request_overrides: dict[str, Any] = {"tools": [LOOKUP_TOOL]}
        if tool_choice is not None:
            request_overrides["tool_choice"] = tool_choice

        assert chat_reply_finish_reason("tool_calls", _reply(tool_calls=[_call()]), _stored_extras(**request_overrides)) == "tool_calls"

    @pytest.mark.parametrize("finish_reason", ["length", "content_filter", "stop"])
    def test_a_named_choice_keeps_every_other_finish_reason(self, finish_reason: str) -> None:
        chat_extras = _stored_extras(tools=[LOOKUP_TOOL], tool_choice=NAMED_LOOKUP_CHOICE)

        assert chat_reply_finish_reason(finish_reason, _reply(tool_calls=[_call()]), chat_extras) == finish_reason


class TestChatJobPayload:
    def test_every_sampling_field_is_filled_with_its_default_and_no_prompt_is_sent(self) -> None:
        payload = build_chat_job_payload({"max_length": 512}, max_length=512, max_context_length=4096)

        assert payload == {
            "temperature": 1.0,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "rep_pen": 1.0,
            "top_k": 0,
            "min_p": 0.0,
            "max_length": 512,
            "max_context_length": 4096,
            "n": 1,
        }

    def test_values_the_client_set_replace_the_defaults_and_stop_and_seed_appear_only_when_sent(self) -> None:
        params = build_text_params(
            parse_chat_request(_body(temperature=0.3, presence_penalty=0.4, stop="END", seed=9, max_tokens=64)),
        )

        payload = build_chat_job_payload(params, max_length=64, max_context_length=2048)

        assert payload["temperature"] == 0.3
        assert payload["presence_penalty"] == 0.4
        assert payload["frequency_penalty"] == 0.0
        assert payload["stop_sequence"] == ["END"]
        assert payload["seed"] == 9
        assert "prompt" not in payload


class TestMessages:
    def test_messages_serialize_in_the_shape_sent_without_unset_keys(self) -> None:
        messages = [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": [{"type": "text", "text": "Hello"}], "name": "alice"},
        ]

        serialized = serialize_messages(parse_chat_request(_body(messages=messages)))

        assert serialized == messages

    def test_a_developer_message_is_stored_as_a_system_message(self) -> None:
        messages = [
            {"role": "developer", "content": "Answer in French."},
            {"role": "user", "content": "Hello"},
        ]

        serialized = serialize_messages(parse_chat_request(_body(messages=messages)))

        assert serialized == [
            {"role": "system", "content": "Answer in French."},
            {"role": "user", "content": "Hello"},
        ]

    def test_flattening_joins_role_and_text_per_message(self) -> None:
        messages = [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]},
        ]

        assert flatten_messages(messages) == "system: Be brief.\nuser: one\ntwo"

    def test_rewriting_applies_to_every_text_and_keeps_the_shape(self) -> None:
        messages = [
            {"role": "system", "content": "keep"},
            {"role": "user", "content": [{"type": "text", "text": "drop"}, {"type": "text", "text": "keep"}]},
        ]

        rewritten = rewrite_message_texts(messages, lambda text: None if text == "drop" else text.upper())

        assert rewritten == [
            {"role": "system", "content": "KEEP"},
            {"role": "user", "content": [{"type": "text", "text": ""}, {"type": "text", "text": "KEEP"}]},
        ]
        assert messages[1]["content"][0]["text"] == "drop"

    def test_flattening_renders_an_image_as_the_placeholder_and_rewriting_leaves_it_alone(self) -> None:
        image_part = _image_part(SMALL_DATA_URL)
        messages = [{"role": "user", "content": [{"type": "text", "text": "what is this"}, image_part]}]

        rewritten = rewrite_message_texts(messages, str.upper)

        assert flatten_messages(messages) == f"user: what is this\n{CHAT_IMAGE_PLACEHOLDER}"
        assert rewritten == [{"role": "user", "content": [{"type": "text", "text": "WHAT IS THIS"}, image_part]}]


SMALL_DATA_URL = "data:image/png;base64,iVBORw0KGgo="


def _image_part(url: str, **image_url_fields: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url, **image_url_fields}}


def _image_messages(*image_urls: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": "Describe these."}, *(_image_part(url) for url in image_urls)]}]


class TestImageReferences:
    def test_each_image_url_is_replaced_by_a_reference_keyed_by_request_and_ordinal(self) -> None:
        messages = [
            {"role": "user", "content": [_image_part(SMALL_DATA_URL, detail="high")]},
            {"role": "assistant", "content": "A cat."},
            {"role": "user", "content": [{"type": "text", "text": "And this?"}, _image_part("https://example.com/b.png")]},
        ]

        stored_messages = replace_image_urls(messages, "request-id")

        assert list_image_urls(stored_messages) == [
            f"{CHAT_IMAGE_REFERENCE_PREFIX}{build_chat_image_key('request-id', 0)}",
            f"{CHAT_IMAGE_REFERENCE_PREFIX}{build_chat_image_key('request-id', 1)}",
        ]
        assert build_chat_image_key("request-id", 1) == "request-id_chat_1.jpg"
        assert stored_messages[0]["content"][0]["image_url"]["detail"] == "high"
        assert stored_messages[1:2] == messages[1:2]
        assert messages[0]["content"][0]["image_url"]["url"] == SMALL_DATA_URL

    def test_resolving_references_replaces_each_with_the_resolved_url(self) -> None:
        stored_messages = replace_image_urls(_image_messages(SMALL_DATA_URL, SMALL_DATA_URL), "request-id")

        resolved_messages = resolve_image_references(stored_messages, lambda image_key: f"https://signed/{image_key}")

        assert list_image_urls(resolved_messages) == [
            "https://signed/request-id_chat_0.jpg",
            "https://signed/request-id_chat_1.jpg",
        ]
        assert flatten_messages(resolved_messages) == flatten_messages(stored_messages)

    def test_stored_keys_are_listed_from_the_references_in_message_order(self) -> None:
        stored_messages = replace_image_urls(_image_messages(SMALL_DATA_URL, SMALL_DATA_URL), "request-id")

        assert list_chat_image_keys(stored_messages) == [build_chat_image_key("request-id", 0), build_chat_image_key("request-id", 1)]
        assert list_chat_image_keys(_image_messages(SMALL_DATA_URL)) == []

    def test_image_parts_are_detected_only_in_part_arrays(self) -> None:
        assert has_image_parts(_image_messages(SMALL_DATA_URL)) is True
        assert has_image_parts([{"role": "user", "content": "an image_url in text"}]) is False
        assert has_image_parts([{"role": "user", "content": [{"type": "text", "text": "image_url"}]}]) is False

    def test_resolving_refuses_an_image_url_that_is_not_a_stored_reference(self) -> None:
        with pytest.raises(ValueError):
            resolve_image_references(_image_messages(SMALL_DATA_URL), lambda image_key: image_key)


class TestMessagesOffload:
    def test_messages_at_or_under_the_threshold_stay_inline(self) -> None:
        messages = [{"role": "user", "content": "Hello."}]
        compact_size = len(json.dumps(messages, separators=(",", ":")).encode())

        assert build_chat_messages_offload(messages, threshold_bytes=compact_size, compress=True) is None

    def test_compressible_messages_over_the_threshold_are_stored_gzipped(self) -> None:
        messages = [{"role": "user", "content": "the same words again " * 2000}]

        offload = build_chat_messages_offload(messages, threshold_bytes=1024, compress=True)

        assert offload is not None
        assert offload.gzipped is True
        assert json.loads(gzip.decompress(offload.body)) == messages
        assert len(offload.body) < len(json.dumps(messages, separators=(",", ":")).encode())

    def test_messages_that_gzip_does_not_shrink_are_stored_plain(self) -> None:
        # gzip's header and trailer alone outweigh what deflate saves on a body this short.
        messages = [{"role": "user", "content": "x"}]

        offload = build_chat_messages_offload(messages, threshold_bytes=1, compress=True)

        assert offload is not None
        assert offload.gzipped is False
        assert json.loads(offload.body) == messages

    def test_non_ascii_text_is_stored_as_utf8(self) -> None:
        messages = [{"role": "user", "content": "été 中文 " * 500}]

        offload = build_chat_messages_offload(messages, threshold_bytes=1, compress=True)

        assert offload is not None
        assert json.loads(gzip.decompress(offload.body).decode("utf-8")) == messages

    def test_messages_are_stored_as_plain_json_while_compression_is_off(self) -> None:
        messages = [{"role": "user", "content": "the same words again " * 2000}]

        offload = build_chat_messages_offload(messages, threshold_bytes=1024, compress=False)

        assert offload is not None
        assert offload.gzipped is False
        assert json.loads(offload.body) == messages

    def test_images_are_inlined_as_jpeg_data_urls_in_order(self) -> None:
        messages = _image_messages("https://example.com/a.png", SMALL_DATA_URL)

        inlined_messages = inline_chat_images(messages, [b"first", b"second"])

        assert list_image_urls(inlined_messages) == [
            f"data:image/jpeg;base64,{base64.b64encode(b'first').decode('ascii')}",
            f"data:image/jpeg;base64,{base64.b64encode(b'second').decode('ascii')}",
        ]
        assert inlined_messages[0]["content"][0] == messages[0]["content"][0]

    def test_inlining_refuses_a_count_of_images_other_than_the_image_parts(self) -> None:
        with pytest.raises(ValueError):
            inline_chat_images(_image_messages(SMALL_DATA_URL), [])

    def test_the_messages_key_sits_beside_the_image_keys(self) -> None:
        assert build_chat_messages_key("request-id") == "request-id_chat_messages.json"

    def test_only_the_messages_key_is_read_as_a_messages_key(self) -> None:
        """The store routes a key to the messages bucket by its suffix, so no image key may carry it."""
        assert is_chat_messages_key(build_chat_messages_key("request-id"))
        assert not is_chat_messages_key(build_chat_image_key("request-id", 0))
        assert not is_chat_messages_key(build_chat_image_key("request-id_chat_messages", 12))

    def test_stored_object_keys_list_the_images_then_the_offloaded_messages(self) -> None:
        stored_messages = replace_image_urls(_image_messages(SMALL_DATA_URL), "request-id")

        assert list_stored_chat_object_keys("request-id", stored_messages, messages_offloaded=True) == [
            build_chat_image_key("request-id", 0),
            build_chat_messages_key("request-id"),
        ]
        assert list_stored_chat_object_keys("request-id", stored_messages, messages_offloaded=False) == [
            build_chat_image_key("request-id", 0),
        ]
        assert list_stored_chat_object_keys("request-id", None, messages_offloaded=False) == []


def _png_data_url(size: tuple[int, int], mode: str = "RGB") -> str:
    image_buffer = BytesIO()
    Image.new(mode, size).save(image_buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(image_buffer.getvalue()).decode('ascii')}"


class TestImageValidation:
    def test_more_images_than_the_cap_are_refused(self) -> None:
        rejection = _rejection(_body(messages=_image_messages(*[SMALL_DATA_URL] * (MAX_CHAT_IMAGE_COUNT + 1))))

        assert rejection.rc == "TooManyExtraSourceImages"

    def test_the_image_cap_itself_is_accepted(self) -> None:
        chat_request = parse_chat_request(_body(messages=_image_messages(*[SMALL_DATA_URL] * MAX_CHAT_IMAGE_COUNT)))

        assert len(list_image_urls(serialize_messages(chat_request))) == MAX_CHAT_IMAGE_COUNT

    def test_a_data_url_decoding_to_more_than_the_byte_cap_is_refused(self) -> None:
        oversized_payload = "A" * ((MAX_CHAT_IMAGE_BYTES + 3) * 4 // 3)

        with pytest.raises(e.ImageValidationFailed) as raised:
            parse_chat_request(_body(messages=_image_messages(f"data:image/png;base64,{oversized_payload}")))

        assert raised.value.rc == "SourceImageSizeExceeded"

    @pytest.mark.parametrize(
        ("image_url", "return_code"),
        [
            ("ftp://example.com/a.png", "SourceImageUrlInvalid"),
            ("data:text/plain;base64,aGk=", "SourceImageUnreadable"),
        ],
    )
    def test_an_image_url_of_another_form_is_refused(self, image_url: str, return_code: str) -> None:
        with pytest.raises(e.ImageValidationFailed) as raised:
            parse_chat_request(_body(messages=_image_messages(image_url)))

        assert raised.value.rc == return_code


class TestImageTransport:
    @pytest.mark.parametrize(
        ("declared", "expected"),
        [
            (["url", "base64"], ChatImageTransport.URL),
            (["base64", "url"], ChatImageTransport.URL),
            (["url"], ChatImageTransport.URL),
            (["base64"], ChatImageTransport.BASE64),
            (["messages_url"], None),
            ([], None),
            (None, None),
        ],
    )
    def test_url_is_chosen_whenever_declared_and_base64_only_alone(self, declared: list[str] | None, expected: Any) -> None:
        assert select_chat_transport(declared, carries_images=True, messages_offloaded=False) == expected

    @pytest.mark.parametrize(
        ("declared", "carries_images", "expected"),
        [
            (["url", "messages_url"], True, ChatImageTransport.MESSAGES_URL),
            (["messages_url", "base64"], True, ChatImageTransport.MESSAGES_URL),
            (["messages_url"], False, ChatImageTransport.MESSAGES_URL),
            (["url", "base64"], True, ChatImageTransport.URL),
            (["url"], False, None),
        ],
    )
    def test_an_offloaded_request_uses_messages_url_whenever_the_worker_declared_it(
        self,
        declared: list[str],
        carries_images: bool,
        expected: Any,
    ) -> None:
        assert select_chat_transport(declared, carries_images=carries_images, messages_offloaded=True) == expected

    def test_a_request_sent_inline_without_images_has_no_transport(self) -> None:
        assert select_chat_transport(["url", "base64", "messages_url"], carries_images=False, messages_offloaded=False) is None

    def test_vision_is_a_declared_feature_name(self) -> None:
        assert ChatFeature("vision") is ChatFeature.VISION


class TestImageLoading:
    def test_a_large_image_is_scaled_to_the_longest_side_cap(self) -> None:
        chat_image = load_chat_image(_png_data_url((3000, 1500)))

        assert chat_image.size == (MAX_CHAT_IMAGE_SIDE, MAX_CHAT_IMAGE_SIDE // 2)
        assert chat_image.mode == "RGB"

    def test_a_small_image_keeps_its_size_and_transparency(self) -> None:
        chat_image = load_chat_image(_png_data_url((64, 32), mode="RGBA"))

        assert chat_image.size == (64, 32)
        assert chat_image.mode == "RGBA"

    def test_an_image_over_the_resolution_cap_is_refused(self) -> None:
        with pytest.raises(e.ImageValidationFailed) as raised:
            load_chat_image(_png_data_url((3100, 3100)))

        assert raised.value.rc == "SourceImageResolutionExceeded"

    def test_base64_that_is_not_an_image_is_refused(self) -> None:
        not_an_image = base64.b64encode(b"plain text, not an image").decode("ascii")

        with pytest.raises(e.ImageValidationFailed) as raised:
            load_chat_image(f"data:image/png;base64,{not_an_image}")

        assert raised.value.rc == "SourceImageUnreadable"


@pytest.mark.parametrize("field", ["temperature", "top_p", "frequency_penalty", "presence_penalty"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_native_sampling_values_must_be_finite(field: str, value: float) -> None:
    assert _rejection(_body(**{field: value})).code == 400
