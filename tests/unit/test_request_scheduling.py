# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the non-API request scheduling forecast loop."""

from datetime import datetime, timedelta

from horde import request_scheduling

_CURRENT_WORKER_STATE = request_scheduling.build_worker_scheduling_state(
    gentype="image",
    model_names=("model",),
    bridge_agent="AI Horde Worker reGen:17:unknown",
)


def _dispatch(
    now: datetime,
    dispatch_number: int,
    *,
    created_at: datetime,
    extra_priority: int = 1,
    worker_state: request_scheduling.WorkerSchedulingState = _CURRENT_WORKER_STATE,
    selected_from_priority_queue: bool = False,
    priority_user_ids: tuple[str, ...] = (),
    dispatched_at: datetime | None = None,
    assigned_work: float = 1,
) -> request_scheduling.DispatchObservation:
    return request_scheduling.DispatchObservation(
        dispatch_id=f"dispatch-{dispatch_number}",
        worker_id="worker-1",
        worker_state=worker_state,
        request_id=f"request-{dispatch_number}",
        request_created_at=created_at,
        request_extra_priority=extra_priority,
        selected_from_priority_queue=selected_from_priority_queue,
        priority_user_ids=priority_user_ids,
        dispatched_at=dispatched_at or now - timedelta(seconds=150 - dispatch_number),
        assigned_work=assigned_work,
    )


def _return(
    now: datetime,
    dispatch_number: int,
    *,
    seconds_ago: int,
) -> request_scheduling.CapacityReturn:
    return request_scheduling.CapacityReturn(
        worker_id="worker-1",
        dispatch_id=f"dispatch-{dispatch_number}",
        returned_at=now - timedelta(seconds=seconds_ago),
    )


def _pressure(
    now: datetime,
    observations: list[request_scheduling.DispatchObservation],
    capacity_returns: list[request_scheduling.CapacityReturn],
    arrivals: tuple[request_scheduling.PrecedingArrival, ...] = (),
    active_ids: tuple[str, ...] = ("dispatch-3",),
) -> request_scheduling.AssignmentPressure:
    return request_scheduling.calculate_assignment_pressure(
        observed_at=now,
        target_request_id="target",
        target_user_id="target-user",
        target_created_at=now - timedelta(minutes=4),
        target_extra_priority=0,
        eligible_worker_states={"worker-1": _CURRENT_WORKER_STATE},
        eligible_worker_threads=1,
        active_dispatch_ids=active_ids,
        preceding_arrivals=arrivals,
        dispatch_observations=observations,
        capacity_returns=capacity_returns,
    )


def test_sustained_preceding_arrivals_outpacing_returns_might_stall() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    observations = [
        _dispatch(now, 1, created_at=now - timedelta(minutes=3), dispatched_at=now - timedelta(seconds=190)),
        _dispatch(now, 2, created_at=now - timedelta(seconds=90), dispatched_at=now - timedelta(seconds=80)),
        _dispatch(now, 3, created_at=now - timedelta(seconds=30), dispatched_at=now - timedelta(seconds=20)),
    ]
    arrivals = (
        request_scheduling.PrecedingArrival("request-1", now - timedelta(minutes=3), 2),
        request_scheduling.PrecedingArrival("request-2", now - timedelta(seconds=90), 2),
    )

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
        arrivals,
    )

    assert pressure.evidence == "arrival_outpaces_drain"
    assert pressure.returned_capacity == 2
    assert pressure.arriving_preceding_work == 7
    assert pressure.returned_work == 2
    assert pressure.active_preceding_dispatches == 1
    assert pressure.might_stall is True


def test_finite_preexisting_backlog_never_counts_as_new_arrival_pressure() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    created_at = now - timedelta(minutes=5)
    observations = [_dispatch(now, index, created_at=created_at) for index in range(1, 4)]

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
    )

    assert pressure.evidence == "preceding_arrivals_not_outpacing_drain"
    assert pressure.might_stall is False


def test_one_finite_arrival_burst_does_not_show_sustained_rate_pressure() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    observations = [_dispatch(now, index, created_at=now - timedelta(minutes=3)) for index in range(1, 4)]
    arrivals = (request_scheduling.PrecedingArrival("burst", now - timedelta(minutes=3), 20),)

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
        arrivals,
    )

    assert pressure.evidence == "preceding_arrivals_not_outpacing_drain"
    assert pressure.might_stall is False


def test_arrivals_matching_clearance_do_not_might_stall() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    observations = [
        _dispatch(
            now,
            1,
            created_at=now - timedelta(minutes=3),
            dispatched_at=now - timedelta(seconds=190),
        ),
        _dispatch(
            now,
            2,
            created_at=now - timedelta(seconds=90),
            dispatched_at=now - timedelta(seconds=80),
        ),
        _dispatch(
            now,
            3,
            created_at=now - timedelta(minutes=5),
            dispatched_at=now - timedelta(seconds=20),
        ),
    ]

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
    )

    assert pressure.evidence == "preceding_arrivals_not_outpacing_drain"
    assert pressure.might_stall is False


def test_non_preceding_pop_breaks_the_stall_evidence() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    older = now - timedelta(minutes=5)
    newer = now - timedelta(minutes=1)
    observations = [
        _dispatch(now, 1, created_at=older),
        _dispatch(now, 2, created_at=older),
        _dispatch(now, 3, created_at=older),
        _dispatch(now, 4, created_at=newer, extra_priority=-1),
    ]

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
        (
            request_scheduling.PrecedingArrival("arrival-1", now - timedelta(minutes=3), 2),
            request_scheduling.PrecedingArrival("arrival-2", now - timedelta(minutes=1), 2),
        ),
    )

    assert pressure.evidence == "target_opportunity_seen"
    assert pressure.might_stall is False


