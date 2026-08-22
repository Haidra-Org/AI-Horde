# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shadow forecasts and observations for request scheduling."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from horde import metrics
from horde.bridge_reference import get_bridge_capabilities, get_supported_samplers, is_backed_validated
from horde.horde_redis import horde_redis as hr
from horde.logger import logger  # type: ignore[attr-defined]

SHADOW_ESTIMATOR = "compatible-queue-v1"
P90_MINIMUM_MARGIN_SECONDS = 60
P90_RELATIVE_MARGIN = 0.5
FORECAST_EXPIRY_GRACE = timedelta(minutes=10)
SCHEDULING_EVENT_QUEUE_SIZE = 2048
ASSIGNMENT_PRESSURE_WINDOW = timedelta(minutes=5)
DISPATCH_OBSERVATION_RETENTION = timedelta(minutes=10)
ASSIGNMENT_PRESSURE_MINIMUM_SECONDS = 120

_START_FORECAST_FIELD = "start_forecast"
_COMPLETION_FORECAST_FIELD = "completion_forecast"
_STARTED_AT_FIELD = "started_at"
_COMPLETED_AT_FIELD = "completed_at"
_EXPIRED_WITHOUT_START_FIELD = "expired_without_start"
_CANCELLED_AT_FIELD = "cancelled_at"
_GENTYPE_FIELD = "gentype"
_START_VALIDATED_FIELD = "start_validated"
_COMPLETION_VALIDATED_FIELD = "completion_validated"
_STALL_VALIDATED_FIELD = "stall_validated"


@dataclass(frozen=True)
class SchedulingForecast:
    """Represent one shadow forecast for request start and completion."""

    estimator: str
    forecasted_at: datetime
    start_p50_seconds: float
    start_p90_seconds: float
    completion_p50_seconds: float
    completion_p90_seconds: float
    predicted_stall: bool


@dataclass(frozen=True)
class BridgeSchedulingCapabilities:
    """Represent bridge behavior that affects request eligibility.

    Attributes:
        feature_names: Feature gates supported by the bridge version.
        sampler_names: Samplers supported when Karras scheduling is not
            required; this includes samplers that also support Karras.
        karras_sampler_names: Samplers supported with Karras scheduling.
        validated_backend: Whether text requests requiring a validated backend
            may be dispatched to this bridge.
    """

    feature_names: tuple[str, ...]
    sampler_names: tuple[str, ...]
    karras_sampler_names: tuple[str, ...]
    validated_backend: bool


@dataclass(frozen=True)
class WorkerSchedulingState:
    """Represent the worker capabilities used to compare recent pop history.

    Attributes:
        gentype: Request family served by the worker, such as image or text.
        model_names: Sorted models advertised for this worker state.
        bridge_capabilities: Bridge behavior that governed dispatch.
        softprompt_names: Sorted softprompts advertised by a text worker.
    """

    gentype: str
    model_names: tuple[str, ...]
    bridge_capabilities: BridgeSchedulingCapabilities
    softprompt_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class EligibleWorkerState:
    """Represent a worker currently eligible for a particular request.

    Attributes:
        worker_id: Stable worker identifier used to find recent pop history.
        scheduling_state: Models, bridge capabilities, and text softprompts that make
            recent observations comparable with the worker's current state.
    """

    worker_id: str
    scheduling_state: WorkerSchedulingState


@dataclass(frozen=True)
class ActiveWorkerDispatch:
    """Represent one worker-pop batch that currently occupies a thread.

    Attributes:
        worker_id: Worker currently processing the batch.
        dispatch_id: Shared identity of the processing-generation batch.
    """

    worker_id: str
    dispatch_id: str


@dataclass(frozen=True)
class DispatchObservation:
    """Represent one successful worker pop on a capability state.

    The request ordering fields preserve the facts needed to compare this pop
    with any target request during the bounded observation window. One record
    represents one occupied worker thread even when the pop returns a batch.

    Attributes:
        dispatch_id: Shared identity for every generation returned by the pop.
        worker_id: Worker that accepted the request.
        worker_state: Scheduling capabilities advertised during the pop.
        request_id: Request selected by the worker.
        request_created_at: Creation time used by normal queue ordering.
        request_extra_priority: Kudos-derived normal queue priority.
        selected_from_priority_queue: Whether selection occurred in the
            owner/bridge-priority pass.
        priority_user_ids: Users included in that priority pass.
        dispatched_at: Time the pop occupied a worker thread.
        assigned_work: Normalized work assigned in this worker-pop batch.
    """

    dispatch_id: str
    worker_id: str
    worker_state: WorkerSchedulingState
    request_id: str
    request_created_at: datetime
    request_extra_priority: int
    selected_from_priority_queue: bool
    priority_user_ids: tuple[str, ...]
    dispatched_at: datetime
    assigned_work: float


