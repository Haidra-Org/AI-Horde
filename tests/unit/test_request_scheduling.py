# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the non-API request scheduling forecast loop."""

from datetime import datetime, timedelta

from horde import request_scheduling


def _forecast(now: datetime) -> request_scheduling.SchedulingForecast:
    return request_scheduling.calculate_scheduling_forecast(
        forecasted_at=now,
        request_expires_at=now + timedelta(minutes=20),
        queued_work=300,
        own_work=100,
        queued_jobs=3,
        own_jobs=1,
        average_work_per_second=10,
        eligible_worker_threads=10,
    )


def test_calculate_scheduling_forecast_separates_start_and_completion() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)

    forecast = _forecast(now)

    assert forecast.estimator == "compatible-queue-v1"
    assert forecast.start_p50_seconds == 10
    assert forecast.completion_p50_seconds == 10
    assert forecast.start_p90_seconds == 70
    assert forecast.completion_p90_seconds == 70
    assert forecast.predicted_stall is False


def test_calculate_scheduling_forecast_marks_missing_capacity_as_stall() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)

    forecast = request_scheduling.calculate_scheduling_forecast(
        forecasted_at=now,
        request_expires_at=now + timedelta(minutes=20),
        queued_work=80,
        own_work=80,
        queued_jobs=1,
        own_jobs=1,
        average_work_per_second=0,
        eligible_worker_threads=0,
    )

    assert forecast.predicted_stall is True


def test_stored_forecast_is_paired_with_start_and_completion(
    fake_redis,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    forecast = _forecast(now)
    estimate_calls = []
    stall_calls = []
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_estimate_validation",
        lambda **kwargs: estimate_calls.append(kwargs),
    )
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_stall_validation",
        lambda **kwargs: stall_calls.append(kwargs),
    )

    request_scheduling.store_scheduling_forecast(
        request_id="request-1",
        forecast=forecast,
        request_expires_at=now + timedelta(minutes=20),
        already_started=False,
    )
    later_forecast = request_scheduling.calculate_scheduling_forecast(
        forecasted_at=now + timedelta(seconds=10),
        request_expires_at=now + timedelta(minutes=20),
        queued_work=900,
        own_work=100,
        queued_jobs=9,
        own_jobs=1,
        average_work_per_second=1,
        eligible_worker_threads=1,
    )
    request_scheduling.store_scheduling_forecast(
        request_id="request-1",
        forecast=later_forecast,
        request_expires_at=now + timedelta(minutes=20),
        already_started=False,
    )
    request_scheduling.record_request_start_forecast(
        request_id="request-1",
        gentype="text",
        started_at=now + timedelta(seconds=30),
        request_expires_at=now + timedelta(minutes=20),
    )
    request_scheduling.record_request_completion_forecast(
        request_id="request-1",
        gentype="text",
        completed_at=now + timedelta(seconds=90),
    )
    assert request_scheduling.wait_for_scheduling_forecast_events()

    assert [call["phase"] for call in estimate_calls] == ["start", "completion"]
    assert [call["observed_seconds"] for call in estimate_calls] == [30, 90]
    assert estimate_calls[0]["predicted_p50_seconds"] == forecast.start_p50_seconds
    assert stall_calls == [
        {
            "estimator": "compatible-queue-v1",
            "gentype": "text",
            "predicted_stall": False,
            "expired_without_start": False,
        },
    ]
    state = fake_redis.horde_r.hgetall("request_scheduling_forecast:request-1")
    assert state[b"start_validated"] == b"1"
    assert state[b"completion_validated"] == b"1"
    assert state[b"stall_validated"] == b"1"


def test_expiry_records_stall_ground_truth_and_discards_completion(
    fake_redis,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    forecast = _forecast(now)
    stall_calls = []
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_stall_validation",
        lambda **kwargs: stall_calls.append(kwargs),
    )
    request_scheduling.store_scheduling_forecast(
        request_id="request-2",
        forecast=forecast,
        request_expires_at=now + timedelta(minutes=20),
        already_started=False,
    )

    request_scheduling.record_request_expiry_forecast(
        request_id="request-2",
        gentype="image",
        expired_at=now + timedelta(minutes=20),
        expired_without_start=True,
    )
    assert request_scheduling.wait_for_scheduling_forecast_events()

    assert stall_calls[0]["expired_without_start"] is True
    assert fake_redis.horde_r.hget("request_scheduling_forecast:request-2", "stall_validated") == b"1"


def test_request_that_already_started_only_stores_completion(fake_redis) -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)

    request_scheduling.store_scheduling_forecast(
        request_id="request-3",
        forecast=_forecast(now),
        request_expires_at=now + timedelta(minutes=20),
        already_started=True,
    )
    assert request_scheduling.wait_for_scheduling_forecast_events()

    state = fake_redis.horde_r.hgetall("request_scheduling_forecast:request-3")
    assert b"start_forecast" not in state
    assert state[b"completion_forecast"] is not None


def test_terminal_observation_can_arrive_before_forecast(fake_redis, monkeypatch) -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    estimate_calls = []
    stall_calls = []
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_estimate_validation",
        lambda **kwargs: estimate_calls.append(kwargs),
    )
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_stall_validation",
        lambda **kwargs: stall_calls.append(kwargs),
    )

    request_scheduling.record_request_completion_forecast(
        request_id="request-4",
        gentype="image",
        completed_at=now + timedelta(seconds=90),
    )
    request_scheduling.store_scheduling_forecast(
        request_id="request-4",
        forecast=_forecast(now),
        request_expires_at=now + timedelta(minutes=20),
        already_started=False,
    )
    assert request_scheduling.wait_for_scheduling_forecast_events()

    assert [call["phase"] for call in estimate_calls] == ["completion"]
    assert stall_calls[0]["expired_without_start"] is False


def test_cancellation_excludes_terminal_validation(fake_redis, monkeypatch) -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    estimate_calls = []
    stall_calls = []
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_estimate_validation",
        lambda **kwargs: estimate_calls.append(kwargs),
    )
    monkeypatch.setattr(
        request_scheduling.metrics,
        "record_request_stall_validation",
        lambda **kwargs: stall_calls.append(kwargs),
    )

    request_scheduling.store_scheduling_forecast(
        request_id="request-5",
        forecast=_forecast(now),
        request_expires_at=now + timedelta(minutes=20),
        already_started=False,
    )
    request_scheduling.record_request_cancellation_forecast(
        request_id="request-5",
        cancelled_at=now + timedelta(seconds=20),
    )
    request_scheduling.record_request_completion_forecast(
        request_id="request-5",
        gentype="text",
        completed_at=now + timedelta(seconds=30),
    )
    assert request_scheduling.wait_for_scheduling_forecast_events()

    assert estimate_calls == []
    assert stall_calls == []
    state = fake_redis.horde_r.hgetall("request_scheduling_forecast:request-5")
    assert b"cancelled_at" in state
    assert b"start_forecast" not in state
    assert b"completion_forecast" not in state


def test_backend_failure_isolated_from_caller(fake_redis, monkeypatch) -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    dropped_reasons = []

    class UnavailableRedis:
        def hexists(self, *_args) -> bool:
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(request_scheduling.hr, "horde_r", UnavailableRedis())
    monkeypatch.setattr(
        request_scheduling,
        "_record_dropped_event",
        dropped_reasons.append,
    )

    request_scheduling.store_scheduling_forecast(
        request_id="request-6",
        forecast=_forecast(now),
        request_expires_at=now + timedelta(minutes=20),
        already_started=False,
    )

    assert request_scheduling.wait_for_scheduling_forecast_events()
    assert dropped_reasons == ["processing_error"]
