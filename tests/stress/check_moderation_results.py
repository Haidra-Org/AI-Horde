# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gate a moderation evidence run on what the backend recorded, not only on how each response looked.

Locust judges each response alone: a 400 ``CorruptPrompt`` or a 403 ``TimeoutIP`` counts as expected whatever the
backend did with it. The promises of the evidence path are about state: every rejection is captured, text moves to
object storage and matches its digest, the model-rejection timeout engages after the threshold and holds, moderators
are exempt. ``locustfile_moderation.py`` records the run's ground truth (every flood request, text fetch and probe) to
JSONL; this checker reads it, gathers the run's events from the moderator listing, and compares the two.

Check groups:

- A (always): the moderator API, the only interface that must be reachable on every deployment.
- B (``--pg-dsn``): the ``prompt_moderation_events`` rows directly.
- C (``--evidence-endpoint`` and ``--evidence-bucket``): the ``evidence/`` objects in the bucket.
- D (``--mimir-url``): the applier and countermeasure metrics over the run window.

Each check prints PASS or FAIL with its numbers; the exit status is 1 when any check fails. The run's events are those
with an id above the ``first_event_id`` the driver recorded at test start, so other traffic in the same window is
counted too; ``--allow-other-traffic`` turns the count equalities into lower bounds for that case.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

MAX_EVIDENCE_CHARACTERS = 16000
"""Mirror of ``horde.database.prompt_moderation.MAX_EVIDENCE_CHARACTERS``; the checker runs without the horde package."""

EVIDENCE_UPLOAD_BATCH_SIZE = 50
"""Mirror of ``horde.database.prompt_moderation.EVIDENCE_UPLOAD_BATCH_SIZE``, the most rows one upload cycle moves."""

LISTING_PATH = "/api/v2/operations/moderation/prompts"
"""The moderator event listing."""

LISTING_PAGE_SIZE = 100
"""The listing's largest page, so a long run is gathered in the fewest rate-limited requests."""

CLIENT_AGENT = "aihorde_moderation_checker:1.0.0:(discord)stress_tester"
"""Client-Agent of the checker's own requests, so they are told apart from the run's in the server logs."""

HTTP_TIMEOUT_SECONDS = 30.0
"""Bound on each request the checker makes, so an unreachable service fails its check instead of hanging."""

RATE_LIMIT_RETRIES = 6
"""Times a rate-limited listing request is retried; moderator reads are 120 a minute per key."""

RATE_LIMIT_MAX_SLEEP_SECONDS = 30.0
"""Longest wait honored from a ``Retry-After`` header."""

METRIC_WINDOW_MARGIN_SECONDS = 60
"""Margin added to each side of the run window in metric queries, covering scrape and export intervals."""

DETAIL_SAMPLE = 5
"""Most offending ids or records quoted in one check's detail."""

FRESH_TIMEOUT_WINDOW_SECONDS = 2.0
"""A 403 sent this soon after a rejection returned reports nearly the full timeout it set."""

STAGE_KEYS = frozenset(
    {
        "submitted_prompt",
        "moderation_prompt",
        "effective_prompt",
    },
)
"""The keys of a stored evidence object and the stage fields of a listed event."""

TEXT_STATES = frozenset(
    {
        "pending",
        "stored",
        "none",
    },
)
"""The values ``text_state`` may take (``ck_prompt_moderation_text_state``)."""

REJECTION_RC = "CorruptPrompt"
"""Return code of a rejected prompt."""

TIMEOUT_RC = "TimeoutIP"
"""Return code of a request refused because its address is timed out."""

MODEL_PATHS = frozenset(
    {
        "model",
        "oversized",
    },
)
"""Flood paths that are model rejections: the prober's oversized prompt uses the model token, so it counts too."""

PROBE_EXPECTED_STATUSES: dict[str, frozenset[int]] = {
    # A request without an apikey header fails request parsing (400) before any authorization check runs.
    "no_key": frozenset({400, 401, 403}),
    "non_moderator": frozenset({401, 403}),
    "limit_101": frozenset({400, 429}),
    "tampered_link": frozenset({403}),
    "listing": frozenset({200, 429}),
}
"""Statuses each prober request may receive; 429 is allowed where the moderator key's read limit can apply."""

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
"""A lowercase hexadecimal SHA-256 digest."""

OBJECT_KEY_PATTERN = re.compile(r"^evidence/(\d+)\.json$")
"""An evidence object key (``evidence_object_key``)."""

SERVER_ERROR_PATTERN = re.compile(r"(?:Status |Server error: |HTTPError\(')5\d\d")
"""A 5xx status in a Locust failure message, as the suite's users and Locust itself word it."""

FLOOD_REQUEST_NAMES = frozenset(
    {
        "/api/v2/generate/async [model-rejection]",
        "/api/v2/generate/async [filter-rejection]",
    },
)
"""Locust request names of the flooder's requests in the stats CSV."""


@dataclass
class Check:
    """One check outcome: its code, whether it passed, and the numbers behind it."""

    code: str
    passed: bool
    detail: str