@dataclass(frozen=True)
class PrecedingArrival:
    """Represent new compatible demand that entered ahead of a target.

    Attributes:
        request_id: Stable request identity used to avoid double-counting.
        arrived_at: Time the request entered the queue.
        work_amount: Normalized demand from this arrival still queued now.
    """

    request_id: str
    arrived_at: datetime
    work_amount: float


@dataclass(frozen=True)
class CapacityReturn:
    """Represent one worker-pop batch returning an eligible thread.

    Attributes:
        worker_id: Worker whose thread became available.
        dispatch_id: Identity joining the return to its assigned work.
        returned_at: Time the complete pop batch became terminal.
    """

    worker_id: str
    dispatch_id: str
    returned_at: datetime


@dataclass(frozen=True)
class AssignmentPressure:
    """Represent the bounded evidence behind a request stall advisory.

    Attributes:
        evidence: Stable reason describing why the signal fired or failed clear.
        lost_opportunities: Compatible pops assigned to preceding requests.
        returned_capacity: Eligible pop batches that returned capacity.
        active_preceding_dispatches: Preceding pops still occupying worker threads.
        might_stall: Whether preceding arrivals persistently outpace clearance.
        arriving_preceding_work: Compatible work arriving ahead during the window.
        returned_work: Work completed by eligible capacity during the window.
    """

    evidence: str
    lost_opportunities: int
    returned_capacity: int
    active_preceding_dispatches: int
    might_stall: bool
    arriving_preceding_work: float = 0
    returned_work: float = 0


@dataclass(frozen=True)
class _StoreForecastEvent:
    request_id: str
    forecast: SchedulingForecast
    request_expires_at: datetime
    already_started: bool


@dataclass(frozen=True)
class _StartObservedEvent:
    request_id: str
    gentype: str
    started_at: datetime
    request_expires_at: datetime


@dataclass(frozen=True)
class _CompletionObservedEvent:
    request_id: str
    gentype: str
    completed_at: datetime


@dataclass(frozen=True)
class _ExpiryObservedEvent:
    request_id: str
    gentype: str
    expired_at: datetime
    expired_without_start: bool


@dataclass(frozen=True)
class _CancellationObservedEvent:
    request_id: str
    cancelled_at: datetime


@dataclass(frozen=True)
class _DispatchObservedEvent:
    observation: DispatchObservation


@dataclass(frozen=True)
class _CapacityReturnedEvent:
    observation: CapacityReturn


type _SchedulingEvent = (
    _StoreForecastEvent
    | _StartObservedEvent
    | _CompletionObservedEvent
    | _ExpiryObservedEvent
    | _CancellationObservedEvent
    | _DispatchObservedEvent
    | _CapacityReturnedEvent
)

_scheduling_event_queue: queue.Queue[_SchedulingEvent] = queue.Queue(maxsize=SCHEDULING_EVENT_QUEUE_SIZE)
_scheduling_worker_lock = threading.Lock()
_scheduling_worker: threading.Thread | None = None


def build_worker_scheduling_state(
    *,
    gentype: str,
    model_names: Sequence[str],
    bridge_agent: str,
    softprompt_names: Sequence[str] = (),
) -> WorkerSchedulingState:
    """Return the normalized worker state used for recent-history comparison.

    The live eligibility check remains authoritative. This state only prevents
    reuse of observations after a worker changes generation type, models,
    effective bridge capabilities, or text softprompts.

    Args:
        gentype: Request family served by the worker.
        model_names: Models advertised by the worker.
        bridge_agent: Bridge identity used to resolve effective dispatch gates.
        softprompt_names: Softprompts advertised by a text worker.

    Returns:
        A normalized immutable scheduling state.
    """

    bridge_capabilities = BridgeSchedulingCapabilities(
        feature_names=tuple(sorted(get_bridge_capabilities(bridge_agent))) if gentype == "image" else (),
        sampler_names=tuple(sorted(get_supported_samplers(bridge_agent, karras=False))) if gentype == "image" else (),
        karras_sampler_names=(tuple(sorted(get_supported_samplers(bridge_agent, karras=True))) if gentype == "image" else ()),
        validated_backend=is_backed_validated(bridge_agent) if gentype == "text" else False,
    )
    return WorkerSchedulingState(
        gentype=gentype,
        model_names=tuple(sorted(model_names)),
        bridge_capabilities=bridge_capabilities,
        softprompt_names=tuple(sorted(softprompt_names)),
    )


