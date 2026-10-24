# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The stress suite's chat mix: response checks, the scenario catalogue, the simulated worker's script and user, the
settings and coverage gate, the opt-in class wiring, the front client's fail-fast and stream-abandon shares, and the
smoke checker's contract and environment buckets."""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import random
import select
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_SCHEMA_NESTING, chat_schema_exceeds_nesting
from pydantic import TypeAdapter

os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from locust.argument_parser import get_empty_argument_parser
from locust.env import Environment
from locust.exception import RescheduleTask, StopUser
from locust.runners import MasterRunner, WorkerRunner

from horde.chat_contract import ChatResponseFormat, check_strict_response_schema
from tests.stress import check_smoke_results
from tests.stress.locustsuite import chat_mix_settings as chat_mix_settings_module
from tests.stress.locustsuite import chat_worker_user, helpers
from tests.stress.locustsuite.chat_checks import (
    CONTRACT_VIOLATION_PREFIX,
    ENVIRONMENT_CONDITION_PREFIX,
    BenignRefusal,
    CompletionExpectation,
    ContentRule,
    EnvironmentCondition,
    ErrorExpectation,
    StreamTranscript,
    find_abandoned_stream_violations,
    find_completion_violations,
    find_stream_violations,
    judge_front_response,
    queue_unavailable_reason,
)
from tests.stress.locustsuite.chat_front_client import ChatMixFrontClient, build_request_body
from tests.stress.locustsuite.chat_mix_settings import (
    SLOT_CLAIM_MESSAGE,
    SLOT_GRANT_MESSAGE,
    ChatMixConfigurationError,
    ChatMixCoverage,
    ChatMixSettings,
    IntervalGate,
    RunnerRole,
    RunTopology,
    WorkerSlotClaims,
    _add_chat_mix_arguments,
    build_run_topology,
    chat_mix_settings,
    coverage_for_gate,
    find_coverage_gaps,
)
from tests.stress.locustsuite.chat_scenarios import (
    FAIL_FAST_SHARE_GROUPS,
    RATING_SCHEMA,
    SCENARIO_BUILDERS,
    UPFRONT_KUDOS_FREE_TOKENS,
    ChatModelNames,
    HourlyBudget,
    ScenarioContext,
    ScenarioGroup,
    as_anonymous,
    as_fail_fast,
    build_authorization_headers,
    build_nested_object_schema,
    build_oversized_body,
    parse_group_weights,
    plain_prompt,
)
from tests.stress.locustsuite.chat_worker_script import (
    VISION_OK_PREFIX,
    VISION_UNEXPECTED_PREFIX,
    ChatPersonaRole,
    ReplyScript,
    WorkerReply,
    build_misbehaving_reply,
    build_persona,
    build_schema_sample,
    build_scripted_reply,
    find_job_payload_violations,
    nonce_marker,
    reply_marker,
)
from tests.stress.locustsuite.chat_worker_user import ChatMixWorker, refusal_was_intended, reply_posts_progress
from tests.stress.locustsuite.events import (
    _leave_out_user_classes,
    _remove_tag_filtered_user_classes,
    _remove_unrequested_opt_in_classes,
)
from tests.stress.locustsuite.helpers import _REFUSED_WORKER_NAME_PATTERN, _random_worker_name

MODEL: str = "stress/chat-conformant"
"""The model every test completion names."""

CONTEXT: ScenarioContext = ScenarioContext(
    models=ChatModelNames(conformant=MODEL, limited="stress/chat-limited", flaky="stress/chat-flaky"),
    worker_max_length=512,
    nonce="nonce123",
)
"""The context every catalogue test builds scenarios with."""

TEXT_EXPECTATION: CompletionExpectation = CompletionExpectation(model=MODEL, finish_reasons=frozenset({"stop"}))
"""A plain text reply."""

TOOL_EXPECTATION: CompletionExpectation = CompletionExpectation(
    model=MODEL,
    finish_reasons=frozenset({"tool_calls"}),
    content_rule=ContentRule.NULL,
    tool_call_names=frozenset({"get_weather"}),
    min_tool_calls=1,
    max_tool_calls=1,
)
"""A reply with one call to ``get_weather``."""

WEATHER_CALL: dict[str, Any] = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
"""One call as the front delivers it."""


