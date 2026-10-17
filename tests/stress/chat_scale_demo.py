# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Process helpers for the chat front scale demo driven by ``run_chat_scale_demo.sh``.

Three subcommands:

- ``front`` runs the OpenAI-compatible front (``horde.oai``) with the ``httpx`` request log enabled, one line per
  call the front makes to a Horde, so the summary can count submits, status reads and cancels by route. The
  repository root must be on ``PYTHONPATH``.
- ``sample`` polls each Horde instance's heartbeat (which carries the waitress gauges), the front's ``/healthz``
  in-flight count, Redis ``INFO`` and, optionally, Postgres backend counts every ``--interval`` seconds, and appends
  one CSV row per sample, with the resident memory and CPU of each process found listening on the sampled ports.
- ``summarize`` reads a run directory (samples, Locust CSVs, the client evidence file and the logs) and writes
  ``summary.md`` and ``summary.json`` with per-stage numbers. It reports measurements and never passes or fails a
  run.

Usage:
    python tests/stress/chat_scale_demo.py sample --horde-urls http://127.0.0.1:7101 --front-url http://127.0.0.1:7002 \\
        --redis-host 127.0.0.1 --redis-port 6379 --out run/samples.csv
    python tests/stress/chat_scale_demo.py summarize --run-dir run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import math
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import requests

HEARTBEAT_PATH: str = "/api/v2/status/heartbeat"
"""The Horde route that reports the waitress gauges (``queue``, ``threads``, ``active_count``)."""

HEALTHZ_PATH: str = "/healthz"
"""The front route that reports its in-flight request count."""

SAMPLE_REQUEST_TIMEOUT_SECONDS: float = 4.0
"""How long one gauge read may take; below the default interval, so a saturated Horde cannot stall the sampler."""

BYTES_PER_MEGABYTE: int = 1024 * 1024
"""Bytes in a mebibyte, the unit RSS is reported in."""

HTTPX_LOGGER_NAME: str = "httpx"
"""The logger ``httpx`` writes one INFO line per request to."""

HTTPX_LOG_FORMAT: str = "%(created).3f httpx %(message)s"
"""The front's request log line: Unix time, then httpx's ``HTTP Request: METHOD URL "STATUS"``."""

HTTPX_LINE_PATTERN: re.Pattern[str] = re.compile(r"^(\d+\.\d+) httpx HTTP Request: (\w+) (\S+) \"HTTP/[\d.]+ (\d+)")
"""Parse a front request log line into time, method, URL and status."""

FINISHED_LINE_PATTERN: re.Pattern[str] = re.compile(
    r"Chat request (\S+) finished: streamed=(\w+) submitted_at=([\d.]+) woke_at=([\d.]+) woke_by=(\S+) waited=([\d.]+)s",
)
"""Parse the front's one line per finished request (``horde.oai.app.RequestTrace.finish``)."""

SAMPLE_HORDE_COLUMN_PATTERN: re.Pattern[str] = re.compile(r"^horde_(\d+)_active$")
"""Find the Horde ports a samples file covers from its column names."""

FRONT_LOG_PATTERNS: dict[str, str] = {
    "cancelled": "Cancelled chat request",
    "cancel_failed": "Cancelling chat request",
    "client_disconnected": "disconnected while waiting",
    "horde_refused_connection": "refused the connection",
    "status_unavailable": "unavailable, retrying at the next poll",
    "status_unreachable": "unreachable, retrying at the next poll",
    "signal_subscription_failed": "Chat signal subscription failed",
    "traceback": "Traceback",
}
"""Front log substrings counted in the summary: the cancel path and the failure modes that point at a bottleneck."""

HORDE_LOG_PATTERNS: dict[str, str] = {
    "queue_pool_limit": "QueuePool limit",
    "too_many_postgres_clients": "too many clients already",
    "operational_error": "OperationalError",
    "redis_error": "redis.exceptions",
    "traceback": "Traceback",
}
"""Horde log substrings counted in the summary as evidence of database, Redis or crash bottlenecks."""

LOCUST_LOG_PATTERNS: dict[str, str] = {
    "cpu_warning": "CPU usage above",
    "too_many_open_files": "Too many open files",
    "no_buffer_space": "10055",
    "address_in_use": "10048",
    "connection_refused": "10061",
    "connection_reset": "Connection reset",
}
"""Load generator log substrings that show the generator, not the system under test, was the limit."""

PERCENTILES: tuple[int, ...] = (50, 95, 99)
"""The percentiles the summary reports for latency distributions."""