def parse_worker_scheduling_state(payload: Mapping[str, Any]) -> WorkerSchedulingState:
    """Parse a worker scheduling state from its JSON-compatible representation.

    Args:
        payload: Mapping produced from a serialized ``WorkerSchedulingState``.

    Returns:
        Parsed immutable scheduling state.

    Raises:
        KeyError: If a required field is absent.
        TypeError: If the validated-backend field is not a Boolean.
    """

    bridge_capabilities = payload["bridge_capabilities"]
    validated_backend = bridge_capabilities["validated_backend"]
    if not isinstance(validated_backend, bool):
        raise TypeError("validated_backend must be a boolean")
    return WorkerSchedulingState(
        gentype=str(payload["gentype"]),
        model_names=tuple(str(model_name) for model_name in payload["model_names"]),
        bridge_capabilities=BridgeSchedulingCapabilities(
            feature_names=tuple(str(name) for name in bridge_capabilities["feature_names"]),
            sampler_names=tuple(str(name) for name in bridge_capabilities["sampler_names"]),
            karras_sampler_names=tuple(str(name) for name in bridge_capabilities["karras_sampler_names"]),
            validated_backend=validated_backend,
        ),
        softprompt_names=tuple(str(name) for name in payload["softprompt_names"]),
    )


def request_precedes_target(
    observation: DispatchObservation,
    *,
    target_request_id: str,
    target_user_id: str,
    target_created_at: datetime,
    target_extra_priority: int,
) -> bool:
    """Return whether the scheduler placed an observed request first.

    Args:
        observation: Successful pop being compared with the target.
        target_request_id: Queued request whose opportunities are measured.
        target_user_id: Owner of the target request.
        target_created_at: Target creation time used by normal ordering.
        target_extra_priority: Target's Kudos-derived normal queue priority.

    Returns:
        True when the priority pass or normal ordering selects the observation.
    """

    if observation.request_id == target_request_id:
        return False
    target_had_priority = target_user_id in observation.priority_user_ids
    if observation.selected_from_priority_queue and not target_had_priority:
        return True
    if observation.request_extra_priority != target_extra_priority:
        return observation.request_extra_priority > target_extra_priority
    return observation.request_created_at < target_created_at