def completion_body(
    *, content: str | None = "Hello.", tool_calls: list[dict[str, Any]] | None = None, finish_reason: str = "stop"
) -> dict[str, Any]:
    """Return a non-streamed completion as the front writes it."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-req-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        "system_fingerprint": "fp_0123456789",
        "x_horde": {"request_id": "req-1", "kudos": 2.0, "estimated_usage": False, "charge_basis": "output_estimate"},
    }


def stream_lines(*, delta_chunks: list[dict[str, Any]], finish_reason: str = "stop", include_usage: bool = True) -> list[str]:
    """Return the lines of a stream as the front sends it: a keepalive, the role chunk, ``delta_chunks``, the finish chunk,
    the usage chunk when asked for, and ``[DONE]``."""

    def chunk(delta: dict[str, Any], **extra: Any) -> dict[str, Any]:
        shared = {
            "id": "chatcmpl-req-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": MODEL,
            "system_fingerprint": "fp_0123456789",
        }
        if include_usage:
            shared["usage"] = None
        return {**shared, "choices": [{"index": 0, "delta": delta, "finish_reason": None}], **extra}

    finish_chunk = chunk({}, x_horde={"request_id": "req-1", "kudos": 2.0, "estimated_usage": False})
    finish_chunk["choices"][0]["finish_reason"] = finish_reason
    chunks = [chunk({"role": "assistant"}), *(chunk(delta) for delta in delta_chunks), finish_chunk]
    if include_usage:
        usage_chunk = chunk({})
        usage_chunk["choices"] = []
        usage_chunk["usage"] = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        chunks.append(usage_chunk)
    return [": horde queue_position=1 wait_time=2", "", *(f"data: {json.dumps(each_chunk)}" for each_chunk in chunks), "data: [DONE]"]


def read_transcript(lines: list[str]) -> StreamTranscript:
    """Return the transcript of ``lines``, received one second apart."""
    transcript = StreamTranscript()
    for line_index, line in enumerate(lines):
        transcript.feed_line(line, float(line_index))
    return transcript


class TestCompletionChecks:
    def test_a_well_formed_completion_has_no_violation(self) -> None:
        assert find_completion_violations(completion_body(), TEXT_EXPECTATION) == []

    def test_a_tool_call_completion_has_no_violation(self) -> None:
        body = completion_body(content=None, tool_calls=[WEATHER_CALL], finish_reason="tool_calls")
        assert find_completion_violations(body, TOOL_EXPECTATION) == []

    def test_usage_that_does_not_add_up_is_a_violation(self) -> None:
        body = completion_body()
        body["usage"]["total_tokens"] = 9
        assert any("total_tokens" in violation for violation in find_completion_violations(body, TEXT_EXPECTATION))

    def test_a_call_to_another_function_and_a_wrong_finish_reason_are_violations(self) -> None:
        other_call = {**WEATHER_CALL, "function": {"name": "get_time", "arguments": "{}"}}
        violations = find_completion_violations(
            completion_body(content=None, tool_calls=[other_call], finish_reason="stop"), TOOL_EXPECTATION
        )
        assert any("get_time" in violation for violation in violations)
        assert any("finish_reason" in violation for violation in violations)

    def test_content_breaking_the_requested_schema_is_a_violation(self) -> None:
        expectation = CompletionExpectation(
            model=MODEL, finish_reasons=frozenset({"stop"}), content_rule=ContentRule.JSON_SCHEMA, json_schema=RATING_SCHEMA
        )
        assert find_completion_violations(completion_body(content='{"name": "x", "score": 7, "tags": []}'), expectation) == []
        violations = find_completion_violations(completion_body(content='{"x": 0}'), expectation)
        assert violations and all("schema" in violation for violation in violations)

    def test_a_request_id_that_does_not_match_the_completion_id_is_a_violation(self) -> None:
        body = completion_body()
        body["x_horde"]["request_id"] = "other"
        assert any("x_horde.request_id" in violation for violation in find_completion_violations(body, TEXT_EXPECTATION))


class TestStreamChecks:
    def test_a_stream_in_the_documented_order_has_no_violation(self) -> None:
        transcript = read_transcript(stream_lines(delta_chunks=[{"content": "Hello."}]))
        assert find_stream_violations(transcript, TEXT_EXPECTATION, include_usage=True) == []
        assert transcript.keepalive_comments == 1
        assert transcript.first_keepalive_at == 0.0
        assert transcript.first_chunk_at == 2.0
        assert transcript.first_content_at == 3.0

    def test_a_tool_call_stream_without_usage_has_no_violation(self) -> None:
        tool_delta = {"tool_calls": [{"index": 0, **WEATHER_CALL}]}
        transcript = read_transcript(stream_lines(delta_chunks=[tool_delta], finish_reason="tool_calls", include_usage=False))
        assert find_stream_violations(transcript, TOOL_EXPECTATION, include_usage=False) == []

    def test_a_stream_without_done_or_usage_breaks_the_order(self) -> None:
        lines = stream_lines(delta_chunks=[{"content": "Hello."}], include_usage=False)[:-1]
        violations = find_stream_violations(read_transcript(lines), TEXT_EXPECTATION, include_usage=True)
        assert any("[DONE]" in violation for violation in violations)
        assert any("usage chunks" in violation for violation in violations)

    def test_content_and_calls_in_one_chunk_is_a_violation(self) -> None:
        mixed_delta = {"content": "Checking.", "tool_calls": [{"index": 0, **WEATHER_CALL}]}
        expectation = CompletionExpectation(
            model=MODEL,
            finish_reasons=frozenset({"tool_calls"}),
            tool_call_names=frozenset({"get_weather"}),
            min_tool_calls=1,
            max_tool_calls=1,
        )
        transcript = read_transcript(stream_lines(delta_chunks=[mixed_delta], finish_reason="tool_calls"))
        assert any(
            "both content and tool calls" in violation for violation in find_stream_violations(transcript, expectation, include_usage=True)
        )

    def test_a_first_chunk_with_content_is_a_violation(self) -> None:
        lines = stream_lines(delta_chunks=[])
        first_chunk = json.loads(lines[2].removeprefix("data: "))
        first_chunk["choices"][0]["delta"]["content"] = "Hello."
        lines[2] = f"data: {json.dumps(first_chunk)}"
        violations = find_stream_violations(read_transcript(lines), TEXT_EXPECTATION, include_usage=True)
        assert any("first chunk" in violation for violation in violations)

    def test_an_error_event_and_foreign_comments_are_kept(self) -> None:
        transcript = read_transcript([": something else", 'data: {"error": {"code": "x"}}', "data: [DONE]", "data: late"])
        assert transcript.error_envelope == {"error": {"code": "x"}}
        assert transcript.foreign_comments == ["something else"]
        assert transcript.data_after_done == 1


class TestJudgeFrontResponse:
    faulted = ErrorExpectation(
        status=502, codes=frozenset({"ChatRequestFaulted"}), after_headers=True, rc="ChatSubmitNonconformant", should_not_retry=True
    )

    def judge(self, expectation: Any, **response: Any) -> Any:
        arguments: dict[str, Any] = {
            "status_code": 200,
            "headers": {},
            "body": None,
            "transcript": None,
            "streamed": False,
            "include_usage": False,
            "cors_expected": False,
            **response,
        }
        return judge_front_response(expectation, None, **arguments)

    def test_a_rate_limit_and_a_full_front_are_benign(self) -> None:
        assert self.judge(TEXT_EXPECTATION, status_code=429, body=None).benign_refusal == BenignRefusal.RATE_LIMITED
        full_front = {"error": {"message": "full", "type": "x", "param": None, "code": "FrontAtCapacity"}}
        assert self.judge(TEXT_EXPECTATION, status_code=503, body=full_front).benign_refusal == BenignRefusal.FRONT_AT_CAPACITY

    def test_an_unexpected_refusal_is_a_violation_whatever_its_status(self) -> None:
        refusal = {"error": {"message": "no", "type": "x", "param": "temperature", "code": "UnsupportedChatParameter"}}
        verdict = self.judge(TEXT_EXPECTATION, status_code=400, body=refusal)
        assert verdict.violations and "expected a completion" in verdict.violations[0]

    def test_an_expected_fault_in_a_stream_matches(self) -> None:
        envelope = {
            "error": {
                "message": "faulted",
                "type": "api_error",
                "param": None,
                "code": "ChatRequestFaulted",
                "metadata": {"request_id": "r", "rc": "ChatSubmitNonconformant"},
            }
        }
        transcript = read_transcript([f"data: {json.dumps(envelope)}", "data: [DONE]"])
        verdict = self.judge(self.faulted, streamed=True, transcript=transcript, headers={"x-should-retry": "false"})
        assert verdict.violations == []
        assert verdict.matched_error is self.faulted

    def test_an_alternative_refusal_is_accepted_beside_a_completion_expectation(self) -> None:
        envelope = {
            "error": {
                "message": "faulted",
                "type": "api_error",
                "param": None,
                "code": "ChatRequestFaulted",
                "metadata": {"rc": "ChatSubmitNonconformant"},
            }
        }
        verdict = judge_front_response(
            TEXT_EXPECTATION,
            self.faulted,
            status_code=502,
            headers={"x-should-retry": "false"},
            body=envelope,
            transcript=None,
            streamed=False,
            include_usage=False,
            cors_expected=False,
        )
        assert verdict.violations == []

    def test_a_missing_cors_header_and_retry_header_are_violations(self) -> None:
        verdict = self.judge(
            self.faulted,
            status_code=502,
            body={
                "error": {
                    "message": "m",
                    "type": "t",
                    "param": None,
                    "code": "ChatRequestFaulted",
                    "metadata": {"rc": "ChatSubmitNonconformant"},
                }
            },
            cors_expected=True,
        )
        assert any("access-control-allow-origin" in violation for violation in verdict.violations)
        assert any("x-should-retry" in violation for violation in verdict.violations)


class TestScenarioCatalogue:
    def test_every_scenario_builds_with_a_unique_name_in_its_group(self) -> None:
        names = []
        for group, builders in SCENARIO_BUILDERS.items():
            for builder in builders:
                scenario = builder(CONTEXT)
                assert scenario.group == group
                assert scenario.name == builder.__name__
                names.append(scenario.name)
        assert len(names) == len(set(names))

    def test_a_scenario_needing_a_persona_targets_that_personas_model(self) -> None:
        models_by_persona = {
            ChatPersonaRole.CONFORMANT: CONTEXT.models.conformant,
            ChatPersonaRole.LIMITED: CONTEXT.models.limited,
            ChatPersonaRole.FLAKY: CONTEXT.models.flaky,
        }
        for builders in SCENARIO_BUILDERS.values():
            for builder in builders:
                scenario = builder(CONTEXT)
                if scenario.persona is not None:
                    assert scenario.body["model"] == models_by_persona[scenario.persona], scenario.name

    def test_only_the_scenarios_whose_faults_abort_jobs_are_budgeted(self) -> None:
        aborting_scripts = (ReplyScript.UNDECLARED, ReplyScript.DUP_IDS, ReplyScript.FAULT_GENERATION_ERROR)
        for builders in SCENARIO_BUILDERS.values():
            for builder in builders:
                scenario = builder(CONTEXT)
                body_text = json.dumps(scenario.body)
                assert scenario.aborts_worker_jobs == any(reply_marker(script) in body_text for script in aborting_scripts), scenario.name

    def test_the_nesting_schemas_sit_on_either_side_of_the_limit_and_are_strict(self) -> None:
        at_limit = build_nested_object_schema(MAX_CHAT_SCHEMA_NESTING)
        assert not chat_schema_exceeds_nesting(at_limit)
        assert chat_schema_exceeds_nesting(build_nested_object_schema(MAX_CHAT_SCHEMA_NESTING + 1))
        response_format = TypeAdapter(ChatResponseFormat).validate_python(
            {"type": "json_schema", "json_schema": {"name": "deep", "schema": at_limit, "strict": True}}
        )
        check_strict_response_schema(response_format)
        jsonschema.validate(build_schema_sample(at_limit, text="leaf"), at_limit)

    def test_group_weights_parse_and_refuse_unknown_groups(self) -> None:
        weights = parse_group_weights("ordinary=6, tools=0")
        assert weights[ScenarioGroup.ORDINARY] == 6
        assert weights[ScenarioGroup.VISION] == 0
        with pytest.raises(ValueError):
            parse_group_weights("nonsense=1")

    def test_an_hourly_budget_refuses_past_its_limit_until_an_hour_passes(self) -> None:
        budget = HourlyBudget(2)
        assert budget.try_take(0.0) and budget.try_take(10.0)
        assert not budget.try_take(20.0)
        assert budget.try_take(3600.0)


class TestWorkerScript:
    conformant = build_persona(ChatPersonaRole.CONFORMANT, vision=True)
    limited = build_persona(ChatPersonaRole.LIMITED, vision=False)

    def payload(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "messages": [{"role": "user", "content": "Weather?"}],
            "temperature": 1.0,
            "top_p": 1.0,
            "frequency_penalty": 0,
            "presence_penalty": 0,
            "rep_pen": 1.0,
            "top_k": 0,
            "min_p": 0,
            "max_length": 64,
            "max_context_length": 1024,
            "n": 1,
        }
        return {**base, **overrides}

    def test_every_script_produces_its_reply(self) -> None:
        tools = [{"type": "function", "function": {"name": "get_weather"}}]
        for script in ReplyScript:
            reply = build_scripted_reply(self.payload(messages=[{"role": "user", "content": f"Hi {reply_marker(script)}"}], tools=tools))
            if script == ReplyScript.FAULT_GENERATION_ERROR:
                assert reply.fault_reason == "generation_error"
            elif script == ReplyScript.DUP_IDS:
                assert reply.breaks_every_contract and reply.tool_calls[0]["id"] == reply.tool_calls[1]["id"]
            elif script == ReplyScript.UNDECLARED:
                assert reply.breaks_every_contract and reply.tool_calls[0]["function"]["name"] == "not_offered"
            elif script in (ReplyScript.PROSE, ReplyScript.NONCONFORMANT):
                assert reply.breaks_strict_contract and reply.tool_calls is None

    def test_a_well_behaved_reply_fits_the_request(self) -> None:
        tool_reply = build_scripted_reply(
            self.payload(tools=[{"type": "function", "function": {"name": "get_time"}}], tool_choice="required")
        )
        assert tool_reply.finish_reason == "tool_calls" and tool_reply.tool_calls[0]["function"]["name"] == "get_time"
        schema_format = {"type": "json_schema", "json_schema": {"name": "rating", "schema": dict(RATING_SCHEMA), "strict": True}}
        jsonschema.validate(json.loads(build_scripted_reply(self.payload(response_format=schema_format)).content), RATING_SCHEMA)
        follow_up = self.payload(
            messages=[{"role": "user", "content": "Weather?"}, {"role": "tool", "tool_call_id": "c", "content": "18C"}],
            tools=[{"type": "function", "function": {"name": "get_time"}}],
        )
        assert build_scripted_reply(follow_up).tool_calls is None

    def test_a_misbehaving_reply_breaks_what_the_request_asked_for(self) -> None:
        required = build_misbehaving_reply(
            self.payload(tools=[{"type": "function", "function": {"name": "get_time"}}], tool_choice="required")
        )
        assert required.tool_calls is None and required.breaks_strict_contract
        json_reply = build_misbehaving_reply(self.payload(response_format={"type": "json_object"}))
        assert json_reply.breaks_strict_contract
        with pytest.raises(ValueError):
            json.loads(json_reply.content)

    def test_images_are_described_by_how_they_arrived(self) -> None:
        jpeg_url = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
        assert build_scripted_reply(
            self.payload(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": jpeg_url}}]}])
        ).content.startswith(VISION_OK_PREFIX)
        png_url = "data:image/png;base64,iVBORw0KGgo="
        assert build_scripted_reply(
            self.payload(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": png_url}}]}])
        ).content.startswith(VISION_UNEXPECTED_PREFIX)

    def test_a_payload_honouring_the_contract_has_no_violation(self) -> None:
        assert (
            find_job_payload_violations(
                self.payload(tools=[{"type": "function", "function": {"name": "get_time"}}], tool_choice="required"), self.conformant
            )
            == []
        )

    def test_undispatched_and_undeclared_payloads_are_violations(self) -> None:
        forced = self.payload(
            tools=[{"type": "function", "function": {"name": "get_time"}}],
            tool_choice={"type": "function", "function": {"name": "get_time"}},
        )
        assert any("tool_choice" in violation for violation in find_job_payload_violations(forced, self.conformant))
        assert any(
            "tool_choice" in violation for violation in find_job_payload_violations(self.payload(tool_choice="none"), self.conformant)
        )
        assert any(
            "json_schema" in violation
            for violation in find_job_payload_violations(self.payload(response_format={"type": "json_schema"}), self.limited)
        )
        hot_tools = self.payload(tools=[{"type": "function", "function": {"name": "get_time"}}], temperature=0.9)
        assert any("tools_max_temperature" in violation for violation in find_job_payload_violations(hot_tools, self.limited))
        developer = self.payload(messages=[{"role": "developer", "content": "x"}], prompt="x", max_length=4096)
        violations = find_job_payload_violations({key: value for key, value in developer.items() if key != "top_k"}, self.conformant)
        expected_fragments = (
            "carries a prompt",
            "lacks sampling fields ['top_k']",
            "carries roles ['developer']",
            "max_length 4096 exceeds",
        )
        assert all(any(fragment in violation for violation in violations) for fragment in expected_fragments), violations
        assert len(violations) == len(expected_fragments)

    def test_the_personas_declare_what_their_scenarios_rely_on(self) -> None:
        assert "tools_with_response_format" in self.conformant.features
        assert "vision" in self.conformant.features and self.conformant.image_transports == ("base64",)
        assert self.limited.guarantees == ("prompt_overflow_refused",)
        assert self.limited.prompt_jobs and not self.conformant.prompt_jobs


class TestSmokeCheckerContractBucket:
    def write_csvs(self, tmp_path: Path, failures: list[tuple[str, str, str, int]]) -> Path:
        stats_path = tmp_path / "run_stats.csv"
        with stats_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Type", "Name", "Request Count", "Failure Count"])
            writer.writerow(["", "Aggregated", "10", str(len(failures))])
        with (tmp_path / "run_failures.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Method", "Name", "Error", "Occurrences"])
            writer.writerows(failures)
        return stats_path

    def test_a_contract_violation_gates_whatever_status_it_names(self, tmp_path: Path) -> None:
        contract_error = f"CatchResponseError('{CONTRACT_VIOLATION_PREFIX} [plain_prompt] status 400: expected a completion')"
        stats_path = self.write_csvs(tmp_path, [("POST", "/v1/chat/completions [plain_prompt] [stream]", contract_error, 1)])
        classified = check_smoke_results._classify_failures(tmp_path / "run_failures.csv")
        assert len(classified.contract) == 1 and not classified.client and not classified.crash
        with pytest.raises(SystemExit, match="1 contract violation"):
            check_smoke_results.main(["--stats", str(stats_path)])

    def test_an_environment_condition_gates_in_its_own_bucket(self, tmp_path: Path) -> None:
        environment_error = f"CatchResponseError('{ENVIRONMENT_CONDITION_PREFIX} [horde_unavailable] [plain_prompt] status 502: x')"
        stats_path = self.write_csvs(tmp_path, [("POST", "/v1/chat/completions [plain_prompt] [non-stream]", environment_error, 1)])
        classified = check_smoke_results._classify_failures(tmp_path / "run_failures.csv")
        assert len(classified.environment) == 1 and not classified.crash and not classified.contract
        with pytest.raises(SystemExit, match="1 environment condition"):
            check_smoke_results.main(["--stats", str(stats_path)])

    def test_a_plain_4xx_stays_benign(self, tmp_path: Path) -> None:
        stats_path = self.write_csvs(tmp_path, [("POST", "/api/v2/generate/async", "CatchResponseError('Status 403: KudosUpfront')", 2)])
        assert check_smoke_results.main(["--stats", str(stats_path)]) == 0


NONCE: str = "0a1b2c3d4e5f"
"""A request nonce."""


class TestNonceAndEnvironmentChecks:
    nonce_expectation = CompletionExpectation(model=MODEL, finish_reasons=frozenset({"stop"}), nonce=NONCE)

    def test_a_reply_without_the_request_nonce_answers_another_request(self) -> None:
        assert find_completion_violations(completion_body(content=f"Hello {NONCE}"), self.nonce_expectation) == []
        violations = find_completion_violations(completion_body(content="Hello"), self.nonce_expectation)
        assert any("another request" in violation for violation in violations)

    def test_a_call_without_the_request_nonce_answers_another_request(self) -> None:
        expectation = CompletionExpectation(
            model=MODEL,
            finish_reasons=frozenset({"tool_calls"}),
            content_rule=ContentRule.NULL,
            tool_call_names=frozenset({"get_weather"}),
            min_tool_calls=1,
            max_tool_calls=1,
            nonce=NONCE,
        )
        body = completion_body(content=None, tool_calls=[WEATHER_CALL], finish_reason="tool_calls")
        assert any("another request" in violation for violation in find_completion_violations(body, expectation))

    def test_the_vision_marker_is_required(self) -> None:
        expectation = CompletionExpectation(model=MODEL, finish_reasons=frozenset({"stop"}), nonce=NONCE, content_fragment=VISION_OK_PREFIX)
        assert find_completion_violations(completion_body(content=f"{VISION_OK_PREFIX} 1 jpeg {NONCE}"), expectation) == []
        violations = find_completion_violations(completion_body(content=f"{VISION_UNEXPECTED_PREFIX} [] {NONCE}"), expectation)
        assert any(VISION_OK_PREFIX in violation for violation in violations)

    def test_a_null_fingerprint_is_a_violation(self) -> None:
        body = completion_body()
        body["system_fingerprint"] = None
        assert any("system_fingerprint" in violation for violation in find_completion_violations(body, TEXT_EXPECTATION))

    def test_deployment_conditions_are_environment_not_contract(self) -> None:
        horde_unavailable = {"error": {"message": "m", "type": "api_error", "param": None, "code": "HordeUnavailable"}}
        verdict = judge_front_response(
            TEXT_EXPECTATION,
            None,
            status_code=502,
            headers={},
            body=horde_unavailable,
            transcript=None,
            streamed=False,
            include_usage=False,
            cors_expected=False,
        )
        assert verdict.environment_condition == EnvironmentCondition.HORDE_UNAVAILABLE and verdict.violations == []
        proxy_page = judge_front_response(
            TEXT_EXPECTATION,
            None,
            status_code=503,
            headers={},
            body=None,
            transcript=None,
            streamed=False,
            include_usage=False,
            cors_expected=False,
        )
        assert proxy_page.environment_condition == EnvironmentCondition.PROXY_ERROR

    def test_the_error_type_and_param_are_checked(self) -> None:
        expectation = ErrorExpectation(
            status=400, codes=frozenset({"UnsupportedChatParameter"}), param="temperature", should_not_retry=False
        )
        wrong = {"error": {"message": "m", "type": "api_error", "param": None, "code": "UnsupportedChatParameter"}}
        verdict = judge_front_response(
            expectation,
            None,
            status_code=400,
            headers={},
            body=wrong,
            transcript=None,
            streamed=False,
            include_usage=False,
            cors_expected=False,
        )
        assert any("error.type" in violation for violation in verdict.violations)
        assert any("error.param" in violation for violation in verdict.violations)


class TestOversizedBody:
    def test_the_body_is_valid_json_of_exactly_the_requested_size(self) -> None:
        body = build_oversized_body("stress/chat-conformant", 5000)
        assert len(body) == 5000
        parsed = json.loads(body)
        assert parsed["model"] == "stress/chat-conformant"
        assert set(parsed["messages"][0]["content"]) == {"a"}

    def test_a_size_too_small_for_a_request_is_refused(self) -> None:
        with pytest.raises(ValueError):
            build_oversized_body("stress/chat-conformant", 10)


def chat_mix_options(**overrides: Any) -> SimpleNamespace:
    """Return parsed chat mix options at their defaults, with ``overrides`` by short name.

    Args:
        **overrides: Option values without the ``chat_mix_`` prefix, such as ``workers=3``.
    """
    defaults: dict[str, Any] = {
        "front_host": "",
        "front_clients": 0,
        "native_clients": 0,
        "workers": 0,
        "worker_names": "",
        "worker_api_key": "",
        "worker_personas": "conformant,limited,flaky",
        "model": "stress/chat-conformant",
        "limited_model": "stress/chat-limited",
        "flaky_model": "stress/chat-flaky",
        "flaky_rate": 0.3,
        "group_weights": "ordinary=6,tools=4,structured=3,conformance=2,gap=2,misuse=3,lite=2,vision=1",
        "stream_chance": 0.5,
        "include_usage_chance": 0.5,
        "gen_time_min": 0.0,
        "gen_time_max": 0.0,
        "vision": False,
        "skip_fault_verify": False,
        "aborting_per_hour": 2,
        "anonymous_share": 0.1,
        "fail_fast_share": 0.0,
        "stream_abandon_share": 0.0,
        "stream_abandon_chunks": 2,
        "oversized_per_hour": 2,
        "worker_progress": False,
        "worker_progress_interval": 1.0,
    }
    return SimpleNamespace(**{f"chat_mix_{option}": value for option, value in {**defaults, **overrides}.items()})


class TestSettings:
    def configure(self, **overrides: Any) -> ChatMixSettings:
        settings = ChatMixSettings()
        settings.configure(chat_mix_options(**overrides), horde_host="http://horde.test", topology=RunTopology(role=RunnerRole.LOCAL))
        return settings

    def test_configured_names_pair_one_to_one_with_personas_under_the_owner_key(self) -> None:
        settings = self.configure(workers=3, worker_names="sdwb, bvqr, ztmg", worker_api_key="owner-key")
        slots = [settings.slot_at(settings.allocate_slot_index()) for _ in range(3)]
        assert [(slot.persona, slot.configured_name, slot.api_key) for slot in slots if slot is not None] == [
            (ChatPersonaRole.CONFORMANT, "sdwb", "owner-key"),
            (ChatPersonaRole.LIMITED, "bvqr", "owner-key"),
            (ChatPersonaRole.FLAKY, "ztmg", "owner-key"),
        ]
        assert settings.slot_at(settings.allocate_slot_index()) is None

    def test_names_without_their_owner_key_are_refused(self) -> None:
        with pytest.raises(ChatMixConfigurationError, match="worker-api-key"):
            self.configure(workers=1, worker_names="sdwb")

    def test_more_workers_than_names_are_refused(self) -> None:
        with pytest.raises(ChatMixConfigurationError, match="worker-names"):
            self.configure(workers=3, worker_names="sdwb,bvqr", worker_api_key="owner-key")

    def test_personas_repeat_over_slots_while_each_name_keeps_one_persona(self) -> None:
        settings = self.configure(workers=4, worker_personas="conformant,limited", worker_names="a,b,c,d", worker_api_key="owner-key")
        slots = [settings.slot_at(slot_index) for slot_index in range(4)]
        assert [(slot.configured_name, slot.persona.value) for slot in slots if slot is not None] == [
            ("a", "conformant"),
            ("b", "limited"),
            ("c", "conformant"),
            ("d", "limited"),
        ]

    def test_the_aborting_budget_is_bounded_and_held_by_worker_process_zero_only(self) -> None:
        with pytest.raises(ChatMixConfigurationError, match="aborting"):
            self.configure(aborting_per_hour=7)
        assert self.configure().aborting_budget.limit_per_hour == 2
        budgets = []
        for worker_index in range(3):
            settings = ChatMixSettings()
            topology = RunTopology(role=RunnerRole.WORKER, worker_index=worker_index, expected_workers=3)
            settings.configure(chat_mix_options(aborting_per_hour=6), horde_host="http://horde.test", topology=topology)
            budgets.append(settings.aborting_budget.limit_per_hour)
        assert budgets == [6, 0, 0]

    def test_the_anonymous_share_is_a_fraction(self) -> None:
        assert self.configure(anonymous_share=0.25).anonymous_share == 0.25
        with pytest.raises(ChatMixConfigurationError, match="anonymous-share"):
            self.configure(anonymous_share=1.5)

    def test_the_fail_fast_and_abandon_options_parse_and_apply(self) -> None:
        parser = get_empty_argument_parser()
        _add_chat_mix_arguments(parser)
        defaults = parser.parse_args([])
        assert (defaults.chat_mix_fail_fast_share, defaults.chat_mix_stream_abandon_share, defaults.chat_mix_stream_abandon_chunks) == (
            0.0,
            0.0,
            2,
        )
        options = parser.parse_args(
            [
                "--chat-mix-fail-fast-share",
                "0.25",
                "--chat-mix-stream-abandon-share",
                "0.5",
                "--chat-mix-stream-abandon-chunks",
                "3",
            ]
        )
        settings = ChatMixSettings()
        settings.configure(options, horde_host="http://horde.test", topology=RunTopology(role=RunnerRole.LOCAL))
        assert (settings.fail_fast_share, settings.stream_abandon_share, settings.stream_abandon_chunks) == (0.25, 0.5, 3)

    def test_the_fail_fast_and_abandon_shares_are_fractions_and_the_chunk_count_is_positive(self) -> None:
        with pytest.raises(ChatMixConfigurationError, match="fail-fast-share"):
            self.configure(fail_fast_share=-0.1)
        with pytest.raises(ChatMixConfigurationError, match="stream-abandon-share"):
            self.configure(stream_abandon_share=1.5)
        with pytest.raises(ChatMixConfigurationError, match="stream-abandon-chunks"):
            self.configure(stream_abandon_chunks=0)

    def test_front_clients_need_a_front_host(self) -> None:
        with pytest.raises(ChatMixConfigurationError, match="front-host"):
            self.configure(front_clients=1)
        assert self.configure(front_clients=1, front_host="https://front.test/v1").front_host == "https://front.test"


class TestCoverageGaps:
    weights = {ScenarioGroup.ORDINARY: 1, ScenarioGroup.GAP: 1, ScenarioGroup.VISION: 0}

    def gaps(self, coverage: ChatMixCoverage, *, expected_chat_workers: int = 0, run_seconds: float = 60) -> list[str]:
        return find_coverage_gaps(
            coverage=coverage,
            expected_chat_workers=expected_chat_workers,
            front_clients=1,
            group_weights=self.weights,
            run_seconds=run_seconds,
        )

    def test_a_worker_that_never_came_online_is_a_gap(self) -> None:
        coverage = ChatMixCoverage(
            assigned_workers={"sdwb": "conformant", "bvqr": "limited"},
            online_workers={"sdwb"},
            completed_front_scenarios=1,
        )
        assert self.gaps(coverage, expected_chat_workers=2) == ["the limited worker bvqr never came online"]

    def test_fewer_started_workers_than_asked_for_is_a_gap(self) -> None:
        coverage = ChatMixCoverage(assigned_workers={"sdwb": "conformant"}, online_workers={"sdwb"}, completed_front_scenarios=1)
        assert self.gaps(coverage, expected_chat_workers=3) == ["only 1 of 3 chat workers started"]

    def test_no_completed_scenario_and_a_stopped_worker_are_gaps(self) -> None:
        coverage = ChatMixCoverage(stopped_workers=["the conformant worker stopped"])
        assert self.gaps(coverage) == [
            "the conformant worker stopped",
            "no front scenario outside the misuse group completed with its expected outcome",
        ]

    def test_an_unattempted_weighted_group_is_a_gap_only_in_a_long_run(self) -> None:
        coverage = ChatMixCoverage(completed_front_scenarios=3)
        coverage.group_attempts[ScenarioGroup.ORDINARY.value] = 3
        assert self.gaps(coverage, run_seconds=120) == []
        assert self.gaps(coverage, run_seconds=600) == ["weighted scenario groups ['gap'] recorded no attempt in 600 seconds"]


class TestDistributedRun:
    def configure(self, topology: RunTopology, **overrides: Any) -> ChatMixSettings:
        settings = ChatMixSettings()
        settings.configure(chat_mix_options(**overrides), horde_host="http://horde.test", topology=topology)
        return settings

    def worker(self, worker_index: int, **overrides: Any) -> ChatMixSettings:
        return self.configure(RunTopology(role=RunnerRole.WORKER, worker_index=worker_index, expected_workers=2), **overrides)

    def test_worker_processes_get_distinct_slots_from_the_master_whatever_their_share(self) -> None:
        master = self.configure(
            RunTopology(role=RunnerRole.MASTER, expected_workers=2), workers=6, worker_names="n0,n1,n2,n3,n4,n5", worker_api_key="k"
        )
        claims_by_process = {0: WorkerSlotClaims(), 1: WorkerSlotClaims()}
        granted: dict[int, list[str]] = {0: [], 1: []}
        # Locust's dispatcher can give one process four chat workers and the other two.
        claim_order = [0, 0, 1, 0, 1, 0]
        for worker_index in claim_order:
            worker_settings = self.worker(worker_index, workers=6, worker_names="n0,n1,n2,n3,n4,n5", worker_api_key="k")
            runner = LoopbackRunner(master=master, claims=claims_by_process[worker_index], client_id=f"client-{worker_index}")
            with use_settings(worker_settings):
                slot_index = claims_by_process[worker_index].claim(runner, timeout_seconds=1)
            assert slot_index is not None
            slot = worker_settings.slot_at(slot_index)
            assert slot is not None and slot.configured_name is not None
            granted[worker_index].append(slot.configured_name)
        assert granted == {0: ["n0", "n1", "n3", "n5"], 1: ["n2", "n4"]}

    def test_a_claim_the_master_never_answers_gives_up(self) -> None:
        silent_runner = SimpleNamespace(send_message=lambda *_arguments, **_options: None)
        assert WorkerSlotClaims().claim(silent_runner, timeout_seconds=0.05) is None

    def test_the_master_refuses_duplicate_names_in_active_slots(self) -> None:
        master = RunTopology(role=RunnerRole.MASTER, expected_workers=2)
        with pytest.raises(ChatMixConfigurationError, match="unique name"):
            self.configure(master, workers=3, worker_names="a,b,a", worker_api_key="owner-key")
        self.configure(master, workers=2, worker_names="a,b,a", worker_api_key="owner-key")

    def test_the_master_refuses_fewer_names_than_the_run_has_workers(self) -> None:
        master = RunTopology(role=RunnerRole.MASTER, expected_workers=2)
        with pytest.raises(ChatMixConfigurationError, match="needs as many"):
            self.configure(master, workers=6, worker_names="a,b,c,d,e", worker_api_key="owner-key")

    def test_the_master_gates_on_the_merged_reports_of_its_workers(self) -> None:
        master = self.configure(RunTopology(role=RunnerRole.MASTER, expected_workers=2), workers=6, front_clients=4, front_host="https://f")
        reports = []
        for worker_index, online in ((0, ("w0a", "w0b", "w0c")), (1, ("w1a", "w1c"))):
            worker_coverage = ChatMixCoverage(
                assigned_workers={
                    f"w{worker_index}{letter}": persona for letter, persona in zip("abc", ("conformant", "limited", "flaky"), strict=True)
                },
                online_workers=set(online),
                completed_front_scenarios=2,
            )
            worker_coverage.group_attempts["ordinary"] = 5
            reports.append((f"client-{worker_index}", {"chat_mix_coverage": worker_coverage.to_report(), "stats": []}))
        with use_settings(master):
            for client_id, data in reports:
                chat_mix_settings_module._receive_chat_mix_coverage(client_id=client_id, data=data)
            chat_mix_settings_module._receive_chat_mix_coverage(client_id="client-9", data={"stats": []})
            merged = coverage_for_gate(master)

        assert merged.completed_front_scenarios == 4
        assert merged.group_attempts["ordinary"] == 10
        assert find_coverage_gaps(
            coverage=merged, expected_chat_workers=6, front_clients=4, group_weights={ScenarioGroup.ORDINARY: 1}, run_seconds=60
        ) == ["the limited worker w1b never came online"]

    def test_a_later_report_from_a_worker_replaces_its_earlier_one(self) -> None:
        master = self.configure(RunTopology(role=RunnerRole.MASTER, expected_workers=1))
        with use_settings(master):
            first = ChatMixCoverage(completed_front_scenarios=1)
            later = ChatMixCoverage(completed_front_scenarios=3)
            chat_mix_settings_module._receive_chat_mix_coverage(client_id="client-0", data={"chat_mix_coverage": first.to_report()})
            chat_mix_settings_module._receive_chat_mix_coverage(client_id="client-0", data={"chat_mix_coverage": later.to_report()})
            assert coverage_for_gate(master).completed_front_scenarios == 3

    def test_a_worker_adds_its_coverage_to_its_stats_report(self) -> None:
        worker = self.worker(0, workers=3)
        worker.mark_worker_started("w0a", ChatPersonaRole.CONFORMANT)
        worker.mark_worker_online("w0a", ChatPersonaRole.CONFORMANT)
        data: dict[str, Any] = {}
        with use_settings(worker):
            chat_mix_settings_module._report_chat_mix_coverage(client_id="client-0", data=data)
        assert data["chat_mix_coverage"]["online_workers"] == ["w0a"]
        assert json.loads(json.dumps(data)) == data

    def test_the_topology_comes_from_the_runner(self) -> None:
        options = SimpleNamespace(expect_workers=2)
        # Bare runner objects: built without a connection, so their clean-up has nothing to clean.
        stub_worker_runner_class = type("StubWorkerRunner", (WorkerRunner,), {"__del__": lambda _self: None})
        stub_master_runner_class = type("StubMasterRunner", (MasterRunner,), {"__del__": lambda _self: None})
        worker_runner = object.__new__(stub_worker_runner_class)
        worker_runner.worker_index = 1
        assert build_run_topology(SimpleNamespace(parsed_options=options, runner=worker_runner)) == RunTopology(
            role=RunnerRole.WORKER, worker_index=1, expected_workers=2
        )
        assert (
            build_run_topology(SimpleNamespace(parsed_options=options, runner=object.__new__(stub_master_runner_class))).role
            == RunnerRole.MASTER
        )
        assert build_run_topology(SimpleNamespace(parsed_options=options, runner=None)).role == RunnerRole.LOCAL


class LoopbackRunner:
    """A worker runner whose messages reach the master's slot handler at once, and whose grants come straight back."""

    def __init__(self, *, master: ChatMixSettings, claims: WorkerSlotClaims, client_id: str) -> None:
        self.master = master
        self.claims = claims
        self.client_id = client_id

    def send_message(self, msg_type: str, data: dict[str, Any], client_id: str | None = None) -> None:
        if msg_type == SLOT_CLAIM_MESSAGE:
            master_environment = SimpleNamespace(runner=self)
            with use_settings(self.master):
                chat_mix_settings_module._grant_worker_slot(master_environment, SimpleNamespace(data=data, node_id=self.client_id))
            return
        assert msg_type == SLOT_GRANT_MESSAGE and client_id == self.client_id
        self.claims.resolve(data["claim_id"], data["slot"])