@dataclass
class RunBounds:
    """The run's event id floor and its wall-clock window, from the driver's start and stop records."""

    first_event_id: int
    start: float
    stop: float
    labels: list[str]
    errors: list[str]


def _sample_ids(ids: Iterable[object]) -> str:
    listed = list(ids)
    head = ", ".join(str(i) for i in listed[:DETAIL_SAMPLE])
    return head + (f" (+{len(listed) - DETAIL_SAMPLE} more)" if len(listed) > DETAIL_SAMPLE else "")


def _compare(sent: int, recorded: int, allow_other: bool) -> bool:
    return recorded >= sent if allow_other else recorded == sent


def read_evidence(paths: list[Path]) -> list[dict[str, Any]]:
    """Return every record of the evidence files, one file per load-generating host."""
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"Evidence file not found: {path}")
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    records.append(json.loads(line))
    return records


def run_bounds(records: list[dict[str, Any]]) -> RunBounds:
    """Combine the hosts' start and stop records into one id floor and one window.

    The floor is the lowest ``first_event_id``, so an event any host's run could have caused is counted; a host that
    saw an empty listing floors it at 0.
    """
    starts = [r for r in records if r.get("kind") == "run_start"]
    stops = [r for r in records if r.get("kind") == "run_stop"]
    errors = [f"{r.get('address_label')}: {r['first_event_error']}" for r in starts if r.get("first_event_error")]
    if not starts:
        errors.append("no run_start record; the evidence file is not from locustfile_moderation.py")
    floors = [int(r["first_event_id"]) if r.get("first_event_id") is not None else 0 for r in starts]
    timestamps = [float(r["ts"]) for r in records if "ts" in r]
    start = min((float(r["ts"]) for r in starts), default=min(timestamps, default=time.time()))
    stop = max((float(r["ts"]) for r in stops), default=max(timestamps, default=time.time()))
    if starts and len(stops) < len(starts):
        errors.append(f"{len(starts) - len(stops)} host(s) wrote no run_stop record; the run window ends at the last record")
    return RunBounds(
        first_event_id=min(floors, default=0),
        start=start,
        stop=stop,
        labels=sorted({str(r.get("address_label")) for r in starts}),
        errors=errors,
    )


class ModeratorApi:
    """The moderator listing, retried on rate limits."""

    def __init__(self, host: str, api_key: str) -> None:
        self.host = host.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": api_key,
                "Client-Agent": CLIENT_AGENT,
            },
        )

    def listing(self, params: dict[str, object]) -> dict[str, Any]:
        """Return one listing page, waiting out rate limits."""
        for _ in range(RATE_LIMIT_RETRIES):
            resp = self.session.get(self.host + LISTING_PATH, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            if resp.status_code == 429:
                time.sleep(min(float(resp.headers.get("Retry-After") or 5.0), RATE_LIMIT_MAX_SLEEP_SECONDS))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"listing still rate limited after {RATE_LIMIT_RETRIES} attempts")

    def run_events(self, first_event_id: int) -> list[dict[str, Any]]:
        """Return the events with an id above ``first_event_id``, newest first, paging by ``before_id``."""
        events: list[dict[str, Any]] = []
        params: dict[str, object] = {"limit": LISTING_PAGE_SIZE}
        while True:
            page = self.listing(params)
            for event in page["events"]:
                if int(event["id"]) <= first_event_id:
                    return events
                events.append(event)
            if page.get("next_cursor") is None:
                return events
            params = {"limit": LISTING_PAGE_SIZE, "before_id": page["next_cursor"]}

    def fresh_event(self, event_id: int) -> dict[str, Any] | None:
        """Return one event with a newly signed ``text_url``, for a link that expired while the checker ran."""
        events = self.listing({"limit": 1, "event_id": event_id})["events"]
        return events[0] if events else None


def _created_epoch(event: dict[str, Any]) -> float:
    created = datetime.fromisoformat(str(event["created"]))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.timestamp()


# ---------------------------------------------------------------------------
# A: API checks
# ---------------------------------------------------------------------------


def check_capture(floods: list[dict[str, Any]], events: list[dict[str, Any]], allow_other: bool) -> list[Check]:
    """A1: every rejected request the run sent is an event, per reason."""
    rejected = [f for f in floods if f["status"] == 400 and f.get("rc") == REJECTION_RC]
    sent = {
        "model_rejection": sum(1 for f in rejected if f["path"] in MODEL_PATHS),
        "filter_rejection": sum(1 for f in rejected if f["path"] == "filter"),
    }
    recorded = Counter(e["reason"] for e in events)
    relation = ">=" if allow_other else "=="
    total_sent = sum(sent.values())
    total_recorded = recorded["model_rejection"] + recorded["filter_rejection"]
    checks = [
        Check(
            "A1",
            _compare(total_sent, total_recorded, allow_other),
            f"capture completeness: {total_recorded} rejection events, {total_sent} rejected requests sent (want {relation})",
        ),
    ]
    for reason, count in sent.items():
        checks.append(
            Check(
                f"A1.{reason}",
                _compare(count, recorded[reason], allow_other),
                f"{recorded[reason]} {reason} events, {count} sent (want {relation})",
            ),
        )
    return checks