def calculate_assignment_pressure(
    *,
    observed_at: datetime,
    target_request_id: str,
    target_user_id: str,
    target_created_at: datetime,
    target_extra_priority: int,
    eligible_worker_states: Mapping[str, WorkerSchedulingState],
    eligible_worker_threads: int,
    active_dispatch_ids: Sequence[str],
    preceding_arrivals: Sequence[PrecedingArrival],
    dispatch_observations: Sequence[DispatchObservation],
    capacity_returns: Sequence[CapacityReturn],
) -> AssignmentPressure:
    """Return a conservative arrival-versus-drain stall advisory.

    The observation window is split in half. Each half must contain a complete
    replacement wave and more newly arrived, scheduler-preceding compatible
    work than eligible workers completed. A finite backlog or one finite burst
    therefore cannot satisfy the signal. Missing evidence always fails clear.

    Args:
        observed_at: End of the bounded observation window.
        target_request_id: Queued request whose opportunities are measured.
        target_user_id: Owner of the target request.
        target_created_at: Target creation time used by normal ordering.
        target_extra_priority: Target's Kudos-derived normal queue priority.
        eligible_worker_states: Current scheduling state by eligible worker ID.
        eligible_worker_threads: Advertised threads across eligible workers.
        active_dispatch_ids: Pop batches currently occupying those workers.
        preceding_arrivals: Recent requests independently verified to precede
            the target and be compatible with at least one eligible worker.
        dispatch_observations: Recent successful worker pops.
        capacity_returns: Pop batches whose complete work returned capacity.

    Returns:
        Bounded evidence and the resulting stall advisory.
    """

    if eligible_worker_threads < 1 or not eligible_worker_states:
        return AssignmentPressure("none_eligible", 0, 0, 0, False)

    window_start = max(target_created_at, observed_at - ASSIGNMENT_PRESSURE_WINDOW)
    midpoint = window_start + (observed_at - window_start) / 2
    relevant_dispatches = [
        observation
        for observation in dispatch_observations
        if window_start <= observation.dispatched_at <= observed_at
        and eligible_worker_states.get(observation.worker_id) == observation.worker_state
        and observation.request_id != target_request_id
    ]
    preceding_dispatches = [
        observation
        for observation in relevant_dispatches
        if request_precedes_target(
            observation,
            target_request_id=target_request_id,
            target_user_id=target_user_id,
            target_created_at=target_created_at,
            target_extra_priority=target_extra_priority,
        )
    ]
    dispatches_by_id = {observation.dispatch_id: observation for observation in dispatch_observations}
    relevant_returns = [
        capacity_return
        for capacity_return in capacity_returns
        if capacity_return.worker_id in eligible_worker_states and window_start <= capacity_return.returned_at <= observed_at
    ]
    matched_returns = [
        (capacity_return, dispatches_by_id[capacity_return.dispatch_id])
        for capacity_return in relevant_returns
        if capacity_return.dispatch_id in dispatches_by_id
    ]
    has_incomplete_return_history = len(matched_returns) != len(relevant_returns)

    interval_arriving_work = [0.0, 0.0]
    for arrival in preceding_arrivals:
        if not window_start <= arrival.arrived_at <= observed_at or arrival.work_amount < 0:
            continue
        interval = 0 if arrival.arrived_at < midpoint else 1
        interval_arriving_work[interval] += arrival.work_amount
    # Assigned batches preserve demand that has already left the current queue
    # snapshot. Actual priority-pass dispatches also supply precedence evidence
    # when the bridge's transient priority list was not otherwise observable.
    for observation in preceding_dispatches:
        if observation.request_created_at <= target_created_at:
            continue
        interval = 0 if observation.request_created_at < midpoint else 1
        interval_arriving_work[interval] += observation.assigned_work
    interval_returned_work = [0.0, 0.0]
    interval_returns = [0, 0]
    for capacity_return, dispatch in matched_returns:
        interval = 0 if capacity_return.returned_at < midpoint else 1
        interval_returned_work[interval] += dispatch.assigned_work
        interval_returns[interval] += 1

    active_ids = set(active_dispatch_ids)
    active_preceding = sum(observation.dispatch_id in active_ids for observation in preceding_dispatches)
    required_returns_per_interval = max(eligible_worker_threads, 1)
    every_observed_opportunity_was_lost = bool(relevant_dispatches) and len(preceding_dispatches) == len(
        relevant_dispatches,
    )
    arrivals_outpace_drain = all(
        arriving_work > returned_work for arriving_work, returned_work in zip(interval_arriving_work, interval_returned_work, strict=True)
    )

    if (observed_at - window_start).total_seconds() < ASSIGNMENT_PRESSURE_MINIMUM_SECONDS:
        evidence = "insufficient_window"
    elif has_incomplete_return_history:
        evidence = "incomplete_return_history"
    elif any(return_count < required_returns_per_interval for return_count in interval_returns):
        evidence = "insufficient_replacement"
    elif not every_observed_opportunity_was_lost:
        evidence = "target_opportunity_seen"
    elif active_preceding < eligible_worker_threads:
        evidence = "compatible_capacity_returning"
    elif not arrivals_outpace_drain:
        evidence = "preceding_arrivals_not_outpacing_drain"
    else:
        evidence = "arrival_outpaces_drain"

    return AssignmentPressure(
        evidence=evidence,
        lost_opportunities=len(preceding_dispatches),
        returned_capacity=len(matched_returns),
        active_preceding_dispatches=active_preceding,
        might_stall=evidence == "arrival_outpaces_drain",
        arriving_preceding_work=sum(interval_arriving_work),
        returned_work=sum(interval_returned_work),
    )


def calculate_scheduling_forecast(
    *,
    forecasted_at: datetime,
    request_expires_at: datetime,
    queued_work: float,
    own_work: float,
    queued_jobs: int,
    own_jobs: int,
    average_work_per_second: float,
    eligible_worker_threads: int,
) -> SchedulingForecast:
    """Return a compatible-capacity queue forecast for a waiting request.

    Args:
        forecasted_at: Time at which the queue snapshot was observed.
        request_expires_at: Request expiry used by the shadow stall signal.
        queued_work: Normalized cumulative work through this request.
        own_work: Normalized work belonging to this request.
        queued_jobs: Cumulative generation count through this request.
        own_jobs: Generations belonging to this request.
        average_work_per_second: Horde-wide normalized throughput per thread.
        eligible_worker_threads: Advertised threads on compatible workers.

    Returns:
        Separate p50 and p90 remaining-time forecasts for first assignment
        and completion, plus an unstarted-expiry prediction.

    Raises:
        ValueError: If queue values are negative or exclude the request's work.
    """

    if queued_work < 0 or own_work < 0 or queued_jobs < 0 or own_jobs < 0 or eligible_worker_threads < 0:
        raise ValueError("Queue work and worker counts must be non-negative")
    if own_work > queued_work or own_jobs > queued_jobs:
        raise ValueError("Request work must be included in the cumulative queue values")

    has_observed_capacity = average_work_per_second > 0 and eligible_worker_threads > 0
    start_parallel_threads = min(eligible_worker_threads, max(queued_jobs - own_jobs, 0))
    completion_parallel_threads = min(eligible_worker_threads, queued_jobs)
    start_throughput = average_work_per_second * start_parallel_threads if start_parallel_threads > 0 else 1
    completion_throughput = average_work_per_second * completion_parallel_threads if has_observed_capacity else 1

    start_p50 = max(queued_work - own_work, 0) / start_throughput
    completion_p50 = queued_work / completion_throughput
    start_p90 = _p90_from_p50(start_p50)
    completion_p90 = _p90_from_p50(completion_p50)
    remaining_lifetime = max((request_expires_at - forecasted_at).total_seconds(), 0)

    return SchedulingForecast(
        estimator=SHADOW_ESTIMATOR,
        forecasted_at=forecasted_at,
        start_p50_seconds=start_p50,
        start_p90_seconds=start_p90,
        completion_p50_seconds=completion_p50,
        completion_p90_seconds=completion_p90,
        predicted_stall=not has_observed_capacity or start_p90 >= remaining_lifetime,
    )