@contextmanager
def use_settings(settings: ChatMixSettings) -> Iterator[None]:
    """Make ``settings`` the process-wide chat mix settings the event listeners read, for the duration."""
    original = chat_mix_settings_module.chat_mix_settings
    chat_mix_settings_module.chat_mix_settings = settings
    try:
        yield
    finally:
        chat_mix_settings_module.chat_mix_settings = original


class TestScaleBounds:
    def test_model_card_checks_run_once_per_interval_per_process(self) -> None:
        gate = IntervalGate(30)
        assert [gate.try_open(now) for now in (0.0, 1.0, 29.9, 30.0, 31.0)] == [True, False, False, True, False]

    def test_an_hourly_budget_never_holds_more_than_its_limit(self) -> None:
        budget = HourlyBudget(6)
        for second in range(10_000):
            budget.try_take(float(second))
        assert len(budget._taken_at) <= 6


class TestAnonymousVariant:
    def test_an_anonymous_variant_keeps_the_outcome_and_drops_the_key(self) -> None:
        scenario = SCENARIO_BUILDERS[ScenarioGroup.ORDINARY][0](CONTEXT)
        anonymous = as_anonymous(scenario)
        assert anonymous.name == "plain_prompt anonymous"
        assert anonymous.expectation == scenario.expectation
        assert build_authorization_headers(anonymous.authorization, api_key="key") == {}
        assert anonymous.kudos_upfront_benign is False

    def test_kudos_upfront_is_benign_only_above_the_upfront_free_size(self) -> None:
        at_limit_builder = next(
            builder for builder in SCENARIO_BUILDERS[ScenarioGroup.ORDINARY] if builder.__name__ == "max_tokens_at_worker_limit"
        )
        at_free_size = at_limit_builder(dataclasses.replace(CONTEXT, worker_max_length=UPFRONT_KUDOS_FREE_TOKENS))
        above_free_size = at_limit_builder(dataclasses.replace(CONTEXT, worker_max_length=UPFRONT_KUDOS_FREE_TOKENS + 1))
        assert as_anonymous(at_free_size).kudos_upfront_benign is False
        assert as_anonymous(above_free_size).kudos_upfront_benign is True

    def test_kudos_upfront_on_a_small_anonymous_request_is_a_violation(self) -> None:
        envelope = {"error": {"message": "upfront", "type": "permission_error", "param": None, "code": "KudosUpfront"}}
        arguments: dict[str, Any] = {
            "status_code": 403,
            "headers": {},
            "body": envelope,
            "transcript": None,
            "streamed": False,
            "include_usage": False,
            "cors_expected": False,
        }
        assert judge_front_response(TEXT_EXPECTATION, None, **arguments).benign_refusal == BenignRefusal.KUDOS_UPFRONT
        verdict = judge_front_response(TEXT_EXPECTATION, None, kudos_upfront_benign=False, **arguments)
        assert verdict.violations == ["a request too small for upfront kudos was refused with KudosUpfront"]