@dataclass
class ProcessGauge:
    """Track the process listening on one port, for RSS and CPU sampling."""

    port: int
    """The listening port the process is found by."""
    process: psutil.Process | None = None
    """The process last found listening on ``port``."""

    def read(self) -> tuple[float | None, float | None]:
        """Return the process's resident memory in MB and CPU percent since the last read, or Nones when not found."""
        if self.process is None or not self.process.is_running():
            self.process = _find_listening_process(self.port)
            if self.process is None:
                return None, None
            self.process.cpu_percent(None)
        try:
            with self.process.oneshot():
                return self.process.memory_info().rss / BYTES_PER_MEGABYTE, self.process.cpu_percent(None)
        except psutil.Error as error:
            logging.warning("Reading process on port %d failed: %s", self.port, error)
            self.process = None
            return None, None


def _find_listening_process(port: int) -> psutil.Process | None:
    """Return the process listening on TCP ``port``, or None when there is none or it is not visible."""
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied as error:
        logging.warning("Listing connections to find port %d is not permitted: %s", port, error)
        return None
    for connection in connections:
        is_listener = connection.status == psutil.CONN_LISTEN and connection.laddr and connection.laddr.port == port
        if is_listener and connection.pid:
            try:
                return psutil.Process(connection.pid)
            except psutil.NoSuchProcess:
                return None
    return None


def _port_of(url: str) -> int:
    """Return the port of an ``http://host:port`` URL."""
    return int(url.rstrip("/").rsplit(":", 1)[1].split("/", 1)[0])


def _timed_get_json(url: str) -> tuple[dict[str, Any] | None, float]:
    """Return the JSON body of a GET and its latency in milliseconds; the body is None on any failure."""
    started_at = time.perf_counter()
    try:
        response = requests.get(url, timeout=SAMPLE_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as error:
        logging.warning("Sampling %s failed: %s", url, error)
        return None, (time.perf_counter() - started_at) * 1000
    return body, (time.perf_counter() - started_at) * 1000


def _read_redis(redis_host: str, redis_port: int) -> dict[str, Any]:
    """Return the Redis ``INFO`` fields the demo samples, or an empty dict when Redis does not answer."""
    import redis

    try:
        info = redis.Redis(host=redis_host, port=redis_port, socket_timeout=SAMPLE_REQUEST_TIMEOUT_SECONDS).info()
    except redis.RedisError as error:
        logging.warning("Sampling Redis failed: %s", error)
        return {}
    return {
        "redis_connected_clients": info.get("connected_clients"),
        "redis_blocked_clients": info.get("blocked_clients"),
        "redis_pubsub_channels": info.get("pubsub_channels"),
        "redis_pubsub_patterns": info.get("pubsub_patterns"),
        "redis_ops_per_sec": info.get("instantaneous_ops_per_sec"),
        "redis_used_memory_mb": round(info.get("used_memory", 0) / BYTES_PER_MEGABYTE, 1),
    }


def _read_postgres(postgres_dsn: str | None) -> dict[str, Any]:
    """Return Postgres backend counts, or an empty dict when no DSN is given or the query fails."""
    if not postgres_dsn:
        return {}
    import psycopg2

    try:
        with psycopg2.connect(postgres_dsn, connect_timeout=int(SAMPLE_REQUEST_TIMEOUT_SECONDS)) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), count(*) FILTER (WHERE state = 'active'), "
                    "(SELECT setting::int FROM pg_settings WHERE name = 'max_connections') "
                    "FROM pg_stat_activity WHERE backend_type = 'client backend'",
                )
                total, active, max_connections = cursor.fetchone()
        connection.close()
    except psycopg2.Error as error:
        logging.warning("Sampling Postgres failed: %s", error)
        return {}
    return {"pg_connections": total, "pg_active": active, "pg_max_connections": max_connections}


def _sample_columns(horde_ports: list[int]) -> list[str]:
    """Return the samples CSV header for the given Horde ports."""
    columns = [
        "ts",
        "front_inflight",
        "front_signal_subscribed",
        "front_healthz_ms",
        "front_rss_mb",
        "front_cpu_percent",
        "redis_connected_clients",
        "redis_blocked_clients",
        "redis_pubsub_channels",
        "redis_pubsub_patterns",
        "redis_ops_per_sec",
        "redis_used_memory_mb",
        "pg_connections",
        "pg_active",
        "pg_max_connections",
    ]
    for port in horde_ports:
        columns.extend(
            [
                f"horde_{port}_active",
                f"horde_{port}_queue",
                f"horde_{port}_threads",
                f"horde_{port}_heartbeat_ms",
                f"horde_{port}_rss_mb",
                f"horde_{port}_cpu_percent",
            ],
        )
    return columns