def check_pending(events: list[dict[str, Any]], grace: float, now: float) -> Check:
    """A2: no event older than the grace period is still waiting for upload."""
    stale = [(e["id"], now - _created_epoch(e)) for e in events if e.get("text_state") == "pending" and now - _created_epoch(e) > grace]
    pending = sum(1 for e in events if e.get("text_state") == "pending")
    if stale:
        oldest = max(age for _, age in stale)
        return Check(
            "A2",
            False,
            f"{len(stale)} of {pending} pending events are older than {grace:.0f}s (oldest {oldest:.0f}s): "
            f"the applier is not uploading; ids {_sample_ids(i for i, _ in stale)}",
        )
    return Check("A2", True, f"nothing left pending: {pending} pending, all younger than {grace:.0f}s")


def check_shape(events: list[dict[str, Any]]) -> Check:
    """A3: each state's fields are what the listing contract says."""
    bad: list[str] = []
    states = Counter(e.get("text_state") for e in events)
    for e in events:
        state = e.get("text_state")
        stages = [e.get(k) for k in STAGE_KEYS]
        if state not in TEXT_STATES:
            bad.append(f"{e['id']}:state={state}")
        elif state == "stored":
            if any(s is not None for s in stages):
                bad.append(f"{e['id']}:stored-with-inline-text")
            if not e.get("text_url"):
                bad.append(f"{e['id']}:stored-without-text_url")
            if not SHA256_PATTERN.match(str(e.get("text_sha256") or "")):
                bad.append(f"{e['id']}:bad-sha256")
            if not isinstance(e.get("text_chars"), int) or e["text_chars"] <= 0:
                bad.append(f"{e['id']}:text_chars={e.get('text_chars')}")
        elif state == "pending":
            if all(s is None for s in stages):
                bad.append(f"{e['id']}:pending-without-text")
            if e.get("text_url") is not None:
                bad.append(f"{e['id']}:pending-with-text_url")
        elif any(s is not None for s in stages) or e.get("text_url") is not None:
            bad.append(f"{e['id']}:none-with-text")
    summary = ", ".join(f"{state}={count}" for state, count in sorted(states.items(), key=lambda item: str(item[0])))
    if bad:
        return Check("A3", False, f"stored shape: {len(bad)} violations ({_sample_ids(bad)}); states {summary}")
    return Check("A3", True, f"stored shape: every event matches its state; states {summary}")


def _fetch_object(api: ModeratorApi, event: dict[str, Any]) -> tuple[requests.Response | None, dict[str, Any]]:
    resp = requests.get(event["text_url"], timeout=HTTP_TIMEOUT_SECONDS)
    if resp.status_code == 403:
        fresh = api.fresh_event(int(event["id"]))
        if fresh and fresh.get("text_url"):
            return requests.get(fresh["text_url"], timeout=HTTP_TIMEOUT_SECONDS), fresh
    return resp, event


def check_objects(
    api: ModeratorApi,
    events: list[dict[str, Any]],
    floods: list[dict[str, Any]],
    sample: int,
    allow_other: bool,
) -> list[Check]:
    """A4: stored objects match their listed digest and length, and clipping is bounded and flagged."""
    stored = [e for e in events if e.get("text_state") == "stored" and e.get("text_url")]
    chosen = stored if sample <= 0 or sample >= len(stored) else random.sample(stored, sample)
    bad: list[str] = []
    for event in chosen:
        try:
            resp, event = _fetch_object(api, event)
        except requests.RequestException as err:
            bad.append(f"{event['id']}:{type(err).__name__}")
            continue
        if resp is None or resp.status_code != 200:
            bad.append(f"{event['id']}:status={resp.status_code if resp is not None else None}")
            continue
        if "no-store" not in resp.headers.get("Cache-Control", ""):
            bad.append(f"{event['id']}:cache-control={resp.headers.get('Cache-Control')!r}")
        if hashlib.sha256(resp.content).hexdigest() != event.get("text_sha256"):
            bad.append(f"{event['id']}:digest")
        try:
            body = json.loads(resp.content)
        except ValueError:
            bad.append(f"{event['id']}:not-json")
            continue
        if set(body) != STAGE_KEYS:
            bad.append(f"{event['id']}:keys={sorted(body)}")
            continue
        lengths = [len(body[k]) for k in STAGE_KEYS if body[k] is not None]
        if sum(lengths) != event.get("text_chars"):
            bad.append(f"{event['id']}:chars={sum(lengths)}!={event.get('text_chars')}")
        if any(length > MAX_EVIDENCE_CHARACTERS for length in lengths):
            bad.append(f"{event['id']}:stage-over-limit")
        if event.get("text_truncated") and max(lengths, default=0) != MAX_EVIDENCE_CHARACTERS:
            bad.append(f"{event['id']}:truncated-without-clipped-stage")
    checks = [
        Check(
            "A4",
            not bad,
            f"object integrity: {len(chosen)} of {len(stored)} stored objects fetched, {len(bad)} problems"
            + (f" ({_sample_ids(bad)})" if bad else ""),
        ),
    ]

    bounds_bad = []
    for e in events:
        chars = e.get("text_chars")
        if chars is None:
            continue
        if chars > 2 * MAX_EVIDENCE_CHARACTERS:
            bounds_bad.append(f"{e['id']}:text_chars={chars}")
        elif e.get("text_truncated") and chars < MAX_EVIDENCE_CHARACTERS:
            bounds_bad.append(f"{e['id']}:truncated-with-{chars}")
    checks.append(
        Check(
            "A4.bounds",
            not bounds_bad,
            f"clip bounds: {len(bounds_bad)} events outside [{MAX_EVIDENCE_CHARACTERS}, {2 * MAX_EVIDENCE_CHARACTERS}] when truncated"
            f" or over {2 * MAX_EVIDENCE_CHARACTERS}" + (f" ({_sample_ids(bounds_bad)})" if bounds_bad else ""),
        ),
    )

    oversized = sum(1 for f in floods if f["status"] == 400 and f.get("rc") == REJECTION_RC and f["prompt_chars"] > MAX_EVIDENCE_CHARACTERS)
    truncated = sum(1 for e in events if e.get("text_truncated"))
    checks.append(
        Check(
            "A4.truncated",
            _compare(oversized, truncated, allow_other),
            f"clip flag: {truncated} truncated events, {oversized} rejected prompts over {MAX_EVIDENCE_CHARACTERS} characters sent"
            f" (want {'>=' if allow_other else '=='})",
        ),
    )
    return checks