QUEUE_UNAVAILABLE_MESSAGES: dict[str, str] = {
    "no_eligible_threads": "The online pool has zero worker threads eligible for this request, and none is processing it. "
    "The first status read reported 0 eligible worker threads online, 300 s left in the budget.",
    "wait_exceeds_budget": "The estimated wait exceeds the time left in this request's budget. The first status read "
    "reported 4 eligible worker threads online, an estimated wait of 900 s, 300 s left in the budget.",
}
"""The front's ``queue_unavailable`` message for each refusal reason."""

QUEUE_UNAVAILABLE_HEADERS: dict[str, str] = {"retry-after": "30", "x-should-retry": "false"}
"""The headers the front sends with a ``queue_unavailable`` refusal."""


def queue_unavailable_envelope(reason: str) -> dict[str, Any]:
    """Return the front's ``queue_unavailable`` refusal for a reason."""
    return {
        "error": {
            "message": QUEUE_UNAVAILABLE_MESSAGES[reason],
            "type": "api_error",
            "param": "horde.availability_mode",
            "code": "queue_unavailable",
            "metadata": {"request_id": "req-1", "retry_after": 30},
        }
    }


class TestFailFastVariant:
    def judge_refusal(self, *, fail_fast: bool, headers: dict[str, str], streamed: bool = False) -> Any:
        return judge_front_response(
            TEXT_EXPECTATION,
            None,
            status_code=503,
            headers=headers,
            body=queue_unavailable_envelope("no_eligible_threads"),
            transcript=None,
            streamed=streamed,
            include_usage=False,
            cors_expected=False,
            fail_fast=fail_fast,
        )

    def test_a_fail_fast_variant_asks_the_mode_and_keeps_the_outcome(self) -> None:
        scenario = plain_prompt(CONTEXT)
        fail_fast = as_fail_fast(scenario)
        assert fail_fast.name == "plain_prompt fail_fast"
        assert fail_fast.body["horde"] == {"availability_mode": "fail_fast"}
        assert fail_fast.expectation == scenario.expectation
        assert fail_fast.fail_fast is True
        assert "horde" not in scenario.body

    def test_only_scenarios_that_reach_the_queue_in_their_own_shape_get_the_mode(self) -> None:
        assert ScenarioGroup.MISUSE not in FAIL_FAST_SHARE_GROUPS
        assert ScenarioGroup.LITE not in FAIL_FAST_SHARE_GROUPS
        assert ScenarioGroup.ORDINARY in FAIL_FAST_SHARE_GROUPS

    @pytest.mark.parametrize("streamed", [False, True])
    def test_a_queue_unavailable_refusal_of_a_fail_fast_request_is_benign(self, streamed: bool) -> None:
        verdict = self.judge_refusal(fail_fast=True, headers=QUEUE_UNAVAILABLE_HEADERS, streamed=streamed)
        assert verdict.violations == []
        assert verdict.benign_refusal == BenignRefusal.QUEUE_UNAVAILABLE

    def test_a_queue_unavailable_refusal_without_fail_fast_is_a_violation(self) -> None:
        verdict = self.judge_refusal(fail_fast=False, headers=QUEUE_UNAVAILABLE_HEADERS)
        assert verdict.benign_refusal is None
        assert verdict.violations and "expected a completion" in verdict.violations[0]

    def test_a_fail_fast_refusal_without_retry_after_is_a_violation(self) -> None:
        verdict = self.judge_refusal(fail_fast=True, headers={"x-should-retry": "false"})
        assert verdict.benign_refusal is None
        assert verdict.violations == ["retry-after is missing"]

    def test_the_refusal_reason_is_read_from_the_message(self) -> None:
        for reason in QUEUE_UNAVAILABLE_MESSAGES:
            assert queue_unavailable_reason(queue_unavailable_envelope(reason)) == reason
        assert queue_unavailable_reason({"error": {"message": "Refused."}}) is None


