# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Settings, shared recording helpers, arguments and run hooks of the chat contract mix.

The chat mix users live in [`chat_front_client`][tests.stress.locustsuite.chat_front_client],
[`chat_native_client`][tests.stress.locustsuite.chat_native_client] and
[`chat_worker_user`][tests.stress.locustsuite.chat_worker_user]; they read the process-wide
[`chat_mix_settings`][tests.stress.locustsuite.chat_mix_settings.chat_mix_settings], which the test start hook here
fills from the ``--chat-mix-*`` arguments and checks. Their counts are pinned by ``events.on_test_start`` like every
other class's.

- [`ChatMixSettings`][tests.stress.locustsuite.chat_mix_settings.ChatMixSettings]: the run's settings, the pairing of
  worker names with personas, which personas are online, and the coverage the run reached.
- [`find_coverage_gaps`][tests.stress.locustsuite.chat_mix_settings.find_coverage_gaps]: what the run failed to
  exercise, checked at test stop.
- The ``record_*`` and ``fail_*`` helpers: how every chat mix user writes its outcomes, so the smoke checker can
  classify them.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import gevent
import requests
from gevent.event import AsyncResult
from locust import events
from locust.env import Environment
from locust.runners import MasterRunner, WorkerRunner
from locust.stats import CSV_STATS_INTERVAL_SEC

from .chat_checks import CONTRACT_VIOLATION_PREFIX, ENVIRONMENT_CONDITION_PREFIX, EnvironmentCondition
from .chat_scenarios import OVERSIZED_BODY_BYTES, ChatModelNames, HourlyBudget, ScenarioGroup, build_oversized_body, parse_group_weights
from .chat_worker_script import ChatPersonaRole
from .config import _config

__all__ = [
    "ChatMixConfigurationError",
    "ChatMixSettings",
    "ChatWorkerLedger",
    "ChatWorkerSlot",
    "IntervalGate",
    "RunTopology",
    "RunnerRole",
    "WorkerSlotClaims",
    "build_run_topology",
    "chat_mix_settings",
    "claim_worker_slot",
    "coverage_for_gate",
    "merge_coverage_reports",
    "fail_contract",
    "fail_environment",
    "find_coverage_gaps",
    "fire_check_failure",
    "record_benign",
    "record_measurement",
]

logger = logging.getLogger(__name__)

METRIC_REQUEST_TYPE: str = "CHAT-METRIC"
"""The request type of the suite's custom measurements, so they sort apart from real requests in the statistics."""

CHECK_REQUEST_TYPE: str = "CHECK"
"""The request type of a failure found across several requests or at test stop rather than on one response."""

COVERAGE_ROW_NAME: str = "[coverage] chat mix"
"""The statistics row a coverage gap fails under."""

DEFAULT_ABORTING_PER_HOUR: int = 2
"""The aborting scenarios a run sends per hour by default, across every persona: 6 aborted jobs."""

DEFAULT_ANONYMOUS_SHARE: float = 0.1
"""The share of the ordinary and Lite front scenarios sent without ``Authorization`` by default."""

DEFAULT_STREAM_ABANDON_CHUNKS: int = 2
"""The relayed content chunks an abandoning streaming client reads before it closes the connection by default. It is
small so the close lands while the worker is still posting progress, which is when the front's cancel settles the
generation on what was posted."""

MODEL_CARD_CHECK_INTERVAL_SECONDS: float = 30.0
"""The shortest time between two model card checks of one process, whatever its number of front clients."""

SLOT_CLAIM_MESSAGE: str = "chat_mix_claim_worker_slot"
"""The message a worker process sends the master to claim a chat worker slot."""

SLOT_GRANT_MESSAGE: str = "chat_mix_worker_slot"
"""The message the master answers a slot claim with."""

SLOT_CLAIM_TIMEOUT_SECONDS: float = 30.0
"""How long a chat worker waits for the master to grant its slot."""

COVERAGE_REPORT_KEY: str = "chat_mix_coverage"
AVAILABILITY_MESSAGE: str = "chat_mix_availability"
AVAILABILITY_REPORT_KEY: str = "chat_mix_online"
AVAILABILITY_TTL_SECONDS: float = 15.0
"""The key a worker process's coverage travels under in its stats report to the master."""

MAX_ABORTING_PER_HOUR: int = 6
"""The most aborting scenarios a run may send per hour: 18 aborted jobs, below the 20 that put a worker into maintenance."""

GROUP_COVERAGE_MINIMUM_RUN_SECONDS: float = 300.0
"""How long a run lasts before a weighted scenario group with no attempt counts as a coverage gap; a shorter run may
simply not have drawn it."""

BACKOFF_CAP_SECONDS: float = 15.0
"""The longest back-off after a benign refusal."""

CSV_WRITE_MARGIN_SECONDS: float = 0.5
"""How much longer than one CSV write interval the stop hook yields, so the writer runs at least once more."""

EXCERPT_CHARACTERS: int = 200
"""How much of a body a failure quotes."""

FAILURE_MESSAGE_CHARACTERS: int = 900
"""How long a failure message may grow."""


class ChatMixConfigurationError(ValueError):
    """The ``--chat-mix-*`` arguments contradict each other or a safety bound."""


@dataclass
class ChatWorkerLedger:
    """Represents what the run's simulated chat workers submitted and had refused, per worker name, for the summary
    logged at test stop beside the Horde's own counters."""

    accepted_replies: Counter[str] = field(default_factory=Counter)
    """Replies the Horde stored."""
    refused_nonconformant: Counter[str] = field(default_factory=Counter)
    """Replies refused as ``ChatSubmitNonconformant``; their faults count as ``nonconformant_replies``."""
    refused_invalid: Counter[str] = field(default_factory=Counter)
    """Replies refused as ``ChatSubmitInvalid``; their faults count as aborted jobs."""
    faults_submitted: Counter[str] = field(default_factory=Counter)
    """Faulted submits sent, scripted or after a refusal."""
    text_jobs: Counter[str] = field(default_factory=Counter)
    """Text jobs served by a chat worker."""
    counters_at_start: dict[str, dict[str, int]] = field(default_factory=dict)
    """The Horde's ``nonconformant_replies`` and ``uncompleted_jobs`` per worker when the run started."""

    def worker_names(self) -> list[str]:
        """Return every worker name the ledger counted anything for, sorted."""
        counted = (self.accepted_replies, self.refused_nonconformant, self.refused_invalid, self.faults_submitted, self.text_jobs)
        return sorted({worker_name for counter in counted for worker_name in counter})