def _is_timeout(record: dict[str, Any]) -> bool:
    return record["status"] == 403 and record.get("rc") == TIMEOUT_RC


def _shortest_timeout(records: list[dict[str, Any]], timeouts: list[dict[str, Any]]) -> float:
    """Return the shortest full timeout the address saw, in seconds; infinite when no 403 reported one.

    A 403 reports the seconds remaining, so only one sent just after a rejection returned reports nearly the full
    length. Without such a 403 the shortest remaining time is used, which can only make the hold check more lenient.
    """
    rejections = [r["done_ts"] for r in records if r["status"] == 400]
    reported = [t for t in timeouts if t.get("retry_after")]
    fresh = [int(t["retry_after"]) for t in reported if any(0 <= t["ts"] - done <= FRESH_TIMEOUT_WINDOW_SECONDS for done in rejections)]
    if fresh:
        return float(min(fresh))
    return float(min((int(t["retry_after"]) for t in reported), default=float("inf")))


def check_timeouts(floods: list[dict[str, Any]], threshold: int, flooders: int) -> list[Check]:
    """A5: per address, the model-rejection timeout engages after the threshold, not before, and holds.

    Request order is judged by the client's send (``ts``) and receive (``done_ts``) times. A request sent before the
    response that set a timeout arrived may have raced it, so the bounds allow ``flooders`` requests in flight. A
    filter rejection times the address out on its own, so a timeout that follows one says nothing about the model
    threshold. The model-rejection count is per hour in Redis: a second run within the hour from the same address
    starts above the threshold, and the onset check then fails.
    """
    checks: list[Check] = []
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in floods:
        if not record.get("moderator"):
            by_label[str(record.get("address_label"))].append(record)
    for label, records in sorted(by_label.items()):
        records.sort(key=lambda r: r["ts"])
        model = [r for r in records if r["path"] in MODEL_PATHS]
        model_400 = [r for r in model if r["status"] == 400]
        model_403 = [r for r in model if _is_timeout(r)]
        timeouts = [r for r in records if _is_timeout(r)]
        filter_400 = [r for r in records if r["path"] == "filter" and r["status"] == 400]
        min_ttl = _shortest_timeout(records, timeouts)
        prefix = f"A5[{label}]"

        if len(model) > threshold + flooders:
            checks.append(
                Check(
                    f"{prefix}.engaged",
                    bool(model_403),
                    f"{len(model)} model requests, {len(model_403)} timed out"
                    + ("" if model_403 else f": no timeout after more than {threshold + flooders} model rejections"),
                ),
            )
        else:
            checks.append(Check(f"{prefix}.engaged", True, f"{len(model)} model requests, not above threshold + flooders; not tested"))

        upper = threshold + 1 + flooders
        if model_403:
            before_first = sum(1 for r in model_400 if r["ts"] < model_403[0]["ts"])
            onset_bad = (
                [] if before_first <= upper else [f"{before_first} model rejections before the first model-path timeout, above {upper}"]
            )
        else:
            before_first = len(model_400)
            onset_bad = []
        first_timeout = timeouts[0] if timeouts else None
        lower_note = "no timeout"
        if first_timeout is not None:
            filter_first = any(r["ts"] < first_timeout["done_ts"] for r in filter_400)
            sent_before = sum(1 for r in model_400 if r["ts"] < first_timeout["done_ts"])
            if filter_first:
                lower_note = "first timeout follows a filter rejection; lower bound not tested"
            elif sent_before < threshold + 1:
                onset_bad.append(
                    f"first timeout after only {sent_before} model rejections, below {threshold + 1}"
                    " (a run within an hour of another from this address starts above the threshold)",
                )
            else:
                lower_note = f"{sent_before} model rejections preceded the first timeout"
        checks.append(
            Check(
                f"{prefix}.onset",
                not onset_bad,
                f"{before_first} model 400s before the first model-path 403, want [{threshold + 1}, {upper}]; "
                + ("; ".join(onset_bad) if onset_bad else lower_note),
            ),
        )

        unheld: list[str] = []
        for index in range(threshold + 1, len(model_400)):
            previous, current = model_400[index - 1], model_400[index]
            gap = current["ts"] - previous["done_ts"]
            if gap <= 0 or gap >= min_ttl:
                continue
            if not any(previous["done_ts"] <= t["ts"] <= current["ts"] for t in timeouts):
                unheld.append(f"#{index + 1} sent {gap:.1f}s after #{index} returned, with no 403 between")
        leaks = [
            f"400 at +{r['ts'] - t['done_ts']:.1f}s into a {t['retry_after']}s timeout"
            for t in timeouts
            if t.get("retry_after")
            for r in model_400
            if r["ts"] > t["done_ts"] and r["done_ts"] < t["ts"] + int(t["retry_after"])
        ]
        hold_bad = unheld + leaks
        checks.append(
            Check(
                f"{prefix}.hold",
                not hold_bad,
                f"{len(timeouts)} timeouts (shortest {min_ttl:.0f}s); "
                + (
                    f"{len(unheld)} rejections past the threshold not followed by a timeout, {len(leaks)} leaks through one: "
                    + _sample_ids(hold_bad)
                    if hold_bad
                    else "every rejection past the threshold timed the address out and no rejection leaked through a timeout"
                ),
            ),
        )

        filter_records = [r for r in records if r["path"] == "filter"]
        if len(filter_records) > 1 and filter_400:
            first = filter_400[0]
            following = next((r for r in records if r["ts"] > first["done_ts"]), None)
            if following is None or following["ts"] - first["done_ts"] >= min_ttl:
                checks.append(Check(f"{prefix}.filter", True, "no request inside the first filter timeout; not tested"))
            else:
                checks.append(
                    Check(
                        f"{prefix}.filter",
                        _is_timeout(following),
                        f"the request after the first filter rejection got {following['status']} {following.get('rc')}"
                        + ("" if _is_timeout(following) else ": the filter path did not time the address out"),
                    ),
                )
        else:
            checks.append(Check(f"{prefix}.filter", True, f"{len(filter_records)} filter requests; not tested"))
    if not by_label:
        checks.append(Check("A5", False, "no non-moderator flood records; the countermeasure was not exercised"))
    return checks