class TestAbandonedStreamChecks:
    def test_the_lines_before_the_close_pass_without_a_finish_or_done(self) -> None:
        lines = stream_lines(delta_chunks=[{"content": "Hel"}, {"content": "lo"}], include_usage=False)
        transcript = read_transcript(lines[:4])
        assert transcript.reply_chunks == 1
        assert transcript.content_characters == 3
        assert find_abandoned_stream_violations(transcript, TEXT_EXPECTATION) == []

    def test_a_first_chunk_with_content_and_an_unparsable_line_are_violations(self) -> None:
        lines = stream_lines(delta_chunks=[{"content": "Hel"}], include_usage=False)
        transcript = read_transcript([lines[3], "data: {not json"])
        violations = find_abandoned_stream_violations(transcript, TEXT_EXPECTATION)
        assert any("not JSON objects" in violation for violation in violations)
        assert any("first chunk's delta" in violation for violation in violations)


SCRIPTED_FRONT_HOLD_SECONDS: float = 5.0
"""How long the scripted front waits for the client to close the connection before it sends the rest of a stream."""


class ScriptedFront:
    """Serve one scripted response on a local port over a plain socket, so a test sees the connection as the front
    does: whether the client closed it, and after which event.

    A stream goes out in HTTP/1.1 chunked encoding, one SSE event per chunk, as the front's server sends it. Before the
    event at ``hold_before``, the server waits for the client to close the connection, and sends nothing more if it
    did.
    """

    def __init__(
        self,
        *,
        status_line: str,
        headers: dict[str, str],
        body: bytes = b"",
        events: list[bytes] | None = None,
        hold_before: int | None = None,
    ) -> None:
        self.status_line = status_line
        self.headers = headers
        self.body = body
        self.events = events
        self.hold_before = hold_before
        self.request_bodies: list[dict[str, Any]] = []
        self.sent_events = 0
        self.client_closed = False
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def finish(self) -> None:
        """Wait for the response to end and stop listening."""
        self.thread.join(timeout=SCRIPTED_FRONT_HOLD_SECONDS * 2)
        self.listener.close()

    def _serve(self) -> None:
        """Answer the one connection the client opens with the scripted response."""
        connection, _address = self.listener.accept()
        with connection:
            self.request_bodies.append(json.loads(self._read_request(connection)))
            head = f"HTTP/1.1 {self.status_line}\r\n" + "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())
            if self.events is None:
                connection.sendall(f"{head}Content-Length: {len(self.body)}\r\n\r\n".encode() + self.body)
                return
            connection.sendall(f"{head}Transfer-Encoding: chunked\r\n\r\n".encode())
            for event_index, event in enumerate(self.events):
                if event_index == self.hold_before and self._waits_for_close(connection):
                    self.client_closed = True
                    return
                connection.sendall(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                self.sent_events += 1
            connection.sendall(b"0\r\n\r\n")

    @staticmethod
    def _read_request(connection: socket.socket) -> bytes:
        """Return the body of the request on ``connection``, read up to its ``Content-Length``."""
        received = b""
        while b"\r\n\r\n" not in received:
            received += connection.recv(4096)
        head, _separator, body = received.partition(b"\r\n\r\n")
        content_length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:"))
        while len(body) < content_length:
            body += connection.recv(4096)
        return body

    @staticmethod
    def _waits_for_close(connection: socket.socket) -> bool:
        """Return whether the client closed ``connection`` within the hold."""
        readable, _writable, _errored = select.select([connection], [], [], SCRIPTED_FRONT_HOLD_SECONDS)
        if not readable:
            return False
        try:
            return connection.recv(1) == b""
        except ConnectionError:
            return True


def sse_events(lines: list[str]) -> list[bytes]:
    """Return the SSE events of stream lines, each line an event of its own."""
    return [f"{line}\n\n".encode() for line in lines if line]


def recorded_front_client(front_url: str, **overrides: Any) -> tuple[ChatMixFrontClient, list[dict[str, Any]]]:
    """Return a front client for the front at ``front_url``, with the settings overridden, and the request events it
    fires."""
    chat_mix_settings.configure(
        chat_mix_options(front_host=front_url, front_clients=1, **overrides),
        horde_host="http://horde.test",
        topology=RunTopology(role=RunnerRole.LOCAL),
    )
    chat_mix_settings.include_usage_chance = 0.0
    # Locust reads the host from the user class, which a run sets from --host.
    hosted_client_class = type("HostedChatMixFrontClient", (ChatMixFrontClient,), {"host": front_url})
    environment = Environment(user_classes=[hosted_client_class])
    events: list[dict[str, Any]] = []
    environment.events.request.add_listener(lambda **event: events.append(event))
    front_client = hosted_client_class(environment)
    front_client.api_key = "requestor-key"
    front_client.rng = random.Random(7)
    return front_client, events


class TestFrontClientShares:
    @pytest.mark.parametrize("streamed", [False, True])
    @pytest.mark.parametrize("reason", sorted(QUEUE_UNAVAILABLE_MESSAGES))
    def test_a_fail_fast_refusal_is_counted_by_its_reason(self, streamed: bool, reason: str) -> None:
        front = ScriptedFront(
            status_line="503 Service Unavailable",
            headers={"Content-Type": "application/json", **QUEUE_UNAVAILABLE_HEADERS},
            body=json.dumps(queue_unavailable_envelope(reason)).encode(),
        )
        client, events = recorded_front_client(front.url, stream_chance=1.0 if streamed else 0.0)

        client.send_scenario(as_fail_fast(plain_prompt(CONTEXT)))
        front.finish()

        (sent_body,) = front.request_bodies
        assert sent_body["horde"] == {"availability_mode": "fail_fast"}
        assert sent_body.get("stream", False) is streamed
        row = f"/v1/chat/completions [plain_prompt fail_fast] [{'stream' if streamed else 'non-stream'}]"
        assert sorted((event["name"], event["exception"]) for event in events) == [
            (row, None),
            (f"{row} [benign queue_unavailable {reason}]", None),
        ]

    def test_a_fail_fast_share_applies_only_to_the_queued_groups(self) -> None:
        client, _events = recorded_front_client("http://front.test", fail_fast_share=1.0)

        assert client._sends_fail_fast(plain_prompt(CONTEXT)) is True
        misuse = SCENARIO_BUILDERS[ScenarioGroup.MISUSE][0](CONTEXT)
        assert client._sends_fail_fast(misuse) is False
        chat_mix_settings.fail_fast_share = 0.0
        assert client._sends_fail_fast(plain_prompt(CONTEXT)) is False

    def test_an_abandoning_client_closes_the_connection_after_the_configured_content_chunks(self) -> None:
        lines = stream_lines(delta_chunks=[{"content": "Hel"}, {"content": "lo "}, {"content": "nonce123"}], include_usage=False)
        events = sse_events(lines)
        # The keepalive, the role chunk and two content chunks precede the hold; the third content chunk follows it.
        front = ScriptedFront(
            status_line="200 OK",
            headers={"Content-Type": "text/event-stream", "x-should-retry": "false"},
            events=events,
            hold_before=4,
        )
        client, request_events = recorded_front_client(front.url, stream_chance=1.0, stream_abandon_share=1.0, stream_abandon_chunks=2)

        client.send_scenario(plain_prompt(CONTEXT))
        front.finish()

        assert front.client_closed is True
        assert front.sent_events == 4
        (request_event,) = request_events
        assert request_event["name"] == "/v1/chat/completions [plain_prompt] [stream] [abandoned]"
        assert request_event["exception"] is None
        assert request_event["response_length"] == sum(len(event) for event in events[:4])
        assert chat_mix_settings.coverage.completed_front_scenarios == 1

    def test_a_stream_that_ends_before_the_abandon_count_is_judged_whole(self) -> None:
        lines = stream_lines(delta_chunks=[{"content": "Hello nonce123"}], include_usage=False)
        front = ScriptedFront(
            status_line="200 OK",
            headers={"Content-Type": "text/event-stream", "x-should-retry": "false"},
            events=sse_events(lines),
        )
        client, request_events = recorded_front_client(front.url, stream_chance=1.0, stream_abandon_share=1.0, stream_abandon_chunks=2)

        client.send_scenario(plain_prompt(CONTEXT))
        front.finish()

        assert front.client_closed is False
        assert [(event["name"], event["exception"]) for event in request_events if event["request_type"] == "POST"] == [
            ("/v1/chat/completions [plain_prompt] [stream]", None)
        ]


class TestLeaveOutByRunner:
    def user_class(self, name: str, *, tasks: list[object], fixed_count: int = 0) -> type:
        return type(name, (), {"tasks": tasks, "fixed_count": fixed_count, "weight": 1})

    def classes(self) -> tuple[type, type, type]:
        return (
            self.user_class("RequestGenerator", tasks=[object()]),
            self.user_class("ChatMixFrontClient", tasks=[object()]),
            self.user_class("MisuseUser", tasks=[]),
        )

    def stub_runner(self, runner_class: type) -> object:
        stub_class = type(f"Stub{runner_class.__name__}", (runner_class,), {"__del__": lambda _self: None})
        return object.__new__(stub_class)

    def test_a_worker_process_keeps_every_class_the_master_may_name(self) -> None:
        kept, unrequested, filtered = self.classes()
        environment = SimpleNamespace(user_classes=[kept, unrequested, filtered], runner=self.stub_runner(WorkerRunner))
        _leave_out_user_classes(environment, {unrequested: 0})
        assert environment.user_classes == [kept, unrequested, filtered]

    def test_a_master_dispatches_none_of_them_by_weight_zero(self) -> None:
        kept, unrequested, filtered = self.classes()
        environment = SimpleNamespace(user_classes=[kept, unrequested, filtered], runner=self.stub_runner(MasterRunner))
        _leave_out_user_classes(environment, {unrequested: 0})
        assert environment.user_classes == [kept, unrequested, filtered]
        assert [user_class.weight for user_class in environment.user_classes] == [1, 0, 0]

    def test_a_local_run_removes_them(self) -> None:
        kept, unrequested, filtered = self.classes()
        environment = SimpleNamespace(user_classes=[kept, unrequested, filtered], runner=None)
        _leave_out_user_classes(environment, {unrequested: 0})
        assert environment.user_classes == [kept]


class TestOptInClasses:
    def user_class(self, name: str, *, tasks: list[object], fixed_count: int = 0) -> type:
        return type(name, (), {"tasks": tasks, "fixed_count": fixed_count})

    def test_an_opt_in_class_without_a_count_is_left_out_and_others_stay(self) -> None:
        image_requester = self.user_class("RequestGenerator", tasks=[object()])
        unrequested = self.user_class("ChatMixFrontClient", tasks=[object()])
        requested = self.user_class("ChatMixWorker", tasks=[object()], fixed_count=3)
        environment = SimpleNamespace(user_classes=[image_requester, unrequested, requested])

        _remove_unrequested_opt_in_classes(environment, {unrequested: 0, requested: 3})
        _remove_tag_filtered_user_classes(environment)

        assert environment.user_classes == [image_requester, requested]

    def test_a_requested_opt_in_class_whose_tasks_a_tag_filter_removed_stops_the_run(self) -> None:
        filtered = self.user_class("ChatMixWorker", tasks=[], fixed_count=3)
        environment = SimpleNamespace(user_classes=[filtered])

        _remove_unrequested_opt_in_classes(environment, {filtered: 3})
        with pytest.raises(RuntimeError, match="3 fixed users"):
            _remove_tag_filtered_user_classes(environment)


class FakeResponse:
    """A stand-in for Locust's response context manager, recording how the user marked it."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(body)
        self.headers: dict[str, str] = {}
        self.error = None
        self.elapsed = timedelta(0)
        self.request = SimpleNamespace(method="POST")
        self.request_meta: dict[str, Any] = {}
        self.marked: str | None = None
        self._body = body

    def json(self) -> Any:
        return self._body

    def success(self) -> None:
        self.marked = "success"

    def failure(self, message: str) -> None:
        self.marked = message

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exception: object) -> bool:
        return False


class FakeClient:
    """A stand-in for Locust's HTTP session, answering posts from a queue and keeping what was sent."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.sent: list[dict[str, Any]] = []

    def post(self, _path: str, *, json: dict[str, Any], **_options: Any) -> FakeResponse:
        self.sent.append(json)
        return self.responses.pop(0)


class TestChatMixWorker:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_worker_user.time, "sleep", lambda _seconds: None)

    def build_worker(self, responses: list[FakeResponse], **overrides: Any) -> ChatMixWorker:
        chat_mix_settings.configure(
            chat_mix_options(**{"workers": 1, **overrides}), horde_host="", topology=RunTopology(role=RunnerRole.LOCAL)
        )
        # Locust reads the host from the user class, which a run sets from --host.
        hosted_worker_class = type("HostedChatMixWorker", (ChatMixWorker,), {"host": "http://horde.test"})
        worker = hosted_worker_class(Environment(user_classes=[hosted_worker_class]))
        worker.on_start()
        worker.client = FakeClient(responses)
        return worker

    def test_a_configured_name_refused_three_times_stops_and_leaves_a_coverage_gap(self) -> None:
        refusals = [FakeResponse(403, {"message": "not yours", "rc": "WrongCredentials"}) for _ in range(3)]
        worker = self.build_worker(refusals, worker_names="sdwb", worker_api_key="owner-key")
        assert worker.api_key == "owner-key" and worker.worker_name == "sdwb"

        for _attempt in range(2):
            with pytest.raises(RescheduleTask):
                worker.pop_and_serve()
        with pytest.raises(StopUser):
            worker.pop_and_serve()

        assert all(refusal.marked == "success" for refusal in refusals)
        assert not chat_mix_settings.is_persona_online(ChatPersonaRole.CONFORMANT)
        assert chat_mix_settings.coverage.stopped_workers == ["the conformant worker sdwb stopped after 3 pop refusals (WrongCredentials)"]

    def test_a_generated_name_in_maintenance_stops_at_once_without_a_new_name(self) -> None:
        worker = self.build_worker([FakeResponse(403, {"message": "maintenance", "rc": "WorkerMaintenance"})])
        name_before = worker.worker_name

        with pytest.raises(StopUser):
            worker.pop_and_serve()

        assert worker.worker_name == name_before
        assert len(chat_mix_settings.coverage.stopped_workers) == 1

    def test_an_unexpected_pop_refusal_is_a_contract_violation(self) -> None:
        refusal = FakeResponse(400, {"message": "bad declaration", "rc": "ChatDeclarationInvalid"})
        worker = self.build_worker([refusal])

        worker.pop_and_serve()

        assert refusal.marked is not None and refusal.marked.startswith(CONTRACT_VIOLATION_PREFIX)

    def test_the_breaker_stops_faulting_after_three_unintended_refusals(self) -> None:
        def chat_job() -> FakeResponse:
            payload = {
                "messages": [{"role": "user", "content": f"Hi {nonce_marker(NONCE)}"}],
                "temperature": 1.0,
                "top_p": 1.0,
                "frequency_penalty": 0,
                "presence_penalty": 0,
                "rep_pen": 1.0,
                "top_k": 0,
                "min_p": 0,
                "max_length": 64,
                "max_context_length": 1024,
            }
            return FakeResponse(200, {"id": "job-1", "chat": True, "model": "stress/chat-conformant", "payload": payload})

        def refused() -> FakeResponse:
            return FakeResponse(400, {"message": "nonconformant", "rc": "ChatSubmitNonconformant"})

        def faulted() -> FakeResponse:
            return FakeResponse(200, {"reward": 0})

        responses = [chat_job(), refused(), faulted(), chat_job(), refused(), faulted(), chat_job(), refused()]
        worker = self.build_worker(responses)

        worker.pop_and_serve()
        worker.pop_and_serve()
        with pytest.raises(StopUser):
            worker.pop_and_serve()

        fault_submits = [sent for sent in worker.client.sent if sent.get("state") == "faulted"]
        assert len(fault_submits) == 2
        assert responses == []
        assert chat_mix_settings.ledger.refused_nonconformant[worker.worker_name] == 3

    def test_a_refusal_is_intended_only_for_the_contract_the_reply_breaks(self) -> None:
        nonconformant = WorkerReply(content="x", tool_calls=None, finish_reason="stop", breaks_strict_contract=True)
        invalid = WorkerReply(content=None, tool_calls=[], finish_reason="tool_calls", breaks_every_contract=True)
        conforming = WorkerReply(content="x", tool_calls=None, finish_reason="stop")
        assert refusal_was_intended(nonconformant, "ChatSubmitNonconformant")
        assert not refusal_was_intended(nonconformant, "ChatSubmitInvalid")
        assert refusal_was_intended(invalid, "ChatSubmitInvalid")
        assert not refusal_was_intended(conforming, "ChatSubmitNonconformant")
        assert not refusal_was_intended(conforming, "ChatModelMismatch")