class RunnerRole(StrEnum):
    """Which part of a Locust run this process plays."""

    LOCAL = "local"
    """A single process that runs the users and gates the run."""
    MASTER = "master"
    """The master of a distributed run: runs no users, merges the workers' coverage and gates the run."""
    WORKER = "worker"
    """A worker process of a distributed run: runs its share of the users and reports its coverage to the master."""


@dataclass(frozen=True)
class RunTopology:
    """Represents where this process sits in the run, which decides its worker slots and its share of the budgets."""

    role: RunnerRole
    """The process's part in the run."""
    worker_index: int = 0
    """The worker process's index, from 0, as the master assigns it; 0 for a local run and the master."""
    expected_workers: int = 1
    """How many worker processes the run has (``--expect-workers`` or ``--processes``); 1 for a local run."""


@dataclass(frozen=True)
class ChatWorkerSlot:
    """Represents the identity one simulated chat worker takes: a persona, and a configured name with the key that owns
    it, or none to generate a name under a worker key from the pool."""

    persona: ChatPersonaRole
    """The persona the worker pops as."""
    configured_name: str | None
    """The configured worker name, or None to generate one."""
    api_key: str | None
    """The key that owns ``configured_name``; None with a generated name."""


@dataclass
class ChatMixCoverage:
    """Represents what the run reached, for the coverage gate at test stop.

    In a distributed run each worker process keeps its own and reports it to the master with every stats report
    ([`to_report`][tests.stress.locustsuite.chat_mix_settings.ChatMixCoverage.to_report]); the master merges the latest
    report of each worker ([`merge_coverage_reports`][tests.stress.locustsuite.chat_mix_settings.merge_coverage_reports]).
    Every field is bounded by the run's worker names, scenario groups or stopped workers, never by its request count.
    """

    started_at: float | None = None
    """When the run was configured."""
    assigned_workers: dict[str, str] = field(default_factory=dict)
    """Every simulated chat worker this process started, name to persona."""
    online_workers: set[str] = field(default_factory=set)
    """The workers that popped successfully at least once."""
    group_attempts: Counter[str] = field(default_factory=Counter)
    """Front scenarios sent, by group."""
    completed_front_scenarios: int = 0
    """Front scenarios outside the misuse group whose response matched the expected outcome."""
    stopped_workers: list[str] = field(default_factory=list)
    """Why each worker that stopped itself stopped."""

    def to_report(self) -> dict[str, Any]:
        """Return the coverage as plain JSON-ready values, for a worker's report to the master."""
        return {
            "assigned_workers": dict(self.assigned_workers),
            "online_workers": sorted(self.online_workers),
            "group_attempts": dict(self.group_attempts),
            "completed_front_scenarios": self.completed_front_scenarios,
            "stopped_workers": list(self.stopped_workers),
        }


def merge_coverage_reports(reports: Iterable[Mapping[str, Any]], *, started_at: float | None) -> ChatMixCoverage:
    """Return the coverage of a whole distributed run from each worker process's latest report.

    Args:
        reports: One report per worker process, as ``ChatMixCoverage.to_report`` writes it.
        started_at: When the master configured the run.
    """
    merged = ChatMixCoverage(started_at=started_at)
    for report in reports:
        merged.assigned_workers.update(report.get("assigned_workers") or {})
        merged.online_workers.update(report.get("online_workers") or [])
        merged.group_attempts.update(report.get("group_attempts") or {})
        merged.completed_front_scenarios += int(report.get("completed_front_scenarios") or 0)
        merged.stopped_workers.extend(report.get("stopped_workers") or [])
    return merged


def find_coverage_gaps(
    *,
    coverage: ChatMixCoverage,
    expected_chat_workers: int,
    front_clients: int,
    group_weights: dict[ScenarioGroup, int],
    run_seconds: float,
) -> list[str]:
    """Return what the run failed to exercise; empty when it covered everything it was configured for.

    Args:
        coverage: What the run reached, merged across worker processes in a distributed run.
        expected_chat_workers: How many simulated chat workers the run asked for.
        front_clients: How many front clients the run asked for.
        group_weights: The front's weight per scenario group.
        run_seconds: How long the run lasted.
    """
    gaps = [
        f"the {persona} worker {worker_name} never came online"
        for worker_name, persona in sorted(coverage.assigned_workers.items())
        if worker_name not in coverage.online_workers
    ]
    if len(coverage.assigned_workers) < expected_chat_workers:
        gaps.append(f"only {len(coverage.assigned_workers)} of {expected_chat_workers} chat workers started")
    gaps.extend(coverage.stopped_workers)
    if front_clients <= 0:
        return gaps
    if coverage.completed_front_scenarios == 0:
        gaps.append("no front scenario outside the misuse group completed with its expected outcome")
    if run_seconds > GROUP_COVERAGE_MINIMUM_RUN_SECONDS:
        unattempted = sorted(
            group.value for group, weight in group_weights.items() if weight > 0 and coverage.group_attempts[group.value] == 0
        )
        if unattempted:
            gaps.append(f"weighted scenario groups {unattempted} recorded no attempt in {run_seconds:.0f} seconds")
    return gaps


