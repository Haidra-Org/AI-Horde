# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat scale demo's simulated worker in its progress mode: the delta split, the posting cadence and how each
progress status ends or continues a job's posting."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from locust.env import Environment

from tests.stress.locustsuite.chat_wire import TEXT_POP_PATH, TEXT_PROGRESS_PATH, TEXT_SUBMIT_PATH
from tests.stress.locustsuite.users import chat as chat_users
from tests.stress.locustsuite.users.chat import (
    WORKER_GENERATION_TEXT,
    ChatWorkerSimulator,
    chat_settings,
    count_progress_deltas,
    progress_request_name,
    split_reply_into_deltas,
)

GENERATION_ID: str = "00000000-0000-0000-0000-000000000001"
"""The generation id every popped test job carries."""


def scenario_options(**overrides: Any) -> SimpleNamespace:
    """Return parsed scale demo options with a fixed three-second generation, with ``overrides`` by attribute name."""
    defaults: dict[str, Any] = {
        "chat_front_host": "http://front.test",
        "chat_horde_urls": "http://horde.test",
        "chat_model": "stress/chat",
        "chat_max_tokens": 64,
        "chat_no_include_usage": False,
        "chat_impatient_timeout": 2.0,
        "chat_impatient_stream_chance": 0.5,
        "chat_nonstream_timeout": 300.0,
        "sim_gen_time_min": 3.0,
        "sim_gen_time_max": 3.0,
        "chat_unreported_usage_chance": 0.0,
        "chat_worker_progress": True,
        "chat_worker_progress_interval": 1.0,
    }
    return SimpleNamespace(**{**defaults, **overrides})


class FakeResponse:
    """A stand-in for Locust's response context manager, recording how the user marked it and the name it set."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.ok = 0 < status_code < 400
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.error = "connection reset" if status_code == 0 else None
        self.elapsed = timedelta(0)
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
    """A stand-in for Locust's HTTP session, answering posts from a queue and keeping each path and body sent."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.base_url = ""
        self.responses = responses
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, *, json: dict[str, Any], **_options: Any) -> FakeResponse:
        self.sent.append((path, json))
        return self.responses.pop(0)

    def bodies_sent_to(self, path: str) -> list[dict[str, Any]]:
        return [body for sent_path, body in self.sent if sent_path == path]


def popped_chat_job(*, stream_progress: bool | None) -> FakeResponse:
    """Return a pop answer assigning one chat job, with ``stream_progress`` left out when None."""
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": "Hi"}], "max_length": 64}
    if stream_progress is not None:
        payload["stream_progress"] = stream_progress
    return FakeResponse(200, {"id": GENERATION_ID, "chat": True, "model": "stress/chat", "payload": payload})


def accepted(seq: int) -> FakeResponse:
    return FakeResponse(200, {"accepted_seq": seq})


def submitted() -> FakeResponse:
    return FakeResponse(200, {"reward": 1})


class TestDeltaSplit:
    def test_the_deltas_concatenate_to_the_reply_in_near_equal_pieces(self) -> None:
        deltas = split_reply_into_deltas(WORKER_GENERATION_TEXT, 5)

        assert "".join(deltas) == WORKER_GENERATION_TEXT
        assert len(deltas) == 5
        assert max(map(len, deltas)) - min(map(len, deltas)) <= 1

    def test_a_reply_shorter_than_the_count_gives_one_delta_per_character(self) -> None:
        assert split_reply_into_deltas("abc", 10) == ["a", "b", "c"]

    def test_a_count_below_one_gives_the_whole_reply(self) -> None:
        assert split_reply_into_deltas("abc", 0) == ["abc"]

    def test_an_empty_reply_is_refused(self) -> None:
        with pytest.raises(ValueError):
            split_reply_into_deltas("", 3)

    def test_a_generation_posts_one_delta_per_whole_interval_and_at_least_one(self) -> None:
        assert count_progress_deltas(generation_seconds=3.7, interval_seconds=1.0) == 3
        assert count_progress_deltas(generation_seconds=0.4, interval_seconds=1.0) == 1
        assert count_progress_deltas(generation_seconds=3.0, interval_seconds=0.5) == 6