def popped_mix_chat_job(*, user_text: str, stream_progress: bool) -> FakeResponse:
    """Return a pop answer assigning one chat job the conformant persona accepts, with the given last user message."""
    payload = {
        "messages": [{"role": "user", "content": user_text}],
        "temperature": 1.0,
        "top_p": 1.0,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "rep_pen": 1.0,
        "top_k": 0,
        "min_p": 0,
        "max_length": 64,
        "max_context_length": 1024,
        "stream_progress": stream_progress,
    }
    return FakeResponse(200, {"id": "job-1", "chat": True, "model": "stress/chat-conformant", "payload": payload})


class TestChatMixWorkerProgress:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_worker_user.time, "sleep", lambda _seconds: None)

    def build_worker(self, responses: list[FakeResponse], **overrides: Any) -> ChatMixWorker:
        options = {"workers": 1, "worker_personas": "conformant", "gen_time_min": 3.0, "gen_time_max": 3.0, "worker_progress": True}
        chat_mix_settings.configure(
            chat_mix_options(**{**options, **overrides}), horde_host="", topology=RunTopology(role=RunnerRole.LOCAL)
        )
        hosted_worker_class = type("HostedChatMixWorker", (ChatMixWorker,), {"host": "http://horde.test"})
        worker = hosted_worker_class(Environment(user_classes=[hosted_worker_class]))
        worker.on_start()
        worker.client = FakeClient(responses)
        return worker

    def test_a_streamed_conforming_reply_is_posted_as_deltas_equal_to_the_submitted_text(self) -> None:
        progress_answers = [FakeResponse(200, {"accepted_seq": seq}) for seq in (1, 2, 3)]
        job = popped_mix_chat_job(user_text=f"Hi {nonce_marker(NONCE)}", stream_progress=True)
        worker = self.build_worker([job, *progress_answers, FakeResponse(200, {"reward": 1})])

        worker.pop_and_serve()

        progress_bodies = [sent for sent in worker.client.sent if "seq" in sent]
        (submit_body,) = [sent for sent in worker.client.sent if "message" in sent]
        assert [body["seq"] for body in progress_bodies] == [1, 2, 3]
        assert "".join(body["delta"]["content"] for body in progress_bodies) == submit_body["message"]["content"]
        assert [answer.request_meta["name"] for answer in progress_answers] == ["/api/v2/generate/text/progress [200]"] * 3

    def test_a_cancelled_acknowledgement_ends_the_posting_without_a_submit(self) -> None:
        cancelled_answer = FakeResponse(200, {"accepted_seq": 1, "cancelled": True})
        job = popped_mix_chat_job(user_text=f"Hi {nonce_marker(NONCE)}", stream_progress=True)
        worker = self.build_worker([job, cancelled_answer])

        worker.pop_and_serve()

        assert len([sent for sent in worker.client.sent if "seq" in sent]) == 1
        assert [sent for sent in worker.client.sent if "message" in sent] == []

    def test_a_stop_status_ends_the_posting_and_the_reply_is_still_submitted(self) -> None:
        refusal = FakeResponse(404, {"message": "not streaming", "rc": "ChatProgressNotStreaming"})
        job = popped_mix_chat_job(user_text=f"Hi {nonce_marker(NONCE)}", stream_progress=True)
        worker = self.build_worker([job, refusal, FakeResponse(200, {"reward": 1})])

        worker.pop_and_serve()

        assert len([sent for sent in worker.client.sent if "seq" in sent]) == 1
        assert len([sent for sent in worker.client.sent if "message" in sent]) == 1
        assert refusal.marked == "success"

    def test_the_mode_off_posts_no_progress(self) -> None:
        job = popped_mix_chat_job(user_text=f"Hi {nonce_marker(NONCE)}", stream_progress=True)
        worker = self.build_worker([job, FakeResponse(200, {"reward": 1})], worker_progress=False)

        worker.pop_and_serve()

        assert not [sent for sent in worker.client.sent if "seq" in sent]

    def test_a_scripted_fault_posts_no_progress(self) -> None:
        user_text = f"Hi {nonce_marker(NONCE)} {reply_marker(ReplyScript.FAULT_GENERATION_ERROR)}"
        worker = self.build_worker([popped_mix_chat_job(user_text=user_text, stream_progress=True), FakeResponse(200, {"reward": 0})])

        worker.pop_and_serve()

        assert not [sent for sent in worker.client.sent if "seq" in sent]
        assert worker.client.sent[-1].get("state") == "faulted"

    def test_only_a_conforming_text_reply_of_a_streamed_job_posts_progress(self) -> None:
        streamed = {"stream_progress": True}
        text = WorkerReply(content="x", tool_calls=None, finish_reason="stop")
        calls = [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        unposted_replies = [
            WorkerReply(content="x", tool_calls=calls, finish_reason="tool_calls"),
            WorkerReply(content=None, tool_calls=None, finish_reason="stop", fault_reason="generation_error"),
            WorkerReply(content="x", tool_calls=None, finish_reason="stop", breaks_strict_contract=True),
            WorkerReply(content="x", tool_calls=None, finish_reason="stop", breaks_every_contract=True),
        ]

        assert reply_posts_progress(streamed, text)
        assert not reply_posts_progress({}, text)
        assert not reply_posts_progress({"stream_progress": False}, text)
        assert not any(reply_posts_progress(streamed, reply) for reply in unposted_replies)

    def test_a_non_positive_interval_is_refused(self) -> None:
        with pytest.raises(ChatMixConfigurationError):
            chat_mix_settings.configure(
                chat_mix_options(worker_progress_interval=0.0), horde_host="", topology=RunTopology(role=RunnerRole.LOCAL)
            )


class TestStreamedVariants:
    def test_streamed_and_non_streamed_requests_send_the_same_conformance(self) -> None:
        for builders in SCENARIO_BUILDERS.values():
            for builder in builders:
                scenario = builder(CONTEXT)
                original_body = json.loads(json.dumps(scenario.body))
                plain = build_request_body(scenario, streamed=False, include_usage=False)
                streamed = build_request_body(scenario, streamed=True, include_usage=True)
                assert plain.get("horde") == streamed.get("horde") == original_body.get("horde"), scenario.name
                assert {key: value for key, value in streamed.items() if key not in ("stream", "stream_options")} == plain, scenario.name
                assert scenario.body == original_body, scenario.name

    def test_the_strict_and_best_effort_twins_share_no_object(self) -> None:
        strict = SCENARIO_BUILDERS[ScenarioGroup.CONFORMANCE][0](CONTEXT)
        best_effort = SCENARIO_BUILDERS[ScenarioGroup.CONFORMANCE][1](CONTEXT)
        assert (strict.name, best_effort.name) == ("nonconformant_schema_strict", "nonconformant_schema_best_effort")
        build_request_body(best_effort, streamed=True, include_usage=True)["horde"]["conformance"] = "strict"
        assert "horde" not in strict.body
        assert best_effort.body["horde"] == {"conformance": "best_effort"}

    def test_an_empty_stream_where_an_error_was_expected_says_so(self) -> None:
        faulted = ErrorExpectation(status=502, codes=frozenset({"ChatRequestFaulted"}), after_headers=True, rc="ChatSubmitNonconformant")
        verdict = judge_front_response(
            faulted,
            None,
            status_code=200,
            headers={"x-should-retry": "false"},
            body=None,
            transcript=read_transcript(["data: [DONE]"]),
            streamed=True,
            include_usage=False,
            cors_expected=False,
            nonce=NONCE,
        )
        assert "a stream with no chunk and no error event" in verdict.violations[0]

    def test_an_unexpected_completion_says_which_request_it_answers(self) -> None:
        faulted = ErrorExpectation(status=502, codes=frozenset({"ChatRequestFaulted"}), after_headers=True, rc="ChatSubmitNonconformant")
        lines = stream_lines(delta_chunks=[{"content": f"Hello {NONCE}"}])
        verdict = judge_front_response(
            faulted,
            None,
            status_code=200,
            headers={"x-should-retry": "false"},
            body=None,
            transcript=read_transcript(lines),
            streamed=True,
            include_usage=True,
            cors_expected=False,
            nonce=NONCE,
        )
        assert "answering this request" in verdict.violations[0]


class TestDistributedPersonaAvailability:
    def test_remote_only_persona_becomes_available_and_stopping_clears_it(self, monkeypatch):
        from unittest.mock import Mock

        master = ChatMixSettings()
        remote = ChatMixSettings()
        client = ChatMixSettings()
        runner = SimpleNamespace(clients={"remote": object(), "client": object()}, send_message=Mock())
        master.availability_runner = runner
        remote.coverage.started_at = 1.0
        remote.mark_worker_online("remote-worker", ChatPersonaRole.CONFORMANT)
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", remote)
        report = {}
        chat_mix_settings_module._report_chat_mix_coverage("remote", report)
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", master)
        chat_mix_settings_module._receive_chat_mix_coverage("remote", report)
        message, online = runner.send_message.call_args.args
        assert message == chat_mix_settings_module.AVAILABILITY_MESSAGE
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", client)
        assert not client.is_persona_online(ChatPersonaRole.CONFORMANT)
        chat_mix_settings_module._receive_availability(None, SimpleNamespace(data=online))
        assert client.online_workers == {}
        assert client.is_persona_online(ChatPersonaRole.CONFORMANT)
        assert ChatPersonaRole.CONFORMANT in client.persona_online_since()

        # Coverage keeps evidence that it was online, but availability must remove the stopped worker.
        remote.mark_worker_offline("remote-worker")
        assert "remote-worker" in remote.coverage.online_workers
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", remote)
        report = {}
        chat_mix_settings_module._report_chat_mix_coverage("remote", report)
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", master)
        chat_mix_settings_module._receive_chat_mix_coverage("remote", report)
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", client)
        chat_mix_settings_module._receive_availability(None, SimpleNamespace(data=runner.send_message.call_args.args[1]))
        assert not client.is_persona_online(ChatPersonaRole.CONFORMANT)

    @pytest.mark.parametrize("disconnected", [False, True])
    def test_missing_remote_reports_expire_and_disconnected_workers_are_removed(self, monkeypatch, disconnected):
        from unittest.mock import Mock

        master = ChatMixSettings()
        master.availability_runner = SimpleNamespace(clients={"client": object()}, send_message=Mock())
        now = 100.0
        monkeypatch.setattr(chat_mix_settings_module.time, "time", lambda: now)
        if not disconnected:
            master.availability_runner.clients["remote"] = object()
        master.availability_reports["remote"] = (1.0 if not disconnected else now, {"conformant": 1.0})
        monkeypatch.setattr(chat_mix_settings_module, "chat_mix_settings", master)
        chat_mix_settings_module._receive_chat_mix_coverage("client", {chat_mix_settings_module.AVAILABILITY_REPORT_KEY: {}})
        assert master.availability_runner.send_message.call_args.args[1] == {}
        assert "remote" not in master.availability_reports

    def test_client_does_not_keep_availability_after_master_stops_broadcasting(self, monkeypatch):
        client = ChatMixSettings()
        client.remote_online_since = {ChatPersonaRole.CONFORMANT: 1.0}
        client.remote_availability_received_at = 1.0
        monkeypatch.setattr(chat_mix_settings_module.time, "time", lambda: 100.0)
        assert not client.is_persona_online(ChatPersonaRole.CONFORMANT)


class TestWorkerNames:
    def test_a_generated_worker_name_never_holds_a_substring_the_horde_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        draws = iter(["t", "p", "u", "x", "y", "z"])
        monkeypatch.setattr(helpers.random, "choices", lambda _letters, k: [next(draws) for _ in range(k)])
        assert _random_worker_name("StressTextWorker-", 3) == "StressTextWorker-xyz"

    def test_the_refused_substrings_match_case_insensitively(self) -> None:
        for refused in ("tpu", "Colab", "GOOGLE"):
            assert _REFUSED_WORKER_NAME_PATTERN.search(f"Stress-{refused}-worker")