class ChatMixSettings:
    """Hold the chat mix settings populated at test start from the ``--chat-mix-*`` arguments."""

    def __init__(self) -> None:
        """Create the settings with the defaults the arguments also use."""
        self.front_host: str = ""
        """The front's base URL, without ``/v1`` or a trailing slash."""
        self.horde_host: str = ""
        """The Horde base URL, without ``/api``."""
        self.models: ChatModelNames = ChatModelNames(
            conformant="stress/chat-conformant", limited="stress/chat-limited", flaky="stress/chat-flaky"
        )
        """The model each persona serves."""
        self.topology: RunTopology = RunTopology(role=RunnerRole.LOCAL)
        """Where this process sits in the run."""
        self.worker_names: list[str] = []
        """The configured worker names, one per worker slot of the whole run; empty generates names."""
        self.worker_api_key: str | None = None
        """The key that owns every configured worker name."""
        self.personas: list[ChatPersonaRole] = [ChatPersonaRole.CONFORMANT, ChatPersonaRole.LIMITED, ChatPersonaRole.FLAKY]
        """The personas the run's worker slots take in turn."""
        self.worker_count: int = 0
        """How many simulated chat workers the whole run asks for."""
        self.front_clients: int = 0
        """How many front clients the whole run asks for."""
        self.flaky_rate: float = 0.3
        """The chance the flaky persona breaks an unscripted reply's contract."""
        self.group_weights: dict[ScenarioGroup, int] = {}
        """The front client's weight per scenario group."""
        self.stream_chance: float = 0.5
        """The chance a scenario that may stream is streamed."""
        self.include_usage_chance: float = 0.5
        """The chance a streamed request asks for the usage chunk."""
        self.anonymous_share: float = DEFAULT_ANONYMOUS_SHARE
        """The share of the ordinary and Lite scenarios sent without ``Authorization``."""
        self.fail_fast_share: float = 0.0
        """The share of the front scenarios of ``FAIL_FAST_SHARE_GROUPS`` sent with ``horde.availability_mode:
        fail_fast``."""
        self.stream_abandon_share: float = 0.0
        """The share of the streamed front requests expecting a reply whose client closes the connection mid-stream."""
        self.stream_abandon_chunks: int = DEFAULT_STREAM_ABANDON_CHUNKS
        """The content chunks an abandoning client reads before it closes the connection."""
        self.vision: bool = False
        """Whether the conformant persona declares vision and the vision group runs."""
        self.verify_faults: bool = True
        """Whether a client cross-checks an intended 502 on the native status route."""
        self.gen_time_min_seconds: float = 1.0
        """The shortest simulated generation time."""
        self.gen_time_max_seconds: float = 3.0
        """The longest simulated generation time."""
        self.worker_progress: bool = False
        """Whether a simulated chat worker posts content deltas for a job whose payload carries ``stream_progress``."""
        self.worker_progress_interval_seconds: float = 1.0
        """The time between two progress posts of one job."""
        self.aborting_budget: HourlyBudget = HourlyBudget(DEFAULT_ABORTING_PER_HOUR)
        """The aborting scenarios this process may send per hour; only one process of a run gets any."""
        self.oversized_budget: HourlyBudget = HourlyBudget(2)
        """The oversized bodies this process may send per hour."""
        self.oversized_body_bytes: int = OVERSIZED_BODY_BYTES
        """The size of the oversized scenario's body."""
        self.model_card_checks: IntervalGate = IntervalGate(MODEL_CARD_CHECK_INTERVAL_SECONDS)
        """Spaces the model card checks of this process's front clients, so their number does not grow with the users."""
        self.online_workers: dict[str, tuple[ChatPersonaRole, float]] = {}
        """The workers of this process that are online, name to persona and when they first popped successfully; a worker
        leaves it when it stops or is refused, so scenarios needing its persona pause."""
        self.coverage: ChatMixCoverage = ChatMixCoverage()
        """What this process reached."""
        self.worker_coverage_reports: dict[str, dict[str, Any]] = {}
        self.availability_reports: dict[str, tuple[float, dict[str, float]]] = {}
        self.remote_online_since: dict[ChatPersonaRole, float] = {}
        self.remote_availability_received_at: float = 0.0
        self.availability_runner: Any = None
        """On the master, each worker process's latest coverage report, by Locust client id."""
        self.ledger: ChatWorkerLedger = ChatWorkerLedger()
        """The workers' submit counts."""
        self._next_slot: int = 0
        self._oversized_body: bytes | None = None

    def configure(self, options: Any, *, horde_host: str, topology: RunTopology) -> None:
        """Mutate the settings from Locust's parsed options, refusing contradictory ones.

        Args:
            options: ``environment.parsed_options``; on a worker process, the master's values for every
                ``--chat-mix-*`` option.
            horde_host: The run's ``--host``, the Horde the workers and native clients talk to.
            topology: Where this process sits in the run.

        Raises:
            ChatMixConfigurationError: Front clients without a front host; worker names without the key that owns them;
                fewer names than the run's workers need; an aborting budget or a share out of bounds; a progress
                interval that is not positive; an abandon chunk count below 1.
        """
        self.topology = topology
        self.front_host = options.chat_mix_front_host.rstrip("/").removesuffix("/v1")
        self.horde_host = horde_host.rstrip("/")
        self.models = ChatModelNames(
            conformant=options.chat_mix_model, limited=options.chat_mix_limited_model, flaky=options.chat_mix_flaky_model
        )
        self.worker_names = [worker_name.strip() for worker_name in options.chat_mix_worker_names.split(",") if worker_name.strip()]
        self.worker_api_key = options.chat_mix_worker_api_key or None
        self.personas = [ChatPersonaRole(persona.strip()) for persona in options.chat_mix_worker_personas.split(",") if persona.strip()]
        self.worker_count = max(int(options.chat_mix_workers), 0)
        self.front_clients = max(int(options.chat_mix_front_clients), 0)
        self.flaky_rate = options.chat_mix_flaky_rate
        self.group_weights = parse_group_weights(options.chat_mix_group_weights)
        self.stream_chance = options.chat_mix_stream_chance
        self.include_usage_chance = options.chat_mix_include_usage_chance
        self.anonymous_share = options.chat_mix_anonymous_share
        self.fail_fast_share = options.chat_mix_fail_fast_share
        self.stream_abandon_share = options.chat_mix_stream_abandon_share
        self.stream_abandon_chunks = options.chat_mix_stream_abandon_chunks
        self.vision = options.chat_mix_vision
        self.verify_faults = not options.chat_mix_skip_fault_verify
        self.gen_time_min_seconds = options.chat_mix_gen_time_min
        self.gen_time_max_seconds = options.chat_mix_gen_time_max
        self.oversized_budget = HourlyBudget(options.chat_mix_oversized_per_hour)
        self.worker_progress = options.chat_mix_worker_progress
        self.worker_progress_interval_seconds = options.chat_mix_worker_progress_interval
        if self.worker_progress_interval_seconds <= 0:
            raise ChatMixConfigurationError(
                f"--chat-mix-worker-progress-interval has to be positive; got {self.worker_progress_interval_seconds}."
            )
        for flag, share in (
            ("--chat-mix-anonymous-share", self.anonymous_share),
            ("--chat-mix-fail-fast-share", self.fail_fast_share),
            ("--chat-mix-stream-abandon-share", self.stream_abandon_share),
        ):
            if not 0.0 <= share <= 1.0:
                raise ChatMixConfigurationError(f"{flag} has to be 0 to 1; got {share}.")
        if self.stream_abandon_chunks < 1:
            raise ChatMixConfigurationError(f"--chat-mix-stream-abandon-chunks has to be at least 1; got {self.stream_abandon_chunks}.")
        self._check_worker_identities()
        self._configure_aborting_budget(options.chat_mix_aborting_per_hour)
        if self.front_clients > 0 and not self.front_host:
            raise ChatMixConfigurationError("--chat-mix-front-clients needs --chat-mix-front-host.")
        if not self.vision:
            self.group_weights[ScenarioGroup.VISION] = 0
        self.model_card_checks = IntervalGate(MODEL_CARD_CHECK_INTERVAL_SECONDS)
        self.online_workers = {}
        self.availability_reports = {}
        self.remote_online_since = {}
        self.remote_availability_received_at = 0.0
        self.coverage = ChatMixCoverage(started_at=time.time())
        self.worker_coverage_reports = {}
        self.ledger = ChatWorkerLedger()
        self._next_slot = 0

    def _check_worker_identities(self) -> None:
        """Refuse worker settings that would pop under a name the key does not own or share a name between workers.

        Raises:
            ChatMixConfigurationError: As ``configure`` documents.
        """
        if not self.personas:
            raise ChatMixConfigurationError("--chat-mix-worker-personas names no persona.")
        if not self.worker_names or self.worker_count == 0:
            return
        if self.worker_api_key is None:
            raise ChatMixConfigurationError("--chat-mix-worker-names needs --chat-mix-worker-api-key, the key that owns every one of them.")
        if len(self.worker_names) < self.worker_count:
            raise ChatMixConfigurationError(
                f"--chat-mix-workers {self.worker_count} needs as many --chat-mix-worker-names, one per worker in the whole run; "
                f"{len(self.worker_names)} are given.",
            )
        active_names = self.worker_names[: self.worker_count]
        if len(set(active_names)) != len(active_names):
            raise ChatMixConfigurationError("--chat-mix-worker-names must give each active worker a unique name.")

    def _configure_aborting_budget(self, aborting_per_hour: int) -> None:
        """Set the aborting budget: the whole of it in a local run and on worker process 0, none on the others, so the
        run's total stays the configured hourly total.

        Raises:
            ChatMixConfigurationError: The budget is negative or above ``MAX_ABORTING_PER_HOUR``.
        """
        if not 0 <= aborting_per_hour <= MAX_ABORTING_PER_HOUR:
            raise ChatMixConfigurationError(
                f"--chat-mix-aborting-per-hour has to be 0 to {MAX_ABORTING_PER_HOUR}; got {aborting_per_hour}."
            )
        if self.topology.role == RunnerRole.WORKER and self.topology.worker_index != 0:
            logger.info("Chat mix: worker process %d skips the aborting scenarios; worker process 0 runs them.", self.topology.worker_index)
            self.aborting_budget = HourlyBudget(0)
            return
        self.aborting_budget = HourlyBudget(aborting_per_hour)

    def allocate_slot_index(self) -> int:
        """Return the next worker slot of the run and count it taken: in a local run for this process's next worker, on
        the master for the next worker process that claims one."""
        slot_index = self._next_slot
        self._next_slot += 1
        return slot_index

    def slot_at(self, slot_index: int) -> ChatWorkerSlot | None:
        """Return the identity of a run-wide worker slot, or None past the workers the run asks for.

        Slot ``n`` takes the name at ``n`` and persona ``n`` modulo the personas, so a name always pops as the same
        persona, whichever process holds it.

        Args:
            slot_index: The slot, from 0, in the order the run hands slots out.
        """
        if slot_index >= self.worker_count:
            return None
        persona = self.personas[slot_index % len(self.personas)]
        if not self.worker_names:
            return ChatWorkerSlot(persona=persona, configured_name=None, api_key=None)
        return ChatWorkerSlot(persona=persona, configured_name=self.worker_names[slot_index], api_key=self.worker_api_key)

    def mark_worker_started(self, worker_name: str, persona: ChatPersonaRole) -> None:
        """Mutate the coverage: a worker took its slot."""
        self.coverage.assigned_workers[worker_name] = persona.value

    def mark_worker_online(self, worker_name: str, persona: ChatPersonaRole) -> None:
        """Mutate the online workers: the worker popped successfully."""
        self.online_workers.setdefault(worker_name, (persona, time.time()))
        self.coverage.online_workers.add(worker_name)

    def mark_worker_offline(self, worker_name: str) -> None:
        """Mutate the online workers: the worker stopped or was refused."""
        self.online_workers.pop(worker_name, None)

    def is_persona_online(self, persona: ChatPersonaRole | None) -> bool:
        """Return whether the persona is online anywhere in the run; True for a scenario that needs none."""
        return persona is None or persona in self.persona_online_since()

    def persona_online_since(self) -> dict[ChatPersonaRole, float]:
        """Return, for each persona with an online worker, when its earliest online worker first popped."""
        online_since: dict[ChatPersonaRole, float] = {}
        for persona, since in self.online_workers.values():
            online_since[persona] = min(since, online_since.get(persona, since))
        if time.time() - self.remote_availability_received_at < AVAILABILITY_TTL_SECONDS:
            for persona, since in self.remote_online_since.items():
                online_since[persona] = min(since, online_since.get(persona, since))
        return online_since

    def model_of(self, persona: ChatPersonaRole) -> str:
        """Return the model a persona serves."""
        if persona == ChatPersonaRole.LIMITED:
            return self.models.limited
        if persona == ChatPersonaRole.FLAKY:
            return self.models.flaky
        return self.models.conformant

    def oversized_body(self) -> bytes:
        """Return the body the oversized scenario sends, built once per process: it is tens of megabytes."""
        if self._oversized_body is None or len(self._oversized_body) != self.oversized_body_bytes:
            self._oversized_body = build_oversized_body(self.models.conformant, self.oversized_body_bytes)
        return self._oversized_body