def check_moderator_exemption(floods: list[dict[str, Any]]) -> Check:
    """A6: a moderator's rejections never time out the address for the moderator."""
    moderator = [f for f in floods if f.get("moderator")]
    refused = [f for f in moderator if f["status"] == 403]
    if not moderator:
        return Check("A6", True, "moderator exemption: no flood request used the moderator key; not tested")
    return Check("A6", not refused, f"moderator exemption: {len(refused)} of {len(moderator)} moderator flood requests got 403")


def check_probes(records: list[dict[str, Any]]) -> list[Check]:
    """A7: every probe is refused as expected, and no recorded response anywhere is a 5xx."""
    probes = [r for r in records if r.get("kind") == "probe"]
    unexpected = [f"{r['name']}:{r['status']}" for r in probes if r["status"] not in PROBE_EXPECTED_STATUSES.get(r["name"], frozenset())]
    by_name = Counter(r["name"] for r in probes)
    server_errors = [f"{r.get('kind')}:{r.get('path') or r.get('name')}:{r['status']}" for r in records if int(r.get("status") or 0) >= 500]
    return [
        Check(
            "A7",
            not unexpected,
            f"probes: {len(probes)} ({', '.join(f'{k}={v}' for k, v in sorted(by_name.items()))}), {len(unexpected)} unexpected"
            + (f" ({_sample_ids(unexpected)})" if unexpected else ""),
        ),
        Check(
            "A7.5xx",
            not server_errors,
            f"{len(server_errors)} 5xx responses in the evidence" + (f" ({_sample_ids(server_errors)})" if server_errors else ""),
        ),
    ]


def check_text_fetches(records: list[dict[str, Any]]) -> Check:
    """A8: every object the reviewers fetched matched its listing, apart from links that expired."""
    fetches = [r for r in records if r.get("kind") == "text_fetch"]
    expired = sum(1 for r in fetches if r["status"] == 403)
    bad = [
        f"{r['event_id']}:{r['status']}"
        for r in fetches
        if r["status"] != 403 and not (r.get("digest_ok") and r.get("keys_ok") and r.get("inline_text_absent"))
    ]
    return Check(
        "A8",
        not bad,
        f"reviewer fetches: {len(fetches)} ({expired} expired links), {len(bad)} mismatched" + (f" ({_sample_ids(bad)})" if bad else ""),
    )