class TestProgressMode:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chat_users.time, "sleep", lambda _seconds: None)

    def build_worker(self, responses: list[FakeResponse], **overrides: Any) -> ChatWorkerSimulator:
        chat_settings.configure(scenario_options(**overrides), fallback_horde_url="http://horde.test")
        hosted_worker_class = type("HostedChatWorkerSimulator", (ChatWorkerSimulator,), {"host": "http://horde.test"})
        worker = hosted_worker_class(Environment(user_classes=[hosted_worker_class]))
        worker.client = FakeClient(responses)
        worker.on_start()
        return worker

    def test_a_streamed_job_posts_the_reply_once_a_second_and_submits_it_after(self) -> None:
        progress_answers = [accepted(1), accepted(2), accepted(3)]
        worker = self.build_worker([popped_chat_job(stream_progress=True), *progress_answers, submitted()])

        worker.pop_and_submit_chat()

        progress_bodies = worker.client.bodies_sent_to(TEXT_PROGRESS_PATH)
        assert [body["seq"] for body in progress_bodies] == [1, 2, 3]
        assert all(body["id"] == GENERATION_ID for body in progress_bodies)
        assert "".join(body["delta"]["content"] for body in progress_bodies) == WORKER_GENERATION_TEXT
        (submit_body,) = worker.client.bodies_sent_to(TEXT_SUBMIT_PATH)
        assert submit_body["message"]["content"].startswith(WORKER_GENERATION_TEXT)
        assert [answer.request_meta["name"] for answer in progress_answers] == [progress_request_name(200)] * 3
        assert all(answer.marked == "success" for answer in progress_answers)
        assert worker.client.responses == []

    def test_the_interval_sets_the_delta_count(self) -> None:
        progress_answers = [accepted(seq) for seq in range(1, 7)]
        worker = self.build_worker(
            [popped_chat_job(stream_progress=True), *progress_answers, submitted()], chat_worker_progress_interval=0.5
        )

        worker.pop_and_submit_chat()

        assert len(worker.client.bodies_sent_to(TEXT_PROGRESS_PATH)) == 6

    @pytest.mark.parametrize("stop_status", [404, 413])
    def test_a_job_without_a_lease_or_at_its_cap_stops_posting_and_still_submits(self, stop_status: int) -> None:
        refusal = FakeResponse(stop_status, {"message": "stop", "rc": "ChatProgressNotStreaming"})
        worker = self.build_worker([popped_chat_job(stream_progress=True), refusal, submitted()])

        worker.pop_and_submit_chat()

        assert len(worker.client.bodies_sent_to(TEXT_PROGRESS_PATH)) == 1
        assert len(worker.client.bodies_sent_to(TEXT_SUBMIT_PATH)) == 1
        assert refusal.request_meta["name"] == f"{TEXT_PROGRESS_PATH} [{stop_status}]"
        assert refusal.marked == "success"

    def test_a_cancelled_acknowledgement_stops_posting_and_skips_the_submit(self) -> None:
        cancelled_answer = FakeResponse(200, {"accepted_seq": 2, "cancelled": True})
        worker = self.build_worker([popped_chat_job(stream_progress=True), accepted(1), cancelled_answer])

        worker.pop_and_submit_chat()

        assert len(worker.client.bodies_sent_to(TEXT_PROGRESS_PATH)) == 2
        assert worker.client.bodies_sent_to(TEXT_SUBMIT_PATH) == []
        assert cancelled_answer.marked == "success"
        assert worker.client.responses == []

    def test_an_out_of_sequence_refusal_stops_posting_and_fails_the_post(self) -> None:
        refusal = FakeResponse(409, {"message": "out of sequence", "rc": "ChatProgressOutOfSequence", "expected_seq": 3})
        worker = self.build_worker([popped_chat_job(stream_progress=True), accepted(1), refusal, submitted()])

        worker.pop_and_submit_chat()

        assert len(worker.client.bodies_sent_to(TEXT_PROGRESS_PATH)) == 2
        assert len(worker.client.bodies_sent_to(TEXT_SUBMIT_PATH)) == 1
        assert refusal.marked is not None and refusal.marked != "success"

    @pytest.mark.parametrize("retry_status", [0, 429, 503])
    def test_an_unaccepted_post_is_resent_unchanged_and_the_next_carries_the_rest(self, retry_status: int) -> None:
        unaccepted = FakeResponse(retry_status, {"message": "later"})
        worker = self.build_worker([popped_chat_job(stream_progress=True), unaccepted, accepted(1), accepted(2), submitted()])

        worker.pop_and_submit_chat()

        first, resent, last = worker.client.bodies_sent_to(TEXT_PROGRESS_PATH)
        assert resent == first
        assert last["seq"] == 2
        assert first["delta"]["content"] + last["delta"]["content"] == WORKER_GENERATION_TEXT
        assert unaccepted.request_meta["name"] == progress_request_name(retry_status)

    def test_the_mode_off_posts_no_progress(self) -> None:
        worker = self.build_worker([popped_chat_job(stream_progress=True), submitted()], chat_worker_progress=False)

        worker.pop_and_submit_chat()

        assert [path for path, _body in worker.client.sent] == [TEXT_POP_PATH, TEXT_SUBMIT_PATH]

    @pytest.mark.parametrize("stream_progress", [None, False])
    def test_a_job_whose_client_does_not_stream_posts_no_progress(self, stream_progress: bool | None) -> None:
        worker = self.build_worker([popped_chat_job(stream_progress=stream_progress), submitted()])

        worker.pop_and_submit_chat()

        assert [path for path, _body in worker.client.sent] == [TEXT_POP_PATH, TEXT_SUBMIT_PATH]

    def test_a_non_positive_interval_is_refused(self) -> None:
        with pytest.raises(ValueError):
            chat_settings.configure(scenario_options(chat_worker_progress_interval=0), fallback_horde_url="http://horde.test")