class IntervalGate:
    """Let an action through at most once per interval, process-wide, so a check every user could make runs once.

    Thread Safety:
        Not locked; Locust users run as greenlets on one thread and never yield inside a call.
    """

    def __init__(self, interval_seconds: float) -> None:
        """Create a gate that is open at once and then once per ``interval_seconds``."""
        self.interval_seconds: float = interval_seconds
        """The shortest time between two actions."""
        self._last_opened_at: float | None = None

    def try_open(self, now: float | None = None) -> bool:
        """Return True and start a new interval when the last action is at least an interval ago, else False.

        Args:
            now: The current time in seconds; the wall clock when omitted.
        """
        current_time = time.time() if now is None else now
        if self._last_opened_at is not None and current_time - self._last_opened_at < self.interval_seconds:
            return False
        self._last_opened_at = current_time
        return True


chat_mix_settings = ChatMixSettings()
"""The process-wide chat mix settings the users read."""


def fire_row(environment: Environment, *, request_type: str, name: str, response_time_ms: float) -> None:
    """Record one successful statistics row that is not an HTTP request of its own: a benign outcome or a measurement."""
    environment.events.request.fire(
        request_type=request_type,
        name=name,
        response_time=response_time_ms,
        response_length=0,
        exception=None,
        context={},
    )