def check_stats(stats_path: Path) -> list[Check]:
    """A9: Locust saw no 5xx and the flooders reached the target."""
    if not stats_path.is_file():
        return [Check("A9", False, f"stats file not found: {stats_path}")]
    with stats_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    flood_requests = sum(int(r.get("Request Count") or 0) for r in rows if r.get("Name") in FLOOD_REQUEST_NAMES)
    failures_path = stats_path.with_name(stats_path.name.replace("_stats.csv", "_failures.csv"))
    if not failures_path.is_file():
        return [
            Check("A9", flood_requests > 0, f"{flood_requests} flood requests in the stats"),
            Check("A9.5xx", False, f"failures file not found next to the stats: {failures_path}"),
        ]
    with failures_path.open(newline="") as handle:
        server_errors = [
            f"{r.get('Name')} x{r.get('Occurrences')}" for r in csv.DictReader(handle) if SERVER_ERROR_PATTERN.search(r.get("Error") or "")
        ]
    return [
        Check(
            "A9",
            flood_requests > 0,
            f"{flood_requests} flood requests in the stats" + ("" if flood_requests else ": the run measured nothing"),
        ),
        Check(
            "A9.5xx",
            not server_errors,
            f"{len(server_errors)} 5xx failure rows in {failures_path.name}"
            + (f" ({_sample_ids(server_errors)})" if server_errors else ""),
        ),
    ]


# ---------------------------------------------------------------------------
# B: database checks
# ---------------------------------------------------------------------------

DB_RULES: dict[str, str] = {
    "B.stored": "text_state = 'stored' AND (submitted_prompt IS NOT NULL OR moderation_prompt IS NOT NULL OR effective_prompt IS NOT NULL)",
    "B.pending": "text_state = 'pending' AND ((submitted_prompt IS NULL AND moderation_prompt IS NULL AND effective_prompt IS NULL)"
    " OR text_redacted)",
    "B.none": "text_state = 'none' AND (submitted_prompt IS NOT NULL OR moderation_prompt IS NOT NULL OR effective_prompt IS NOT NULL)",
    "B.redacted": "text_redacted AND (text_state <> 'none' OR text_sha256 IS NOT NULL OR text_chars IS NOT NULL)",
    "B.anonymized": "anonymized AND (user_id IS NOT NULL OR ipaddr IS NOT NULL OR ip_subject_key IS NOT NULL"
    " OR proxied_account IS NOT NULL OR request_id IS NOT NULL OR job_id IS NOT NULL OR NOT text_redacted OR NOT ipaddr_redacted)",
}
"""Each rule's violating rows, as a WHERE clause over ``prompt_moderation_events``; a passing rule matches none."""