def test_history_from_an_old_worker_state_is_ignored() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    created_at = now - timedelta(minutes=5)
    old_worker_state = request_scheduling.build_worker_scheduling_state(
        gentype="image",
        model_names=("old-model",),
        bridge_agent="AI Horde Worker reGen:17:unknown",
    )
    observations = [_dispatch(now, index, created_at=created_at, worker_state=old_worker_state) for index in range(1, 4)]

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
    )

    assert pressure.lost_opportunities == 0
    assert pressure.might_stall is False


def test_worker_state_compares_effective_bridge_capabilities() -> None:
    equivalent_state = request_scheduling.build_worker_scheduling_state(
        gentype="image",
        model_names=("model",),
        bridge_agent="AI Horde Worker reGen:18:other-build",
    )
    older_state = request_scheduling.build_worker_scheduling_state(
        gentype="image",
        model_names=("model",),
        bridge_agent="AI Horde Worker reGen:16:unknown",
    )

    assert equivalent_state == _CURRENT_WORKER_STATE
    assert older_state != _CURRENT_WORKER_STATE


def test_worker_priority_pass_precedes_a_target_outside_that_pass() -> None:
    now = datetime(2026, 8, 22, 12, 0, 0)
    observations = []
    for index, seconds_ago in enumerate((210, 170, 80, 20), start=1):
        observations.append(
            _dispatch(
                now,
                index,
                created_at=now - timedelta(seconds=seconds_ago + 10),
                extra_priority=-1,
                selected_from_priority_queue=True,
                priority_user_ids=("other-user",),
                dispatched_at=now - timedelta(seconds=seconds_ago),
            ),
        )

    pressure = _pressure(
        now,
        observations,
        [_return(now, 1, seconds_ago=180), _return(now, 2, seconds_ago=60)],
        active_ids=("dispatch-4",),
    )

    assert pressure.might_stall is True


def test_dispatch_and_capacity_return_observations_round_trip_through_redis(fake_redis, monkeypatch) -> None:
    now = datetime.utcnow()
    observations = [
        _dispatch(now, 1, created_at=now - timedelta(minutes=3), dispatched_at=now - timedelta(seconds=190)),
        _dispatch(now, 2, created_at=now - timedelta(seconds=90), dispatched_at=now - timedelta(seconds=80)),
        _dispatch(now, 3, created_at=now - timedelta(seconds=30), dispatched_at=now - timedelta(seconds=20)),
    ]
    for observation in observations:
        request_scheduling.record_worker_dispatch(observation)
    for dispatch_id, seconds_ago in (("dispatch-1", 180), ("dispatch-2", 60)):
        request_scheduling.record_capacity_return(
            request_scheduling.CapacityReturn(
                worker_id="worker-1",
                dispatch_id=dispatch_id,
                returned_at=now - timedelta(seconds=seconds_ago),
            ),
        )
    assert request_scheduling.wait_for_scheduling_forecast_events()

    redis_backend = fake_redis.horde_r
    pipeline_calls = 0
    original_pipeline = redis_backend.pipeline

    def counted_pipeline(*args, **kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        return original_pipeline(*args, **kwargs)

    monkeypatch.setattr(redis_backend, "pipeline", counted_pipeline)

    pressure = request_scheduling.get_request_assignment_pressure(
        observed_at=now,
        target_request_id="target",
        target_user_id="target-user",
        target_created_at=now - timedelta(minutes=4),
        target_extra_priority=0,
        eligible_worker_states={"worker-1": _CURRENT_WORKER_STATE},
        eligible_worker_threads=1,
        active_dispatch_ids=["dispatch-3"],
        preceding_arrivals=(
            request_scheduling.PrecedingArrival("request-1", now - timedelta(minutes=3), 2),
            request_scheduling.PrecedingArrival("request-2", now - timedelta(seconds=90), 2),
        ),
        redis_backend=redis_backend,
    )

    assert pressure.might_stall is True
    assert pipeline_calls == 1


def test_assignment_pressure_loads_history_for_more_than_one_hundred_workers(fake_redis, monkeypatch) -> None:
    redis_backend = fake_redis.horde_r
    pipeline_calls = 0
    original_pipeline = redis_backend.pipeline

    def counted_pipeline(*args, **kwargs):
        nonlocal pipeline_calls
        pipeline_calls += 1
        return original_pipeline(*args, **kwargs)

    monkeypatch.setattr(redis_backend, "pipeline", counted_pipeline)
    pressure = request_scheduling.get_request_assignment_pressure(
        observed_at=datetime(2026, 8, 22, 12, 0, 0),
        target_request_id="target",
        target_user_id="target-user",
        target_created_at=datetime(2026, 8, 22, 11, 55, 0),
        target_extra_priority=0,
        eligible_worker_states={f"worker-{index}": _CURRENT_WORKER_STATE for index in range(101)},
        eligible_worker_threads=101,
        active_dispatch_ids=(),
        preceding_arrivals=(),
        redis_backend=redis_backend,
    )

    assert pressure.evidence == "insufficient_replacement"
    assert pressure.might_stall is False
    assert pipeline_calls == 1


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