def fire_check_failure(environment: Environment, *, name: str, message: str) -> None:
    """Record a failure found across several requests or at test stop, on a ``CHECK`` row of its own."""
    environment.events.request.fire(
        request_type=CHECK_REQUEST_TYPE,
        name=name,
        response_time=0,
        response_length=0,
        exception=Exception(message[:FAILURE_MESSAGE_CHARACTERS]),
        context={},
    )


def record_measurement(environment: Environment, name: str, seconds: float | None) -> None:
    """Record a duration as a ``CHAT-METRIC`` row, so its percentiles sit in the statistics CSV; None records nothing."""
    if seconds is None or seconds < 0:
        return
    fire_row(environment, request_type=METRIC_REQUEST_TYPE, name=name, response_time_ms=seconds * 1000)


def record_benign(environment: Environment, resp: Any, name: str, label: str) -> None:
    """Mark a response successful and record it under ``<name> [benign <label>]``."""
    resp.success()
    fire_row(
        environment,
        request_type=resp.request.method or "POST",
        name=f"{name} [benign {label}]",
        response_time_ms=resp.elapsed.total_seconds() * 1000,
    )


def fail_contract(resp: Any, subject: str, violations: list[str]) -> None:
    """Fail a response with the contract violation message the smoke checker gates on."""
    resp.failure(
        f"{CONTRACT_VIOLATION_PREFIX} [{subject}] status {resp.status_code}: {'; '.join(violations[:4])}"[:FAILURE_MESSAGE_CHARACTERS]
    )


def fail_environment(resp: Any, condition: EnvironmentCondition, subject: str) -> None:
    """Fail a response with the environment condition message the smoke checker reports apart and gates on."""
    excerpt = (resp.text or "")[:EXCERPT_CHARACTERS]
    resp.failure(
        f"{ENVIRONMENT_CONDITION_PREFIX} [{condition.value}] [{subject}] status {resp.status_code}: {excerpt}"[:FAILURE_MESSAGE_CHARACTERS]
    )


def back_off(resp: Any) -> None:
    """Wait the response's ``Retry-After``, or a few seconds, capped."""
    retry_after = resp.headers.get("Retry-After")
    try:
        backoff_seconds = float(retry_after) if retry_after else random.uniform(2.0, 6.0)
    except ValueError:
        backoff_seconds = random.uniform(2.0, 6.0)
    time.sleep(min(backoff_seconds, BACKOFF_CAP_SECONDS))


def read_worker_counters(worker_name: str) -> dict[str, int] | None:
    """Return the Horde's refusal counters of a worker, or None when it is unknown or unreachable."""
    if not chat_mix_settings.horde_host:
        return None
    try:
        response = requests.get(
            f"{chat_mix_settings.horde_host}/api/v2/workers/name/{worker_name}",
            headers={"Client-Agent": _config.get("client_agent", "aihorde_stress_test")},
            timeout=10,
        )
    except requests.RequestException as error:
        logger.warning("Could not read worker %s's counters: %s", worker_name, error)
        return None
    if response.status_code != 200:
        return None
    details = response.json()
    return {counter: int(details.get(counter) or 0) for counter in ("nonconformant_replies", "uncompleted_jobs")}