def check_database(dsn: str, first_event_id: int, grace: float) -> tuple[list[Check], set[int] | None, dict[int, tuple[str, float]]]:
    """B: the row invariants of the evidence lifecycle, read from Postgres.

    Returns the checks, the stored ids (None when the database was unreachable), and every run row's state and age.
    """
    try:
        import psycopg2
    except ImportError:
        return [Check("B", False, "--pg-dsn given but psycopg2 is not installed")], None, {}
    try:
        connection = psycopg2.connect(dsn)
    except psycopg2.Error as err:
        return [Check("B", False, f"cannot connect: {err}")], None, {}
    checks: list[Check] = []
    rows: dict[int, tuple[str, float]] = {}
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, text_state, EXTRACT(EPOCH FROM ((now() AT TIME ZONE 'utc') - created))"
                " FROM prompt_moderation_events WHERE id > %s",
                (first_event_id,),
            )
            for event_id, state, age in cursor.fetchall():
                rows[int(event_id)] = (str(state), float(age))
            states = Counter(state for state, _ in rows.values())
            checks.append(Check("B.states", True, f"{len(rows)} run rows: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items()))))
            for code, clause in DB_RULES.items():
                cursor.execute(f"SELECT id FROM prompt_moderation_events WHERE id > %s AND {clause} ORDER BY id", (first_event_id,))
                bad = [row[0] for row in cursor.fetchall()]
                checks.append(Check(code, not bad, f"{len(bad)} rows violate: {clause}" + (f" (ids {_sample_ids(bad)})" if bad else "")))
            stale = [i for i, (state, age) in rows.items() if state == "pending" and age > grace]
            checks.append(
                Check(
                    "B.stale",
                    not stale,
                    f"{len(stale)} pending rows older than {grace:.0f}s" + (f" (ids {_sample_ids(stale)})" if stale else ""),
                )
            )
            cursor.execute("SELECT 1 FROM pg_constraint WHERE conname = 'ck_prompt_moderation_text_state'")
            checks.append(Check("B.constraint", cursor.fetchone() is not None, "ck_prompt_moderation_text_state present"))
    finally:
        connection.close()
    stored = {i for i, (state, _) in rows.items() if state == "stored"}
    return checks, stored, rows


# ---------------------------------------------------------------------------
# C: bucket checks
# ---------------------------------------------------------------------------


def check_bucket(
    args: argparse.Namespace,
    first_event_id: int,
    last_event_id: int,
    expected_stored: set[int],
    states: dict[int, tuple[str, float]],
    grace: float,
) -> list[Check]:
    """C: the bucket holds exactly the run's stored events, with no orphan object and no missing one."""
    try:
        import boto3
    except ImportError:
        return [Check("C", False, "--evidence-endpoint given but boto3 is not installed")]
    client = boto3.client(
        "s3",
        endpoint_url=args.evidence_endpoint,
        region_name=args.evidence_region,
        aws_access_key_id=os.environ.get("EVIDENCE_AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("EVIDENCE_AWS_SECRET_ACCESS_KEY"),
    )
    objects: set[int] = set()
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=args.evidence_bucket, Prefix="evidence/"):
            for item in page.get("Contents", []):
                match = OBJECT_KEY_PATTERN.match(item["Key"])
                if match and first_event_id < int(match.group(1)) <= last_event_id:
                    objects.add(int(match.group(1)))
    except Exception as err:  # noqa: BLE001 - any client or transport error fails the check the operator asked for
        return [Check("C", False, f"cannot list the bucket: {type(err).__name__}: {err}")]
    missing = sorted(expected_stored - objects)
    # An object can briefly precede its row's update to stored; only rows past the grace period are orphans.
    orphans = sorted(i for i in objects - expected_stored if i not in states or states[i][1] > grace)
    return [
        Check(
            "C.missing",
            not missing,
            f"{len(objects)} run objects, {len(expected_stored)} stored events; {len(missing)} stored without an object"
            + (f" (ids {_sample_ids(missing)})" if missing else ""),
        ),
        Check(
            "C.orphans",
            not orphans,
            f"{len(orphans)} objects for events that are not stored" + (f" (ids {_sample_ids(orphans)})" if orphans else ""),
        ),
    ]


# ---------------------------------------------------------------------------
# D: metric checks
# ---------------------------------------------------------------------------


def _mimir_query(args: argparse.Namespace, query: str, at: float | None) -> float:
    params: dict[str, object] = {"query": query}
    if at is not None:
        params["time"] = at
    headers = {"X-Scope-OrgID": args.mimir_tenant} if args.mimir_tenant else {}
    resp = requests.get(args.mimir_url.rstrip("/") + "/api/v1/query", params=params, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    result = resp.json()["data"]["result"]
    return float(result[0]["value"][1]) if result else 0.0


def check_metrics(args: argparse.Namespace, bounds: RunBounds, engaged_labels: int) -> list[Check]:
    """D: the applier kept up, nothing failed, the countermeasure counter matches the run, retention kept pace."""
    selector = args.mimir_selector
    window = int(bounds.stop - bounds.start) + 2 * METRIC_WINDOW_MARGIN_SECONDS
    at = bounds.stop + METRIC_WINDOW_MARGIN_SECONDS

    def counter(name: str, extra: str = "") -> str:
        labels = ",".join(part for part in (selector, extra) if part)
        return f"sum(increase({name}{{{labels}}}[{window}s]))"

    expectations: list[tuple[str, str, float | None, Callable[[float], bool], str]] = [
        (
            "D.pending",
            f"max(horde_moderation_evidence_pending_rows{{{selector}}})",
            None,
            lambda v: v <= EVIDENCE_UPLOAD_BATCH_SIZE,
            f"<= {EVIDENCE_UPLOAD_BATCH_SIZE}",
        ),
        ("D.upload_failures", counter("horde_moderation_evidence_upload_failures"), at, lambda v: round(v) == 0, "== 0"),
        ("D.write_failures", counter("horde_moderation_evidence_write_failures"), at, lambda v: round(v) == 0, "== 0"),
        (
            "D.countermeasures",
            counter("horde_moderation_countermeasures", 'horde_action="timeout"'),
            at,
            lambda v: round(v) == engaged_labels,
            f"== {engaged_labels} (addresses that passed the threshold)",
        ),
    ]
    if not args.expect_saturation:
        expectations.append(("D.saturation", counter("horde_moderation_retention_saturation"), at, lambda v: round(v) == 0, "== 0"))
    checks = []
    for code, query, when, predicate, want in expectations:
        try:
            value = _mimir_query(args, query, when)
        except Exception as err:  # noqa: BLE001 - an unreachable Mimir fails the check the operator asked for
            checks.append(Check(code, False, f"{query}: {type(err).__name__}: {err}"))
            continue
        checks.append(Check(code, predicate(value), f"{query} = {value:g}, want {want}"))
    return checks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _engaged_labels(floods: list[dict[str, Any]], threshold: int) -> int:
    """Return how many addresses sent more model rejections than the threshold, each of which earns one notice."""
    counts = Counter(f["address_label"] for f in floods if not f.get("moderator") and f["path"] in MODEL_PATHS and f["status"] == 400)
    return sum(1 for count in counts.values() if count > threshold)


def _print_report(checks: list[Check]) -> None:
    for check in checks:
        print(f"{'PASS' if check.passed else 'FAIL'}  {check.code:<28} {check.detail}")
    failed = sum(1 for c in checks if not c.passed)
    print(f"Moderation evidence checks: {len(checks) - failed} PASS, {failed} FAIL.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gate a moderation evidence run on what the backend recorded.")
    parser.add_argument(
        "--evidence",
        required=True,
        type=Path,
        nargs="+",
        help="JSONL evidence file(s) from locustfile_moderation.py, one per load-generating host.",
    )
    parser.add_argument("--host", required=True, help="Base URL of the deployment the run targeted.")
    parser.add_argument(
        "--moderator-api-key",
        default=os.environ.get("HORDE_MODERATION_MODERATOR_API_KEY"),
        help="Moderator API key for the listing (env HORDE_MODERATION_MODERATOR_API_KEY).",
    )
    parser.add_argument("--stats", type=Path, default=None, help="Locust <prefix>_stats.csv; fails on 5xx and on zero flood requests.")
    parser.add_argument("--threshold", type=int, default=5, help="The deployment's HORDE_MODEL_REJECTION_TIMEOUT_THRESHOLD.")
    parser.add_argument("--grace-seconds", type=float, default=60.0, help="How long after capture an event may still be pending.")
    parser.add_argument("--sample", type=int, default=25, help="Stored objects to fetch and verify (0 = all).")
    parser.add_argument(
        "--flooders", type=int, default=8, help="Concurrent flooders per address; the in-flight tolerance on the threshold."
    )
    parser.add_argument("--allow-other-traffic", action="store_true", help="Treat count equalities as lower bounds.")
    parser.add_argument("--pg-dsn", default=None, help="Postgres DSN; enables the database checks (needs psycopg2).")
    parser.add_argument(
        "--evidence-endpoint", default=None, help="S3 endpoint of the evidence store; enables the bucket checks (needs boto3)."
    )
    parser.add_argument("--evidence-bucket", default=None, help="Evidence bucket name.")
    parser.add_argument("--evidence-region", default="auto", help="SigV4 region of the evidence store.")
    parser.add_argument(
        "--mimir-url", default=None, help="Prometheus API base of Mimir, such as https://mimir/prometheus; enables the metric checks."
    )
    parser.add_argument("--mimir-tenant", default=None, help="X-Scope-OrgID for Mimir queries.")
    parser.add_argument(
        "--mimir-selector",
        default="",
        help='Label matchers added to every metric query, such as deployment_environment_name="dev".',
    )
    parser.add_argument("--expect-saturation", action="store_true", help="Do not fail on retention saturation in the run window.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.moderator_api_key:
        raise SystemExit("--moderator-api-key (or HORDE_MODERATION_MODERATOR_API_KEY) is required")
    if bool(args.evidence_endpoint) != bool(args.evidence_bucket):
        raise SystemExit("--evidence-endpoint and --evidence-bucket go together")

    records = read_evidence(args.evidence)
    bounds = run_bounds(records)
    floods = [r for r in records if r.get("kind") == "flood"]
    checks: list[Check] = [
        Check(
            "R0",
            not bounds.errors,
            f"run bounds: events above id {bounds.first_event_id}, window {bounds.stop - bounds.start:.0f}s, "
            f"labels {', '.join(bounds.labels) or 'none'}" + (f"; {'; '.join(bounds.errors)}" if bounds.errors else ""),
        ),
    ]
    thresholds = {r.get("threshold_hint") for r in records if r.get("kind") == "run_start"}
    if thresholds - {args.threshold}:
        print(f"note: the driver recorded threshold hint(s) {sorted(thresholds)}, the checker uses {args.threshold}")

    api = ModeratorApi(args.host, args.moderator_api_key)
    try:
        events = api.run_events(bounds.first_event_id)
    except (requests.RequestException, RuntimeError, ValueError) as err:
        checks.append(Check("A0", False, f"cannot gather the run's events from the listing: {type(err).__name__}: {err}"))
        _print_report(checks)
        return 1
    now = time.time()
    checks.append(Check("A0", True, f"gathered {len(events)} run events from the listing"))
    checks += check_capture(floods, events, args.allow_other_traffic)
    checks.append(check_pending(events, args.grace_seconds, now))
    checks.append(check_shape(events))
    checks += check_objects(api, events, floods, args.sample, args.allow_other_traffic)
    checks += check_timeouts(floods, args.threshold, args.flooders)
    checks.append(check_moderator_exemption(floods))
    checks += check_probes(records)
    checks.append(check_text_fetches(records))
    if args.stats is not None:
        checks += check_stats(args.stats)

    stored_ids: set[int] | None = None
    row_states: dict[int, tuple[str, float]] = {}
    if args.pg_dsn:
        db_checks, stored_ids, row_states = check_database(args.pg_dsn, bounds.first_event_id, args.grace_seconds)
        checks += db_checks
    if args.evidence_endpoint:
        if stored_ids is None:
            stored_ids = {int(e["id"]) for e in events if e.get("text_state") == "stored"}
            row_states = {int(e["id"]): (str(e.get("text_state")), now - _created_epoch(e)) for e in events}
        last_event_id = max([bounds.first_event_id, *(int(e["id"]) for e in events), *row_states])
        checks += check_bucket(args, bounds.first_event_id, last_event_id, stored_ids, row_states, args.grace_seconds)
    if args.mimir_url:
        checks += check_metrics(args, bounds, _engaged_labels(floods, args.threshold))

    _print_report(checks)
    return 1 if any(not c.passed for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
