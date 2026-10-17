# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validation and mapping of an OpenAI chat-completions body onto a text request, and the chat job payload."""

from __future__ import annotations

import base64
import uuid
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from horde import exceptions as e
from horde.chat_contract import (
    CHAT_REQUEST_CONFLICT_ERROR_TYPE,
    MAX_CHAT_MESSAGE_COUNT,
    MAX_CHAT_TEXT_BYTES,
    ChatHordeOptions,
)
from horde.classes.kobold.chat_request import (
    CHAT_IMAGE_REFERENCE_PREFIX,
    CHAT_REQUEST_CONFLICT_RC,
    DEFAULT_CHAT_MAX_TOKENS,
    UNSUPPORTED_CHAT_PARAMETER_RC,
    ChatFeature,
    build_chat_image_key,
    build_chat_job_payload,
    build_model_names,
    build_text_params,
    build_worker_targeting,
    flatten_messages,
    list_image_urls,
    parse_chat_request,
    replace_image_urls,
    required_chat_features,
    resolve_image_references,
    rewrite_message_texts,
    serialize_messages,
)
from horde.exceptions import KNOWN_RC
from horde.image import MAX_CHAT_IMAGE_SIDE, load_chat_image

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
            "tools",
            "tool_choice",
            "response_format",
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

    @pytest.mark.parametrize("role", ["system", "user", "assistant"])
    def test_an_image_part_is_refused_with_its_location_in_any_message(self, role: str) -> None:
        image_part = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
        messages = [{"role": role, "content": [{"type": "text", "text": "Look."}, image_part]}]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "messages[0].content[1].type=image_url" in rejection.specific

    @pytest.mark.parametrize("unsupported_role", ["tool", "function"])
    def test_a_role_outside_the_served_roles_is_refused(self, unsupported_role: str) -> None:
        rejection = _rejection(_body(messages=[{"role": unsupported_role, "content": "x"}]))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert f"messages[0].role={unsupported_role}" in rejection.specific

    def test_a_message_key_for_tool_calls_is_refused(self) -> None:
        messages = [{"role": "assistant", "content": "", "tool_calls": []}]

        rejection = _rejection(_body(messages=messages))

        assert rejection.rc == UNSUPPORTED_CHAT_PARAMETER_RC
        assert "messages[0].tool_calls" in rejection.specific

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


class TestRequiredFeatures:
    def test_a_request_at_default_values_needs_no_feature(self) -> None:
        params = build_text_params(parse_chat_request(_body(frequency_penalty=0, presence_penalty=0.0)))

        assert required_chat_features(params) == set()

    def test_each_non_default_optional_value_needs_its_feature(self) -> None:
        params = build_text_params(parse_chat_request(_body(seed=7, frequency_penalty=0.2, presence_penalty=0.3)))

        assert required_chat_features(params) == {
            ChatFeature.SEED,
            ChatFeature.FREQUENCY_PENALTY,
            ChatFeature.PRESENCE_PENALTY,
        }

    def test_a_zero_seed_still_needs_the_seed_feature(self) -> None:
        params = build_text_params(parse_chat_request(_body(seed=0)))

        assert required_chat_features(params) == {ChatFeature.SEED}


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

    def test_flattening_and_rewriting_leave_image_parts_out_of_the_text(self) -> None:
        image_part = _image_part(SMALL_DATA_URL)
        messages = [{"role": "user", "content": [{"type": "text", "text": "what is this"}, image_part]}]

        rewritten = rewrite_message_texts(messages, str.upper)

        assert flatten_messages(messages) == "user: what is this"
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

    def test_resolving_refuses_an_image_url_that_is_not_a_stored_reference(self) -> None:
        with pytest.raises(ValueError):
            resolve_image_references(_image_messages(SMALL_DATA_URL), lambda image_key: image_key)


def _png_data_url(size: tuple[int, int], mode: str = "RGB") -> str:
    image_buffer = BytesIO()
    Image.new(mode, size).save(image_buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(image_buffer.getvalue()).decode('ascii')}"


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