@events.init_command_line_parser.add_listener
def _add_chat_mix_arguments(parser: Any) -> None:
    group = parser.add_argument_group("AI Horde Stress Test - chat mix (opt-in)")
    for flag, env_var, default, help_text in (
        (
            "--chat-mix-front-clients",
            "HORDE_CHAT_MIX_FRONT_CLIENTS",
            0,
            "ChatMixFrontClient users (front contract scenarios); 0 leaves them out",
        ),
        (
            "--chat-mix-native-clients",
            "HORDE_CHAT_MIX_NATIVE_CLIENTS",
            0,
            "ChatMixNativeClient users (native chat routes); 0 leaves them out",
        ),
        ("--chat-mix-workers", "HORDE_CHAT_MIX_WORKERS", 0, "ChatMixWorker users (simulated chat workers); 0 leaves them out"),
        (
            "--chat-mix-aborting-per-hour",
            "HORDE_CHAT_MIX_ABORTING_PER_HOUR",
            DEFAULT_ABORTING_PER_HOUR,
            f"Aborting scenarios per hour in the whole run (3 aborted jobs each), sent by one process; 0 to {MAX_ABORTING_PER_HOUR}",
        ),
        ("--chat-mix-oversized-per-hour", "HORDE_CHAT_MIX_OVERSIZED_PER_HOUR", 2, "Oversized (36 MiB + 1 byte) bodies per hour"),
    ):
        group.add_argument(flag, type=int, env_var=env_var, default=default, help=help_text)
    group.add_argument(
        "--chat-mix-front-host", type=str, env_var="HORDE_CHAT_MIX_FRONT_HOST", default="", help="Front base URL, with or without /v1"
    )
    group.add_argument(
        "--chat-mix-worker-names",
        type=str,
        env_var="HORDE_CHAT_MIX_WORKER_NAMES",
        default="",
        help="Comma-separated worker names, one per chat worker slot of the whole run, all owned by --chat-mix-worker-api-key; "
        "empty generates names under the worker key pool",
    )
    group.add_argument(
        "--chat-mix-worker-api-key",
        type=str,
        env_var="HORDE_CHAT_MIX_WORKER_API_KEY",
        default="",
        help="The key that owns every --chat-mix-worker-names name; required with them",
    )
    group.add_argument(
        "--chat-mix-worker-personas",
        type=str,
        env_var="HORDE_CHAT_MIX_WORKER_PERSONAS",
        default="conformant,limited,flaky",
        help="Comma-separated personas (conformant, limited, flaky), one per chat worker of each Locust process, in spawn order",
    )
    group.add_argument(
        "--chat-mix-model",
        type=str,
        env_var="HORDE_CHAT_MIX_MODEL",
        default="stress/chat-conformant",
        help="Model the conformant persona serves",
    )
    group.add_argument(
        "--chat-mix-limited-model",
        type=str,
        env_var="HORDE_CHAT_MIX_LIMITED_MODEL",
        default="stress/chat-limited",
        help="Model the limited persona serves",
    )
    group.add_argument(
        "--chat-mix-flaky-model",
        type=str,
        env_var="HORDE_CHAT_MIX_FLAKY_MODEL",
        default="stress/chat-flaky",
        help="Model the flaky persona serves",
    )
    group.add_argument(
        "--chat-mix-flaky-rate",
        type=float,
        env_var="HORDE_CHAT_MIX_FLAKY_RATE",
        default=0.3,
        help="Chance the flaky persona breaks an unscripted reply",
    )
    group.add_argument(
        "--chat-mix-group-weights",
        type=str,
        env_var="HORDE_CHAT_MIX_GROUP_WEIGHTS",
        default="ordinary=6,tools=4,structured=3,conformance=2,gap=2,misuse=3,lite=2,vision=1",
        help="Front scenario group weights, group=weight pairs; an omitted group weighs 0",
    )
    group.add_argument(
        "--chat-mix-stream-chance", type=float, env_var="HORDE_CHAT_MIX_STREAM_CHANCE", default=0.5, help="Chance a scenario is streamed"
    )
    group.add_argument(
        "--chat-mix-anonymous-share",
        type=float,
        env_var="HORDE_CHAT_MIX_ANONYMOUS_SHARE",
        default=DEFAULT_ANONYMOUS_SHARE,
        help="Share (0 to 1) of the ordinary and Lite front scenarios sent without Authorization, as anonymous users",
    )
    group.add_argument(
        "--chat-mix-fail-fast-share",
        type=float,
        env_var="HORDE_CHAT_MIX_FAIL_FAST_SHARE",
        default=0.0,
        help="Share (0 to 1) of the queued front scenarios sent with horde.availability_mode fail_fast; "
        "their 503 queue_unavailable refusals are benign",
    )
    group.add_argument(
        "--chat-mix-stream-abandon-share",
        type=float,
        env_var="HORDE_CHAT_MIX_STREAM_ABANDON_SHARE",
        default=0.0,
        help="Share (0 to 1) of the streamed front requests expecting a reply whose client closes the connection after "
        "--chat-mix-stream-abandon-chunks content chunks",
    )
    group.add_argument(
        "--chat-mix-stream-abandon-chunks",
        type=int,
        env_var="HORDE_CHAT_MIX_STREAM_ABANDON_CHUNKS",
        default=DEFAULT_STREAM_ABANDON_CHUNKS,
        help="Content chunks an abandoning streaming client reads before it closes the connection",
    )
    group.add_argument(
        "--chat-mix-include-usage-chance",
        type=float,
        env_var="HORDE_CHAT_MIX_INCLUDE_USAGE_CHANCE",
        default=0.5,
        help="Chance a streamed request asks for the usage chunk",
    )
    group.add_argument(
        "--chat-mix-gen-time-min", type=float, env_var="HORDE_CHAT_MIX_GEN_TIME_MIN", default=1.0, help="Shortest simulated chat generation"
    )
    group.add_argument(
        "--chat-mix-gen-time-max", type=float, env_var="HORDE_CHAT_MIX_GEN_TIME_MAX", default=3.0, help="Longest simulated chat generation"
    )
    group.add_argument(
        "--chat-mix-vision",
        action="store_true",
        env_var="HORDE_CHAT_MIX_VISION",
        default=False,
        help="The conformant persona declares vision with the base64 transport and the vision group runs",
    )
    group.add_argument(
        "--chat-mix-skip-fault-verify",
        action="store_true",
        env_var="HORDE_CHAT_MIX_SKIP_FAULT_VERIFY",
        default=False,
        help="Do not cross-check an intended 502 on the native status route",
    )
    group.add_argument(
        "--chat-mix-worker-progress",
        action="store_true",
        env_var="HORDE_CHAT_MIX_WORKER_PROGRESS",
        default=False,
        help="Chat workers post a conforming reply as content deltas to /api/v2/generate/text/progress for jobs whose client streams",
    )
    group.add_argument(
        "--chat-mix-worker-progress-interval",
        type=float,
        env_var="HORDE_CHAT_MIX_WORKER_PROGRESS_INTERVAL",
        default=1.0,
        help="Seconds between two progress posts of one job; a job posts one delta per interval of its generation time",
    )