def _take_sample(
    *,
    executor: concurrent.futures.ThreadPoolExecutor,
    horde_urls: list[str],
    front_url: str,
    redis_host: str,
    redis_port: int,
    postgres_dsn: str | None,
    gauges: dict[int, ProcessGauge],
) -> dict[str, Any]:
    """Return one sample row; the HTTP, Redis and Postgres reads run concurrently so one slow source cannot skew the
    others' timestamps."""
    row: dict[str, Any] = {"ts": round(time.time(), 3)}
    heartbeat_futures = {horde_url: executor.submit(_timed_get_json, horde_url + HEARTBEAT_PATH) for horde_url in horde_urls}
    healthz_future = executor.submit(_timed_get_json, front_url + HEALTHZ_PATH)
    redis_future = executor.submit(_read_redis, redis_host, redis_port)
    postgres_future = executor.submit(_read_postgres, postgres_dsn)

    healthz_body, healthz_ms = healthz_future.result()
    row["front_healthz_ms"] = round(healthz_ms, 1)
    if healthz_body is not None:
        row["front_inflight"] = healthz_body.get("inflight")
        row["front_signal_subscribed"] = healthz_body.get("signal_subscribed")
    front_rss_mb, front_cpu_percent = gauges[_port_of(front_url)].read()
    row["front_rss_mb"] = round(front_rss_mb, 1) if front_rss_mb is not None else None
    row["front_cpu_percent"] = front_cpu_percent
    row.update(redis_future.result())
    row.update(postgres_future.result())
    for horde_url, heartbeat_future in heartbeat_futures.items():
        port = _port_of(horde_url)
        heartbeat_body, heartbeat_ms = heartbeat_future.result()
        row[f"horde_{port}_heartbeat_ms"] = round(heartbeat_ms, 1)
        if heartbeat_body is not None:
            row[f"horde_{port}_active"] = heartbeat_body.get("active_count")
            row[f"horde_{port}_queue"] = heartbeat_body.get("queue")
            row[f"horde_{port}_threads"] = heartbeat_body.get("threads")
        horde_rss_mb, horde_cpu_percent = gauges[port].read()
        row[f"horde_{port}_rss_mb"] = round(horde_rss_mb, 1) if horde_rss_mb is not None else None
        row[f"horde_{port}_cpu_percent"] = horde_cpu_percent
    return row


