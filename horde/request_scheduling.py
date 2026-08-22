# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shadow forecasts and observations for request scheduling."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from horde import metrics
from horde.horde_redis import horde_redis as hr
from horde.logger import logger  # type: ignore[attr-defined]

SHADOW_ESTIMATOR = "compatible-queue-v1"
P90_MINIMUM_MARGIN_SECONDS = 60
P90_RELATIVE_MARGIN = 0.5
FORECAST_EXPIRY_GRACE = timedelta(minutes=10)
SCHEDULING_EVENT_QUEUE_SIZE = 2048

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


type _SchedulingEvent = (
    _StoreForecastEvent
    | _StartObservedEvent
    | _CompletionObservedEvent
    | _ExpiryObservedEvent
    | _CancellationObservedEvent
)

_scheduling_event_queue: queue.Queue[_SchedulingEvent] = queue.Queue(maxsize=SCHEDULING_EVENT_QUEUE_SIZE)
_scheduling_worker_lock = threading.Lock()
_scheduling_worker: threading.Thread | None = None


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