def build_run_topology(environment: Environment) -> RunTopology:
    """Return where this process sits in the run, from its Locust runner.

    A worker learns its index from the master's acknowledgement, before the spawn message that fires the test start, and
    ``--expect-workers`` (set by ``--processes`` too) reaches it with the master's other options.
    """
    options = environment.parsed_options
    expected_workers = max(int(getattr(options, "expect_workers", 1) or 1), 1)
    if isinstance(environment.runner, MasterRunner):
        return RunTopology(role=RunnerRole.MASTER, expected_workers=expected_workers)
    if isinstance(environment.runner, WorkerRunner):
        return RunTopology(role=RunnerRole.WORKER, worker_index=max(environment.runner.worker_index, 0), expected_workers=expected_workers)
    return RunTopology(role=RunnerRole.LOCAL)


@events.test_start.add_listener
def _configure_chat_mix(environment: Environment, **_kwargs: Any) -> None:
    """Fill the chat mix settings from the arguments, stopping the run when they contradict each other.

    The counts were already pinned, and classes with a count of zero left out, by ``events.on_test_start``. On a worker
    process the ``--chat-mix-*`` options are the master's: Locust sends the master's custom options with every spawn
    message and they replace the worker's own command line and environment values.

    Raises:
        ChatMixConfigurationError: As ``ChatMixSettings.configure`` documents, after asking the runner to quit.
    """
    options = environment.parsed_options
    if options is None:
        return
    topology = build_run_topology(environment)
    try:
        chat_mix_settings.configure(options, horde_host=environment.host or "", topology=topology)
    except ChatMixConfigurationError as error:
        # Quitting the runner can end this greenlet before the error propagates, so log it and set the exit code first.
        logger.error("Chat mix configuration refused: %s", error)
        environment.process_exit_code = 1
        if environment.runner is not None:
            environment.runner.quit()
        raise
    if chat_mix_settings.worker_count:
        logger.info(
            "Chat mix %s %d: %d chat worker slots, personas %s in turn, names %s, progress posts %s",
            topology.role.value,
            topology.worker_index,
            chat_mix_settings.worker_count,
            ",".join(persona.value for persona in chat_mix_settings.personas),
            ",".join(chat_mix_settings.worker_names) or "<generated>",
            f"every {chat_mix_settings.worker_progress_interval_seconds} s" if chat_mix_settings.worker_progress else "off",
        )


class WorkerSlotClaims:
    """Claim run-wide worker slots from the master, on a worker process.

    Locust's dispatcher spreads a class's fixed count over the worker processes unevenly (six chat workers over two
    processes can land four and two), so a process cannot know ahead which slots are its own. Each chat worker therefore
    claims its slot from the master when it starts, and the master hands slots out in order, so no two workers in the
    run share a name.

    Thread Safety:
        Claims wait on gevent results; the runner's message greenlet resolves them.
    """

    def __init__(self) -> None:
        """Create the claims with none pending."""
        self._pending: dict[str, AsyncResult] = {}

    def claim(self, runner: Any, *, timeout_seconds: float = SLOT_CLAIM_TIMEOUT_SECONDS) -> int | None:
        """Return the slot the master grants this claim, or None when no answer came within ``timeout_seconds``.

        Args:
            runner: The worker's runner, which sends the claim.
            timeout_seconds: How long to wait for the master's answer.
        """
        claim_id = uuid.uuid4().hex
        result: AsyncResult = AsyncResult()
        self._pending[claim_id] = result
        try:
            runner.send_message(SLOT_CLAIM_MESSAGE, {"claim_id": claim_id})
            slot_index: int = result.get(timeout=timeout_seconds)
            return slot_index
        except gevent.Timeout:
            logger.error("Chat mix: the master did not grant a worker slot within %s seconds.", timeout_seconds)
            return None
        finally:
            self._pending.pop(claim_id, None)

    def resolve(self, claim_id: str, slot_index: int) -> None:
        """Hand a granted slot to the claim waiting for it; a claim that already gave up is ignored."""
        pending = self._pending.get(claim_id)
        if pending is not None:
            pending.set(slot_index)


worker_slot_claims = WorkerSlotClaims()
"""The worker process's pending slot claims."""


def claim_worker_slot(environment: Environment) -> ChatWorkerSlot | None:
    """Return the identity of a new chat worker of this process: a slot from the master on a worker process, the next
    local slot otherwise; None when the run has no slot left or the master did not answer.

    Args:
        environment: The Locust environment.
    """
    if chat_mix_settings.topology.role == RunnerRole.WORKER:
        slot_index = worker_slot_claims.claim(environment.runner)
        return None if slot_index is None else chat_mix_settings.slot_at(slot_index)
    return chat_mix_settings.slot_at(chat_mix_settings.allocate_slot_index())


def _grant_worker_slot(environment: Environment, msg: Any, **_kwargs: Any) -> None:
    """On the master, answer a worker process's slot claim with the run's next slot."""
    slot_index = chat_mix_settings.allocate_slot_index()
    environment.runner.send_message(SLOT_GRANT_MESSAGE, {"claim_id": msg.data["claim_id"], "slot": slot_index}, client_id=msg.node_id)


def _receive_worker_slot(environment: Environment, msg: Any, **_kwargs: Any) -> None:
    """On a worker process, hand the master's granted slot to the claim waiting for it."""
    worker_slot_claims.resolve(msg.data["claim_id"], int(msg.data["slot"]))