def run_sampler(arguments: argparse.Namespace) -> int:
    """Append a sample row every interval until the duration passes or the process is stopped.

    Args:
        arguments: The ``sample`` subcommand's arguments.

    Returns:
        The process exit code.
    """
    horde_urls = [horde_url.strip().rstrip("/") for horde_url in arguments.horde_urls.split(",") if horde_url.strip()]
    front_url = arguments.front_url.rstrip("/")
    horde_ports = [_port_of(horde_url) for horde_url in horde_urls]
    gauges = {port: ProcessGauge(port) for port in [*horde_ports, _port_of(front_url)]}
    columns = _sample_columns(horde_ports)
    output_path = Path(arguments.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + arguments.duration if arguments.duration else math.inf
    with (
        output_path.open("w", newline="", encoding="utf-8") as output_file,
        concurrent.futures.ThreadPoolExecutor(max_workers=len(horde_urls) + 3) as executor,
    ):
        writer = csv.DictWriter(output_file, fieldnames=columns)
        writer.writeheader()
        while time.monotonic() < deadline:
            tick_started_at = time.monotonic()
            row = _take_sample(
                executor=executor,
                horde_urls=horde_urls,
                front_url=front_url,
                redis_host=arguments.redis_host,
                redis_port=arguments.redis_port,
                postgres_dsn=arguments.postgres_dsn,
                gauges=gauges,
            )
            writer.writerow(row)
            # Each row is flushed so a sampler stopped by the runner's teardown leaves every sample on disk.
            output_file.flush()
            time.sleep(max(0.0, arguments.interval - (time.monotonic() - tick_started_at)))
    return 0


def run_front(_arguments: argparse.Namespace) -> int:
    """Run the front with the ``httpx`` request log written to stderr; returns when uvicorn stops."""
    httpx_handler = logging.StreamHandler(sys.stderr)
    httpx_handler.setFormatter(logging.Formatter(HTTPX_LOG_FORMAT))
    httpx_logger = logging.getLogger(HTTPX_LOGGER_NAME)
    httpx_logger.addHandler(httpx_handler)
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.propagate = False

    from horde.oai.__main__ import main as run_front_main

    run_front_main()
    return 0


@dataclass
class StageWindow:
    """Represents one stage of the run on the wall clock."""

    index: int
    """The stage's position, from 1."""
    target_clients: int
    """The client users the stage holds, on top of the workers."""
    started_at: float
    """When the stage started, in Unix seconds."""
    ended_at: float
    """When the stage ended, in Unix seconds."""

    def contains(self, timestamp: float | None) -> bool:
        """Return whether ``timestamp`` falls in the stage."""
        return timestamp is not None and self.started_at <= timestamp < self.ended_at

    @property
    def seconds(self) -> float:
        """Return the stage's length."""
        return self.ended_at - self.started_at


@dataclass
class RunFiles:
    """Represents the parsed contents of a run directory."""

    run_start: dict[str, Any]
    """The evidence file's ``run_start`` record."""
    run_stop_at: float | None
    """The evidence file's ``run_stop`` time, when the run ended cleanly."""
    client_records: list[dict[str, Any]]
    """Every client request record."""
    samples: list[dict[str, str]]
    """Every sampler row."""
    horde_ports: list[int]
    """The Horde ports the samples cover."""
    finished_lines: list[dict[str, Any]]
    """The front's finished-request lines."""
    front_calls: list[tuple[float, str, str, int]]
    """The front's Horde calls as (time, method, route, status)."""
    stats_history: list[dict[str, str]]
    """The Locust full stats history rows."""
    stats_totals: list[dict[str, str]]
    """The Locust final stats rows."""
    log_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    """Pattern counts per log file."""


def _percentile(values: list[float], percentile: int) -> float | None:
    """Return the nearest-rank percentile of ``values``, or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    """Return the count, the reported percentiles and the maximum of ``values``, in seconds rounded to milliseconds."""
    summary: dict[str, float | int | None] = {"count": len(values)}
    for percentile in PERCENTILES:
        value = _percentile(values, percentile)
        summary[f"p{percentile}"] = round(value, 3) if value is not None else None
    summary["max"] = round(max(values), 3) if values else None
    return summary


def _float_or_none(text: str | None) -> float | None:
    """Return ``text`` as a float, or None when it is blank or not a number."""
    if text is None or text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _route_of(url: str) -> str:
    """Return a Horde call's route with the request id replaced, for grouping."""
    path = url.split("/api", 1)[-1].split("?", 1)[0]
    return re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/<id>", path)


def _count_patterns(path: Path, patterns: dict[str, str]) -> dict[str, int]:
    """Return how many lines of ``path`` contain each pattern; an absent file counts nothing."""
    counts = dict.fromkeys(patterns, 0)
    if not path.exists():
        return counts
    with path.open(encoding="utf-8", errors="replace") as log_file:
        for line in log_file:
            for pattern_name, pattern_text in patterns.items():
                if pattern_text in line:
                    counts[pattern_name] += 1
    return counts


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Return the rows of a CSV file, or an empty list when it does not exist."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def _load_run(run_dir: Path) -> RunFiles:
    """Parse a run directory's files.

    Raises:
        FileNotFoundError: The evidence file or its ``run_start`` record is missing.
    """
    run_start: dict[str, Any] | None = None
    run_stop_at: float | None = None
    client_records: list[dict[str, Any]] = []
    with (run_dir / "chat_evidence.jsonl").open(encoding="utf-8") as evidence_file:
        for line in evidence_file:
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "run_start":
                run_start = record
            elif record.get("kind") == "run_stop":
                run_stop_at = record.get("ts")
            elif record.get("kind") == "chat_request":
                client_records.append(record)
    if run_start is None:
        raise FileNotFoundError(f"No run_start record in {run_dir / 'chat_evidence.jsonl'}")

    samples = _read_csv(run_dir / "samples.csv")
    horde_ports: list[int] = []
    if samples:
        for column in samples[0]:
            match = SAMPLE_HORDE_COLUMN_PATTERN.match(column)
            if match:
                horde_ports.append(int(match.group(1)))

    finished_lines: list[dict[str, Any]] = []
    front_calls: list[tuple[float, str, str, int]] = []
    front_log_path = run_dir / "front.log"
    if front_log_path.exists():
        with front_log_path.open(encoding="utf-8", errors="replace") as front_log:
            for line in front_log:
                httpx_match = HTTPX_LINE_PATTERN.match(line)
                if httpx_match:
                    front_calls.append(
                        (float(httpx_match.group(1)), httpx_match.group(2), _route_of(httpx_match.group(3)), int(httpx_match.group(4))),
                    )
                    continue
                finished_match = FINISHED_LINE_PATTERN.search(line)
                if finished_match:
                    finished_lines.append(
                        {
                            "request_id": finished_match.group(1),
                            "streamed": finished_match.group(2) == "True",
                            "submitted_at": float(finished_match.group(3)),
                            "woke_at": float(finished_match.group(4)),
                            "woke_by": finished_match.group(5),
                        },
                    )

    log_counts = {
        "front.log": _count_patterns(front_log_path, FRONT_LOG_PATTERNS),
        "locust.log": _count_patterns(run_dir / "locust.log", LOCUST_LOG_PATTERNS),
        "locust.out": _count_patterns(run_dir / "locust.out", LOCUST_LOG_PATTERNS),
    }
    for horde_log_path in sorted(run_dir.glob("horde-*.log")):
        log_counts[horde_log_path.name] = _count_patterns(horde_log_path, HORDE_LOG_PATTERNS)

    return RunFiles(
        run_start=run_start,
        run_stop_at=run_stop_at,
        client_records=client_records,
        samples=samples,
        horde_ports=horde_ports,
        finished_lines=finished_lines,
        front_calls=front_calls,
        stats_history=_read_csv(run_dir / "locust_stats_history.csv"),
        stats_totals=_read_csv(run_dir / "locust_stats.csv"),
        log_counts=log_counts,
    )


def _stage_windows(run_start: dict[str, Any], run_stop_at: float | None) -> list[StageWindow]:
    """Return the stage windows the ``run_start`` record describes; the last one ends at the run's stop when earlier."""
    first_stage_at = float(run_start["ts"]) + float(run_start["warmup_seconds"])
    stage_seconds = float(run_start["stage_seconds"])
    windows = []
    for stage_position, target_clients in enumerate(run_start["stages"]):
        started_at = first_stage_at + stage_position * stage_seconds
        ended_at = started_at + stage_seconds
        if run_stop_at is not None:
            ended_at = min(ended_at, run_stop_at)
        if ended_at <= started_at:
            break
        windows.append(StageWindow(index=stage_position + 1, target_clients=int(target_clients), started_at=started_at, ended_at=ended_at))
    return windows


def _column_values(samples: list[dict[str, str]], column: str, window: StageWindow) -> list[float]:
    """Return a sample column's numeric values within ``window``."""
    values = []
    for sample in samples:
        if not window.contains(_float_or_none(sample.get("ts"))):
            continue
        value = _float_or_none(sample.get(column))
        if value is not None:
            values.append(value)
    return values


def _peak_and_mean(values: list[float]) -> dict[str, float | None]:
    """Return the maximum and the mean of ``values``."""
    if not values:
        return {"peak": None, "mean": None}
    return {"peak": round(max(values), 1), "mean": round(statistics.fmean(values), 1)}


def _history_delta(stats_history: list[dict[str, str]], name: str, window: StageWindow) -> tuple[int, int]:
    """Return how many requests and failures Locust counted for ``name`` within ``window``, from the cumulative history."""
    in_window = [row for row in stats_history if row.get("Name") == name and window.contains(_float_or_none(row.get("Timestamp")))]
    before_window = [
        row for row in stats_history if row.get("Name") == name and (_float_or_none(row.get("Timestamp")) or 0) < window.started_at
    ]
    if not in_window:
        return 0, 0
    last_row = in_window[-1]
    baseline_row = before_window[-1] if before_window else None
    requests_total = int(float(last_row.get("Total Request Count") or 0))
    failures_total = int(float(last_row.get("Total Failure Count") or 0))
    if baseline_row is not None:
        requests_total -= int(float(baseline_row.get("Total Request Count") or 0))
        failures_total -= int(float(baseline_row.get("Total Failure Count") or 0))
    return requests_total, failures_total


def _peak_users(stats_history: list[dict[str, str]], window: StageWindow) -> int | None:
    """Return the most Locust users seen within ``window``."""
    user_counts = [
        int(float(row["User Count"]))
        for row in stats_history
        if row.get("Name") == "Aggregated" and window.contains(_float_or_none(row.get("Timestamp"))) and row.get("User Count")
    ]
    return max(user_counts) if user_counts else None


def _summarize_stage(run_files: RunFiles, window: StageWindow) -> dict[str, Any]:
    """Return one stage's numbers."""
    samples = run_files.samples
    stage: dict[str, Any] = {
        "stage": window.index,
        "target_clients": window.target_clients,
        "started_at": round(window.started_at, 3),
        "seconds": round(window.seconds, 1),
        "peak_locust_users": _peak_users(run_files.stats_history, window),
        "front_inflight": _peak_and_mean(_column_values(samples, "front_inflight", window)),
        "front_rss_mb": _peak_and_mean(_column_values(samples, "front_rss_mb", window)),
        "front_cpu_percent": _peak_and_mean(_column_values(samples, "front_cpu_percent", window)),
        "front_healthz_ms": _peak_and_mean(_column_values(samples, "front_healthz_ms", window)),
        "redis_connected_clients": _peak_and_mean(_column_values(samples, "redis_connected_clients", window)),
        "redis_ops_per_sec": _peak_and_mean(_column_values(samples, "redis_ops_per_sec", window)),
        "pg_connections": _peak_and_mean(_column_values(samples, "pg_connections", window)),
        "pg_active": _peak_and_mean(_column_values(samples, "pg_active", window)),
        "horde": {},
    }
    for port in run_files.horde_ports:
        stage["horde"][str(port)] = {
            metric: _peak_and_mean(_column_values(samples, f"horde_{port}_{metric}", window))
            for metric in ("active", "queue", "threads", "heartbeat_ms", "rss_mb", "cpu_percent")
        }

    calls_by_route: Counter[str] = Counter(
        f"{method} {route}" for called_at, method, route, _status in run_files.front_calls if window.contains(called_at)
    )
    status_by_route: Counter[str] = Counter(
        f"{method} {route} {status}" for called_at, method, route, status in run_files.front_calls if window.contains(called_at)
    )
    stage["front_horde_calls_per_second"] = {route: round(count / window.seconds, 2) for route, count in sorted(calls_by_route.items())}
    stage["front_horde_call_statuses"] = dict(sorted(status_by_route.items()))
    finished_in_stage = [finished for finished in run_files.finished_lines if window.contains(finished["woke_at"])]
    status_reads = sum(count for route, count in calls_by_route.items() if route.startswith("GET /v2/generate/chat/status"))
    stage["finished_on_front"] = len(finished_in_stage)
    stage["status_reads_per_finished_request"] = round(status_reads / len(finished_in_stage), 2) if finished_in_stage else None
    submits = sum(count for route, count in calls_by_route.items() if route.startswith("POST /v2/generate/chat/async"))
    stage["status_reads_per_submit"] = round(status_reads / submits, 2) if submits else None
    stage["woke_by"] = dict(Counter(finished["woke_by"] for finished in finished_in_stage))

    worker_rates: dict[str, Any] = {}
    for route_name in ("/api/v2/generate/text/pop", "/api/v2/generate/text/submit"):
        request_count, failure_count = _history_delta(run_files.stats_history, route_name, window)
        worker_rates[route_name] = {"per_second": round(request_count / window.seconds, 2), "failures": failure_count}
    stage["worker_calls"] = worker_rates

    records = [record for record in run_files.client_records if window.contains(record.get("finished_at"))]
    stage["client_outcomes"] = dict(Counter(f"{record['client']} {record['outcome']}" for record in records))
    stage["client_statuses"] = dict(Counter(str(record.get("status")) for record in records))
    stage["refusal_codes"] = dict(Counter(str(record.get("error_code")) for record in records if record.get("outcome") == "refused"))
    completed = [record for record in records if record["outcome"] == "completed"]
    stage["usage"] = {
        "reported": sum(1 for record in completed if record.get("estimated_usage") is False),
        "estimated": sum(1 for record in completed if record.get("estimated_usage") is True),
        "unknown": sum(1 for record in completed if record.get("estimated_usage") is None),
    }
    streamed = [record for record in completed if record["client"] == "stream"]
    stage["stream_seconds"] = {
        "time_to_first_heartbeat": _distribution(
            [record["first_heartbeat_at"] - record["sent_at"] for record in streamed if record.get("first_heartbeat_at")],
        ),
        "time_to_content": _distribution([record["content_at"] - record["sent_at"] for record in streamed if record.get("content_at")]),
        "total": _distribution([record["finished_at"] - record["sent_at"] for record in streamed]),
    }
    stage["nonstream_total_seconds"] = _distribution(
        [record["finished_at"] - record["sent_at"] for record in completed if record["client"] == "nonstream"],
    )

    woke_at_by_request = {finished["request_id"]: finished["woke_at"] for finished in run_files.finished_lines}
    wake_latencies = [
        woke_at_by_request[record["request_id"]] - record["worker_submitted_at"]
        for record in completed
        if record.get("worker_submitted_at") and record.get("request_id") in woke_at_by_request
    ]
    stage["submit_to_front_wake_seconds"] = _distribution(wake_latencies)
    return stage


def _summarize_run(run_files: RunFiles) -> dict[str, Any]:
    """Return the run's summary: settings, per-stage numbers, whole-run totals and the log pattern counts."""
    windows = _stage_windows(run_files.run_start, run_files.run_stop_at)
    stages = [_summarize_stage(run_files, window) for window in windows]
    front_rss_values = [
        value for value in (_float_or_none(sample.get("front_rss_mb")) for sample in run_files.samples) if value is not None
    ]
    totals = {
        row["Name"]: {
            "requests": int(float(row.get("Request Count") or 0)),
            "failures": int(float(row.get("Failure Count") or 0)),
            "requests_per_second": round(float(row.get("Requests/s") or 0), 2),
            "median_ms": _float_or_none(row.get("Median Response Time")),
            "p95_ms": _float_or_none(row.get("95%")),
        }
        for row in run_files.stats_totals
        if row.get("Name")
    }
    return {
        "settings": run_files.run_start,
        "stages": stages,
        "front_peak_rss_mb": round(max(front_rss_values), 1) if front_rss_values else None,
        "locust_totals": totals,
        "log_pattern_counts": run_files.log_counts,
    }


def _format_number(value: Any) -> str:
    """Return a table cell for a number or None."""
    if value is None:
        return "-"
    return str(value)


def _peak_mean_cell(values: dict[str, Any]) -> str:
    """Return a ``peak / mean`` table cell."""
    return f"{_format_number(values.get('peak'))} / {_format_number(values.get('mean'))}"


def _distribution_cell(values: dict[str, Any]) -> str:
    """Return a ``p50 / p95 / max (n)`` table cell."""
    percentiles = " / ".join(_format_number(values.get(key)) for key in ("p50", "p95", "max"))
    return f"{percentiles} (n={values.get('count')})"


def _render_markdown(summary: dict[str, Any]) -> str:
    """Return the summary as Markdown tables, one column per stage."""
    stages = summary["stages"]
    lines = ["# Chat front scale demo summary", ""]
    settings = summary["settings"]
    lines.append(
        f"Workers {settings.get('workers')}, stages {settings.get('stages')} of {settings.get('stage_seconds')} s after a "
        f"{settings.get('warmup_seconds')} s warm-up, client shares {settings.get('client_shares')}, simulated generation "
        f"{settings.get('sim_gen_time_seconds')} s, Horde instances {settings.get('horde_urls')}.",
    )
    lines.append("")
    lines.append(f"Front peak RSS over the run: {_format_number(summary['front_peak_rss_mb'])} MB.")
    lines.append("")
    if not stages:
        lines.append("No stage started; see the run's logs.")
        return "\n".join(lines) + "\n"

    header = "| Metric | " + " | ".join(f"Stage {stage['stage']} ({stage['target_clients']} clients)" for stage in stages) + " |"
    separator = "| --- | " + " | ".join("---" for _ in stages) + " |"
    rows: list[tuple[str, list[str]]] = [
        ("Stage length (s)", [_format_number(stage["seconds"]) for stage in stages]),
        ("Peak Locust users (workers included)", [_format_number(stage["peak_locust_users"]) for stage in stages]),
        ("Front in-flight, peak / mean", [_peak_mean_cell(stage["front_inflight"]) for stage in stages]),
        ("Front RSS MB, peak / mean", [_peak_mean_cell(stage["front_rss_mb"]) for stage in stages]),
        ("Front CPU %, peak / mean", [_peak_mean_cell(stage["front_cpu_percent"]) for stage in stages]),
        ("Front /healthz ms, peak / mean", [_peak_mean_cell(stage["front_healthz_ms"]) for stage in stages]),
        ("Redis clients, peak / mean", [_peak_mean_cell(stage["redis_connected_clients"]) for stage in stages]),
        ("Postgres connections, peak / mean", [_peak_mean_cell(stage["pg_connections"]) for stage in stages]),
        ("Postgres active, peak / mean", [_peak_mean_cell(stage["pg_active"]) for stage in stages]),
    ]
    horde_ports = list(stages[0]["horde"])
    for port in horde_ports:
        rows.extend(
            [
                (f"Horde {port} waitress active, peak / mean", [_peak_mean_cell(stage["horde"][port]["active"]) for stage in stages]),
                (f"Horde {port} waitress queue, peak / mean", [_peak_mean_cell(stage["horde"][port]["queue"]) for stage in stages]),
                (f"Horde {port} heartbeat ms, peak / mean", [_peak_mean_cell(stage["horde"][port]["heartbeat_ms"]) for stage in stages]),
                (f"Horde {port} CPU %, peak / mean", [_peak_mean_cell(stage["horde"][port]["cpu_percent"]) for stage in stages]),
                (f"Horde {port} RSS MB, peak / mean", [_peak_mean_cell(stage["horde"][port]["rss_mb"]) for stage in stages]),
            ],
        )
    all_routes = sorted({route for stage in stages for route in stage["front_horde_calls_per_second"]})
    for route in all_routes:
        rows.append((f"Front to Horde {route} /s", [_format_number(stage["front_horde_calls_per_second"].get(route)) for stage in stages]))
    for route_name in ("/api/v2/generate/text/pop", "/api/v2/generate/text/submit"):
        rows.append(
            (
                f"Workers {route_name} /s (failures)",
                [
                    f"{stage['worker_calls'][route_name]['per_second']} ({stage['worker_calls'][route_name]['failures']})"
                    for stage in stages
                ],
            ),
        )
    rows.extend(
        [
            ("Requests finished on the front", [_format_number(stage["finished_on_front"]) for stage in stages]),
            ("Status reads per finished request", [_format_number(stage["status_reads_per_finished_request"]) for stage in stages]),
            ("Status reads per submit", [_format_number(stage["status_reads_per_submit"]) for stage in stages]),
            ("Woken by", [_format_number(stage["woke_by"]) for stage in stages]),
            (
                "Worker submit to front wake s, p50 / p95 / max",
                [_distribution_cell(stage["submit_to_front_wake_seconds"]) for stage in stages],
            ),
            (
                "Stream time to first keepalive s",
                [_distribution_cell(stage["stream_seconds"]["time_to_first_heartbeat"]) for stage in stages],
            ),
            ("Stream time to content s", [_distribution_cell(stage["stream_seconds"]["time_to_content"]) for stage in stages]),
            ("Stream total s", [_distribution_cell(stage["stream_seconds"]["total"]) for stage in stages]),
            ("Non-stream total s", [_distribution_cell(stage["nonstream_total_seconds"]) for stage in stages]),
            ("Usage reported / estimated", [f"{stage['usage']['reported']} / {stage['usage']['estimated']}" for stage in stages]),
            ("Client statuses", [_format_number(stage["client_statuses"]) for stage in stages]),
            ("Refusal codes", [_format_number(stage["refusal_codes"]) for stage in stages]),
            ("Client outcomes", [_format_number(stage["client_outcomes"]) for stage in stages]),
        ],
    )
    lines.append(header)
    lines.append(separator)
    for metric_name, cells in rows:
        lines.append(f"| {metric_name} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Log pattern counts")
    lines.append("")
    for log_name, counts in summary["log_pattern_counts"].items():
        lines.append(f"- {log_name}: {counts}")
    lines.append("")
    lines.append("## Locust totals")
    lines.append("")
    lines.append("| Name | Requests | Failures | Req/s | Median ms | p95 ms |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for name, totals in summary["locust_totals"].items():
        lines.append(
            f"| {name} | {totals['requests']} | {totals['failures']} | {totals['requests_per_second']} | "
            f"{_format_number(totals['median_ms'])} | {_format_number(totals['p95_ms'])} |",
        )
    return "\n".join(lines) + "\n"


def run_summarize(arguments: argparse.Namespace) -> int:
    """Write ``summary.json`` and ``summary.md`` into the run directory and print the Markdown.

    Args:
        arguments: The ``summarize`` subcommand's arguments.

    Returns:
        The process exit code.
    """
    run_dir = Path(arguments.run_dir)
    summary = _summarize_run(_load_run(run_dir))
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    markdown = _render_markdown(summary)
    (run_dir / "summary.md").write_text(markdown, encoding="utf-8")
    sys.stdout.write(markdown)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    front_parser = subparsers.add_parser("front", help="Run the chat front with the Horde call log enabled.")
    front_parser.set_defaults(handler=run_front)

    sample_parser = subparsers.add_parser("sample", help="Sample the gauges into a CSV.")
    sample_parser.add_argument("--horde-urls", required=True, help="Comma-separated Horde base URLs (without /api).")
    sample_parser.add_argument("--front-url", required=True, help="The front's base URL.")
    sample_parser.add_argument("--redis-host", default="127.0.0.1", help="Redis host for INFO.")
    sample_parser.add_argument("--redis-port", type=int, default=6379, help="Redis port for INFO.")
    sample_parser.add_argument("--postgres-dsn", default=None, help="Optional libpq DSN for backend counts.")
    sample_parser.add_argument("--interval", type=float, default=5.0, help="Seconds between samples.")
    sample_parser.add_argument("--duration", type=float, default=0.0, help="Seconds to sample; 0 samples until stopped.")
    sample_parser.add_argument("--out", required=True, help="CSV file to write.")
    sample_parser.set_defaults(handler=run_sampler)

    summarize_parser = subparsers.add_parser("summarize", help="Summarize a run directory.")
    summarize_parser.add_argument("--run-dir", required=True, help="The run directory run_chat_scale_demo.sh wrote.")
    summarize_parser.set_defaults(handler=run_summarize)
    return parser


def main() -> int:
    """Run the requested subcommand and return its exit code."""
    arguments = _build_parser().parse_args()
    if arguments.command != "front":
        # The front's logging is configured by uvicorn and loguru; a root handler there would repeat their lines.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    sys.exit(main())