def store_scheduling_forecast(
    *,
    request_id: str,
    forecast: SchedulingForecast,
    request_expires_at: datetime,
    already_started: bool,
) -> None:
    """Queue the first shadow forecast available for a request."""

    _enqueue_scheduling_event(
        _StoreForecastEvent(
            request_id=request_id,
            forecast=forecast,
            request_expires_at=request_expires_at,
            already_started=already_started,
        ),
    )


def record_worker_dispatch(observation: DispatchObservation) -> None:
    """Queue one successful worker pop for assignment-pressure observation.

    Args:
        observation: Immutable facts captured from the successful pop.
    """

    _enqueue_scheduling_event(_DispatchObservedEvent(observation))


def record_capacity_return(observation: CapacityReturn) -> None:
    """Queue one worker-pop batch that returned its occupied thread.

    Args:
        observation: Immutable identity and timing of the returned capacity.
    """

    _enqueue_scheduling_event(_CapacityReturnedEvent(observation))


def get_request_assignment_pressure(
    *,
    observed_at: datetime,
    target_request_id: str,
    target_user_id: str,
    target_created_at: datetime,
    target_extra_priority: int,
    eligible_worker_states: Mapping[str, WorkerSchedulingState],
    eligible_worker_threads: int,
    active_dispatch_ids: Sequence[str],
    preceding_arrivals: Sequence[PrecedingArrival],
    redis_backend: Any | None = None,
) -> AssignmentPressure:
    """Load compatible observations and return request-specific pressure.

    Args:
        observed_at: End of the bounded observation window.
        target_request_id: Queued request whose opportunities are measured.
        target_user_id: Owner of the target request.
        target_created_at: Target creation time used by normal ordering.
        target_extra_priority: Target's Kudos-derived normal queue priority.
        eligible_worker_states: Current scheduling state by eligible worker ID.
        eligible_worker_threads: Advertised threads across eligible workers.
        active_dispatch_ids: Pop batches currently occupying those workers.
        preceding_arrivals: Recent compatible demand independently observed to
            have entered ahead of the target.
        redis_backend: Optional Redis implementation used by tests.

    Returns:
        Bounded evidence. Redis failure produces an evidence-free result.
    """

    backend = hr.horde_r if redis_backend is None else redis_backend
    dispatch_observations: list[DispatchObservation] = []
    capacity_returns: list[CapacityReturn] = []
    if backend is not None:
        minimum_score = max(target_created_at, observed_at - ASSIGNMENT_PRESSURE_WINDOW).timestamp()
        maximum_score = observed_at.timestamp()
        history_score = observed_at.timestamp() - DISPATCH_OBSERVATION_RETENTION.total_seconds()
        try:
            worker_ids = tuple(eligible_worker_states)
            pipeline = backend.pipeline(transaction=False)
            for worker_id in eligible_worker_states:
                pipeline.zrangebyscore(_dispatch_observation_key(worker_id), history_score, maximum_score)
                pipeline.zrangebyscore(
                    _capacity_return_key(worker_id),
                    minimum_score,
                    maximum_score,
                    withscores=True,
                )
            history_results = pipeline.execute()
            for worker_index, _worker_id in enumerate(worker_ids):
                dispatch_payloads = history_results[worker_index * 2]
                return_payloads = history_results[(worker_index * 2) + 1]
                for payload in dispatch_payloads:
                    observation = _parse_dispatch_observation(_decode_redis_value(payload))
                    if observation is not None:
                        dispatch_observations.append(observation)
                for payload, score in return_payloads:
                    capacity_return = _parse_capacity_return(
                        _decode_redis_value(payload),
                        datetime.fromtimestamp(float(score)),
                    )
                    if capacity_return is not None:
                        capacity_returns.append(capacity_return)
        except Exception as err:
            logger.warning(f"Unable to load request assignment pressure: {err}")
            dispatch_observations = []
            capacity_returns = []

    return calculate_assignment_pressure(
        observed_at=observed_at,
        target_request_id=target_request_id,
        target_user_id=target_user_id,
        target_created_at=target_created_at,
        target_extra_priority=target_extra_priority,
        eligible_worker_states=eligible_worker_states,
        eligible_worker_threads=eligible_worker_threads,
        active_dispatch_ids=active_dispatch_ids,
        preceding_arrivals=preceding_arrivals,
        dispatch_observations=dispatch_observations,
        capacity_returns=capacity_returns,
    )