@events.init.add_listener
def _register_slot_messages(environment: Environment, **_kwargs: Any) -> None:
    """Register the slot claim messages: the master grants slots, a worker process receives them."""
    chat_mix_settings.availability_runner = environment.runner
    if isinstance(environment.runner, MasterRunner):
        environment.runner.register_message(SLOT_CLAIM_MESSAGE, _grant_worker_slot)
    elif isinstance(environment.runner, WorkerRunner):
        environment.runner.register_message(SLOT_GRANT_MESSAGE, _receive_worker_slot)
        environment.runner.register_message(AVAILABILITY_MESSAGE, _receive_availability)


def _receive_availability(environment: Environment, msg: Any, **_kwargs: Any) -> None:
    """Replace the run's current availability on a client process, including removals."""
    chat_mix_settings.remote_online_since = {ChatPersonaRole(persona): since for persona, since in msg.data.items()}
    chat_mix_settings.remote_availability_received_at = time.time()


@events.report_to_master.add_listener
def _report_chat_mix_coverage(client_id: str, data: dict[str, Any], **_kwargs: Any) -> None:
    """Add this worker process's coverage to its stats report, so the master can gate the whole run.

    Every report carries the whole coverage, not a delta, so the master keeps only the latest one per worker. The stats
    report rides the runner's own message channel every few seconds; a message sent at the worker's test stop would
    arrive after the master's own test stop when the run ends by ``--run-time``, since the master stops first then.
    """
    if chat_mix_settings.coverage.started_at is not None:
        data[COVERAGE_REPORT_KEY] = chat_mix_settings.coverage.to_report()
        # Historical coverage deliberately keeps stopped workers; availability must describe only current workers.
        current: dict[str, float] = {}
        for persona, since in chat_mix_settings.online_workers.values():
            current[persona.value] = min(since, current.get(persona.value, since))
        data[AVAILABILITY_REPORT_KEY] = current


@events.worker_report.add_listener
def _receive_chat_mix_coverage(client_id: str, data: dict[str, Any], **_kwargs: Any) -> None:
    """Keep each worker process's latest coverage report on the master."""
    report = data.get(COVERAGE_REPORT_KEY)
    if isinstance(report, dict):
        chat_mix_settings.worker_coverage_reports[client_id] = report
    online = data.get(AVAILABILITY_REPORT_KEY)
    runner = chat_mix_settings.availability_runner
    if isinstance(online, dict) and runner is not None:
        now = time.time()
        chat_mix_settings.availability_reports[client_id] = (now, online)
        merged: dict[str, float] = {}
        for node_id, (reported_at, personas) in list(chat_mix_settings.availability_reports.items()):
            if node_id not in runner.clients or now - reported_at >= AVAILABILITY_TTL_SECONDS:
                del chat_mix_settings.availability_reports[node_id]
                continue
            for persona, since in personas.items():
                merged[persona] = min(since, merged.get(persona, since))
        runner.send_message(AVAILABILITY_MESSAGE, merged)


def coverage_for_gate(settings: ChatMixSettings) -> ChatMixCoverage:
    """Return the coverage the gate judges: this process's own in a local run, the workers' merged reports on a master.

    Args:
        settings: The chat mix settings of this process.
    """
    if settings.topology.role == RunnerRole.MASTER:
        return merge_coverage_reports(settings.worker_coverage_reports.values(), started_at=settings.coverage.started_at)
    return settings.coverage


@events.test_stop.add_listener
def _close_chat_mix(environment: Environment, **_kwargs: Any) -> None:
    """Log the workers' submits beside the Horde's counters, and fail the run on any coverage gap.

    The gate runs where the run's statistics are written: in a local run, and on the master of a distributed one, which
    merges the workers' reports. A worker process does not gate.
    """
    ledger = chat_mix_settings.ledger
    for worker_name in ledger.worker_names():
        counters_now = read_worker_counters(worker_name) or {}
        counters_then = ledger.counters_at_start.get(worker_name, {})
        logger.info(
            "Chat mix worker %s: accepted=%d refused_nonconformant=%d refused_invalid=%d faults=%d text_jobs=%d; "
            "Horde nonconformant_replies %s->%s uncompleted_jobs %s->%s",
            worker_name,
            ledger.accepted_replies[worker_name],
            ledger.refused_nonconformant[worker_name],
            ledger.refused_invalid[worker_name],
            ledger.faults_submitted[worker_name],
            ledger.text_jobs[worker_name],
            counters_then.get("nonconformant_replies"),
            counters_now.get("nonconformant_replies"),
            counters_then.get("uncompleted_jobs"),
            counters_now.get("uncompleted_jobs"),
        )
    if chat_mix_settings.topology.role == RunnerRole.WORKER or chat_mix_settings.coverage.started_at is None:
        return
    if not (chat_mix_settings.worker_count or chat_mix_settings.front_clients):
        return
    coverage = coverage_for_gate(chat_mix_settings)
    gaps = find_coverage_gaps(
        coverage=coverage,
        expected_chat_workers=chat_mix_settings.worker_count,
        front_clients=chat_mix_settings.front_clients,
        group_weights=chat_mix_settings.group_weights,
        run_seconds=time.time() - chat_mix_settings.coverage.started_at,
    )
    for gap in gaps:
        logger.error("Chat mix coverage gap: %s", gap)
        fire_check_failure(environment, name=COVERAGE_ROW_NAME, message=f"{CONTRACT_VIOLATION_PREFIX} [coverage]: {gap}")
    if gaps:
        # Locust's CSV writer greenlet rewrites the files every CSV_STATS_INTERVAL_SEC and gets no further turn once
        # shutdown continues, so yield long enough for it to write the coverage failures the smoke checker reads.
        gevent.sleep(CSV_STATS_INTERVAL_SEC + CSV_WRITE_MARGIN_SECONDS)
    logger.info("Chat mix coverage: %s", json.dumps(coverage.to_report()))