def record_request_start_forecast(
    *,
    request_id: str,
    gentype: str,
    started_at: datetime,
    request_expires_at: datetime,
) -> None:
    """Queue a request's first observed assignment time."""

    _enqueue_scheduling_event(
        _StartObservedEvent(
            request_id=request_id,
            gentype=gentype,
            started_at=started_at,
            request_expires_at=request_expires_at,
        ),
    )


def record_request_completion_forecast(*, request_id: str, gentype: str, completed_at: datetime) -> None:
    """Queue a request's observed completion time."""

    _enqueue_scheduling_event(
        _CompletionObservedEvent(request_id=request_id, gentype=gentype, completed_at=completed_at),
    )


def record_request_expiry_forecast(
    *,
    request_id: str,
    gentype: str,
    expired_at: datetime,
    expired_without_start: bool,
) -> None:
    """Queue an observed request expiry for stall validation."""

    _enqueue_scheduling_event(
        _ExpiryObservedEvent(
            request_id=request_id,
            gentype=gentype,
            expired_at=expired_at,
            expired_without_start=expired_without_start,
        ),
    )


def record_request_cancellation_forecast(*, request_id: str, cancelled_at: datetime) -> None:
    """Queue a cancellation so it is excluded from terminal validation."""

    _enqueue_scheduling_event(
        _CancellationObservedEvent(request_id=request_id, cancelled_at=cancelled_at),
    )


def wait_for_scheduling_forecast_events(timeout_seconds: float = 2) -> bool:
    """Wait for queued scheduling events, primarily for tests and shutdowns.

    Args:
        timeout_seconds: Maximum time to wait for the current queue to drain.

    Returns:
        ``True`` when all queued events have been processed before the timeout.
    """

    deadline = time.monotonic() + timeout_seconds
    while _scheduling_event_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return _scheduling_event_queue.unfinished_tasks == 0


def _enqueue_scheduling_event(event: _SchedulingEvent) -> None:
    try:
        _ensure_scheduling_worker()
        _scheduling_event_queue.put_nowait(event)
    except queue.Full:
        logger.warning("Dropping request scheduling telemetry because its local queue is full")
        _record_dropped_event("queue_full")
    except Exception as err:
        logger.warning(f"Unable to queue request scheduling telemetry: {err}")
        _record_dropped_event("enqueue_error")


def _ensure_scheduling_worker() -> None:
    global _scheduling_worker  # noqa: PLW0603

    if _scheduling_worker is not None and _scheduling_worker.is_alive():
        return
    with _scheduling_worker_lock:
        if _scheduling_worker is not None and _scheduling_worker.is_alive():
            return
        _scheduling_worker = threading.Thread(
            target=_process_scheduling_events,
            name="request-scheduling-telemetry",
            daemon=True,
        )
        _scheduling_worker.start()


def _process_scheduling_events() -> None:
    while True:
        event = _scheduling_event_queue.get()
        try:
            _process_scheduling_event(event, hr.horde_r)
        except Exception as err:
            logger.warning(f"Unable to process request scheduling telemetry: {err}")
            _record_dropped_event("processing_error")
        finally:
            _scheduling_event_queue.task_done()


def _record_dropped_event(reason: str) -> None:
    try:
        metrics.request_scheduling_events_dropped.add(1, {"horde.reason": reason})
    except Exception as err:
        logger.warning(f"Unable to record dropped request scheduling event: {err}")


def _process_scheduling_event(event: _SchedulingEvent, redis_backend: Any) -> None:
    if isinstance(event, _DispatchObservedEvent):
        _store_dispatch_observation(redis_backend, event.observation)
        return
    if isinstance(event, _CapacityReturnedEvent):
        _store_capacity_return(redis_backend, event.observation)
        return

    key = _forecast_key(event.request_id)
    if isinstance(event, _StoreForecastEvent):
        if redis_backend.hexists(key, _CANCELLED_AT_FIELD):
            return
        payload = asdict(event.forecast)
        payload["forecasted_at"] = event.forecast.forecasted_at.isoformat()
        serialized_forecast = json.dumps(payload)
        if not event.already_started:
            redis_backend.hsetnx(key, _START_FORECAST_FIELD, serialized_forecast)
        redis_backend.hsetnx(key, _COMPLETION_FORECAST_FIELD, serialized_forecast)
        # Close the check/store race with cancellation. Whichever event runs
        # last removes or declines both forecast fields.
        if redis_backend.hexists(key, _CANCELLED_AT_FIELD):
            redis_backend.hdel(key, _START_FORECAST_FIELD, _COMPLETION_FORECAST_FIELD)
            return
        _extend_state_expiry(redis_backend, key, event.request_expires_at)
    elif isinstance(event, _StartObservedEvent):
        redis_backend.hsetnx(key, _GENTYPE_FIELD, event.gentype)
        redis_backend.hsetnx(key, _STARTED_AT_FIELD, event.started_at.isoformat())
        _extend_state_expiry(redis_backend, key, event.request_expires_at)
    elif isinstance(event, _CompletionObservedEvent):
        redis_backend.hsetnx(key, _GENTYPE_FIELD, event.gentype)
        redis_backend.hsetnx(key, _COMPLETED_AT_FIELD, event.completed_at.isoformat())
        _extend_state_expiry(redis_backend, key, event.completed_at)
    elif isinstance(event, _ExpiryObservedEvent):
        redis_backend.hsetnx(key, _GENTYPE_FIELD, event.gentype)
        redis_backend.hsetnx(key, _EXPIRED_WITHOUT_START_FIELD, json.dumps(event.expired_without_start))
        _extend_state_expiry(redis_backend, key, event.expired_at)
    else:
        redis_backend.hsetnx(key, _CANCELLED_AT_FIELD, event.cancelled_at.isoformat())
        redis_backend.hdel(key, _START_FORECAST_FIELD, _COMPLETION_FORECAST_FIELD)
        _extend_state_expiry(redis_backend, key, event.cancelled_at)
        return
    _finalize_scheduling_validation(redis_backend, key)


def _store_dispatch_observation(redis_backend: Any, observation: DispatchObservation) -> None:
    payload = asdict(observation)
    payload["request_created_at"] = observation.request_created_at.isoformat()
    payload["dispatched_at"] = observation.dispatched_at.isoformat()
    serialized_observation = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    _store_bounded_observation(
        redis_backend,
        _dispatch_observation_key(observation.worker_id),
        serialized_observation,
        observation.dispatched_at,
    )


def _store_capacity_return(redis_backend: Any, observation: CapacityReturn) -> None:
    payload = {
        "worker_id": observation.worker_id,
        "dispatch_id": observation.dispatch_id,
    }
    _store_bounded_observation(
        redis_backend,
        _capacity_return_key(observation.worker_id),
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        observation.returned_at,
    )


def _store_bounded_observation(redis_backend: Any, key: str, member: str, observed_at: datetime) -> None:
    score = observed_at.timestamp()
    redis_backend.zadd(key, {member: score})
    redis_backend.zremrangebyscore(key, "-inf", score - DISPATCH_OBSERVATION_RETENTION.total_seconds())
    redis_backend.expire(key, int(DISPATCH_OBSERVATION_RETENTION.total_seconds()))


def _finalize_scheduling_validation(redis_backend: Any, key: str) -> None:
    state = _decode_redis_mapping(redis_backend.hgetall(key))
    if not state or _CANCELLED_AT_FIELD in state:
        return
    gentype = state.get(_GENTYPE_FIELD)
    if gentype is None:
        return

    start_forecast = _parse_forecast_field(redis_backend, state, key, _START_FORECAST_FIELD)
    started_at = _parse_datetime(state.get(_STARTED_AT_FIELD))
    if start_forecast is not None and started_at is not None and redis_backend.hsetnx(key, _START_VALIDATED_FIELD, "1"):
        metrics.record_request_estimate_validation(
            estimator=start_forecast.estimator,
            gentype=gentype,
            phase="start",
            predicted_p50_seconds=start_forecast.start_p50_seconds,
            predicted_p90_seconds=start_forecast.start_p90_seconds,
            observed_seconds=max((started_at - start_forecast.forecasted_at).total_seconds(), 0),
        )

    completion_forecast = _parse_forecast_field(redis_backend, state, key, _COMPLETION_FORECAST_FIELD)
    completed_at = _parse_datetime(state.get(_COMPLETED_AT_FIELD))
    if (
        completion_forecast is not None
        and completed_at is not None
        and redis_backend.hsetnx(key, _COMPLETION_VALIDATED_FIELD, "1")
    ):
        metrics.record_request_estimate_validation(
            estimator=completion_forecast.estimator,
            gentype=gentype,
            phase="completion",
            predicted_p50_seconds=completion_forecast.completion_p50_seconds,
            predicted_p90_seconds=completion_forecast.completion_p90_seconds,
            observed_seconds=max((completed_at - completion_forecast.forecasted_at).total_seconds(), 0),
        )

    terminal_observed = completed_at is not None or _EXPIRED_WITHOUT_START_FIELD in state
    if start_forecast is not None and terminal_observed and redis_backend.hsetnx(key, _STALL_VALIDATED_FIELD, "1"):
        expired_without_start = json.loads(state.get(_EXPIRED_WITHOUT_START_FIELD, "false"))
        metrics.record_request_stall_validation(
            estimator=start_forecast.estimator,
            gentype=gentype,
            predicted_stall=start_forecast.predicted_stall,
            expired_without_start=expired_without_start,
        )


def _extend_state_expiry(redis_backend: Any, key: str, observed_at: datetime) -> None:
    required_ttl_seconds = max(int(FORECAST_EXPIRY_GRACE.total_seconds()), 60)
    current_time = datetime.utcnow()
    if observed_at > current_time:
        required_ttl_seconds += int((observed_at - current_time).total_seconds())
    current_ttl_seconds = redis_backend.ttl(key)
    if current_ttl_seconds < required_ttl_seconds:
        redis_backend.expire(key, required_ttl_seconds)


def _p90_from_p50(p50_seconds: float) -> float:
    return p50_seconds + max(P90_MINIMUM_MARGIN_SECONDS, p50_seconds * P90_RELATIVE_MARGIN)


def _forecast_key(request_id: str) -> str:
    return f"request_scheduling_forecast:{request_id}"


def _decode_redis_mapping(mapping: dict[Any, Any]) -> dict[str, str]:
    return {_decode_redis_value(key): _decode_redis_value(value) for key, value in mapping.items()}


def _decode_redis_value(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse_forecast(payload: str, key: str) -> SchedulingForecast | None:
    try:
        parsed = json.loads(payload)
        predicted_stall = parsed["predicted_stall"]
        if not isinstance(predicted_stall, bool):
            raise TypeError("predicted_stall must be a boolean")
        return SchedulingForecast(
            estimator=str(parsed["estimator"]),
            forecasted_at=datetime.fromisoformat(parsed["forecasted_at"]),
            start_p50_seconds=float(parsed["start_p50_seconds"]),
            start_p90_seconds=float(parsed["start_p90_seconds"]),
            completion_p50_seconds=float(parsed["completion_p50_seconds"]),
            completion_p90_seconds=float(parsed["completion_p90_seconds"]),
            predicted_stall=predicted_stall,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning(f"Discarding malformed request scheduling forecast at {key}")
        return None


def _parse_dispatch_observation(payload: str) -> DispatchObservation | None:
    try:
        parsed = json.loads(payload)
        worker_state = parsed["worker_state"]
        return DispatchObservation(
            dispatch_id=str(parsed["dispatch_id"]),
            worker_id=str(parsed["worker_id"]),
            worker_state=parse_worker_scheduling_state(worker_state),
            request_id=str(parsed["request_id"]),
            request_created_at=datetime.fromisoformat(parsed["request_created_at"]),
            request_extra_priority=int(parsed["request_extra_priority"]),
            selected_from_priority_queue=bool(parsed["selected_from_priority_queue"]),
            priority_user_ids=tuple(str(user_id) for user_id in parsed["priority_user_ids"]),
            dispatched_at=datetime.fromisoformat(parsed["dispatched_at"]),
            assigned_work=float(parsed["assigned_work"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("Discarding malformed request scheduling observation")
        return None


def _parse_capacity_return(payload: str, returned_at: datetime) -> CapacityReturn | None:
    try:
        parsed = json.loads(payload)
        return CapacityReturn(
            worker_id=str(parsed["worker_id"]),
            dispatch_id=str(parsed["dispatch_id"]),
            returned_at=returned_at,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("Discarding malformed request capacity-return observation")
        return None


def _parse_forecast_field(
    redis_backend: Any,
    state: dict[str, str],
    key: str,
    field: str,
) -> SchedulingForecast | None:
    payload = state.get(field)
    if payload is None:
        return None
    forecast = _parse_forecast(payload, key)
    if forecast is None:
        redis_backend.hdel(key, field)
    return forecast


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _dispatch_observation_key(worker_id: str) -> str:
    return f"request_dispatches:{worker_id}"


def _capacity_return_key(worker_id: str) -> str:
    return f"request_capacity_returns:{worker_id}"
