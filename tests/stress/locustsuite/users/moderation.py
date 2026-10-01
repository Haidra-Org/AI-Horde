# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust users for the prompt moderation evidence path.

Three populations drive the behaviors the evidence store, the model-rejection timeout and the moderator listing
promise under load:

- ``RejectionFlooder`` sends prompts that the deployment's filters reject, so every request records an evidence
  event and, on the model path, counts toward the IP timeout. The operator seeds the filters it trips (see the
  module arguments), so no real abusive text is ever sent.
- ``ModeratorReviewer`` lists events as a moderator, follows ``text_url`` links, checks the object's digest against
  the listed one, and occasionally attaches a note.
- ``ModerationProber`` sends the requests an attacker or a broken client would: unauthenticated listings, tampered
  links, over-limit pages.

Every expected refusal (400 for a rejected prompt, 403 for a timed-out address, 429 for a rate limit, 401 for a
missing moderator key) counts as a success and is recorded under its own name, so the report shows the mix; only
5xx and transport errors fail a run.

Locust's statistics judge each response alone, so they cannot show that every rejection was captured, that the
timeout engaged after the threshold and held, or that stored text matches what was sent. ``evidence_recorder``
therefore appends every flood request, text fetch and probe to a JSONL file, and
``tests/stress/check_moderation_results.py`` compares that ground truth with what the backend recorded.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from pathlib import Path

import gevent.lock
import requests
from locust import HttpUser, between, tag, task
from locust.runners import MasterRunner

from ..config import _config
from ..helpers import _headers, _pick_requestor_key, _record_expected, _safe_json

moderation_state: dict[str, object] = {
    "nsfw_model": "",
    "model_token": "",
    "filter_tokens": [],
    "pad_chars": 0,
    "moderator_api_key": "",
    "listing_limit": 100,
    "note_chance": 0.05,
    "timeouts_seen": {},
}
"""Scenario settings populated at test start from the moderation arguments, plus the per-key timeouts observed."""

EXPECTED_REJECTION_RCS = {"CorruptPrompt"}
"""Return codes a rejected prompt answers with; anything else on a 400 is a real failure."""

MODERATION_LISTING_PATH = "/api/v2/operations/moderation/prompts"
"""The moderator event listing, which also bounds the run's events by their first id."""

TIMEOUT_SECONDS_PATTERN = re.compile(r"for (\d+) more seconds")
"""Remaining timeout in a ``TimeoutIP`` message; the API sends no ``Retry-After`` header with it."""

RUN_START_LISTING_TIMEOUT_SECONDS = 30.0
"""Bound on the one listing request at test start, so an unreachable target cannot hang the start hook."""


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModerationEvidenceRecorder:
    """Append the run's ground truth, one JSON record per line, for the moderation results checker.

    Each record is written with an open, append and close under a lock, as ``OracleRecorder`` does, so concurrent
    greenlets never interleave lines and a killed run leaves every completed record readable.
    """

    def __init__(self) -> None:
        self._path: Path | None = None
        self._lock = gevent.lock.RLock()
        self.address_label = "local"

    def configure(self, path: str | None, address_label: str) -> None:
        """Set the evidence path and truncate it, so a run's evidence never includes an earlier run's records."""
        with self._lock:
            self.address_label = address_label
            if not path:
                self._path = None
                return
            self._path = Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("", encoding="utf-8")

    def write(self, record: dict[str, object]) -> None:
        """Append one record; a no-op when no path is configured."""
        with self._lock:
            if self._path is None:
                return
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")


evidence_recorder = ModerationEvidenceRecorder()
"""The process-wide recorder the users and the start and stop hooks write through."""


def _first_event_id(host: str, moderator_api_key: str) -> tuple[int | None, str | None]:
    """Return the newest event id before the run, so the checker can tell the run's events from earlier ones.

    Returns:
        The id (None when the listing is empty) and None, or None and the error when the listing failed.
    """
    try:
        resp = requests.get(
            host.rstrip("/") + MODERATION_LISTING_PATH,
            params={"limit": 1},
            headers=_headers(moderator_api_key),
            timeout=RUN_START_LISTING_TIMEOUT_SECONDS,
        )
    except requests.RequestException as err:
        return None, f"{type(err).__name__}: {err}"
    if not resp.ok:
        return None, f"Status {resp.status_code}: {resp.text[:200]}"
    events = (_safe_json(resp) or {}).get("events") or []
    return (int(events[0]["id"]) if events else None), None


def start_moderation_evidence(environment) -> None:
    """Truncate the evidence file and write the ``run_start`` record.

    A distributed master runs no users, so it records nothing; each worker host writes its own file under its own
    address label.
    """
    if isinstance(environment.runner, MasterRunner):
        return
    options = environment.parsed_options
    evidence_recorder.configure(options.moderation_evidence_path, options.moderation_address_label)
    first_event_id = None
    first_event_error = None
    if moderation_state["moderator_api_key"]:
        first_event_id, first_event_error = _first_event_id(environment.host or "", moderation_state["moderator_api_key"])
    record: dict[str, object] = {
        "kind": "run_start",
        "ts": time.time(),
        "address_label": evidence_recorder.address_label,
        "threshold_hint": int(options.moderation_threshold_hint),
        "first_event_id": first_event_id,
    }
    if first_event_error is not None:
        record["first_event_error"] = first_event_error
    evidence_recorder.write(record)


def stop_moderation_evidence(environment) -> None:
    """Write the ``run_stop`` record, which closes the window the checker's metric queries cover."""
    if isinstance(environment.runner, MasterRunner):
        return
    evidence_recorder.write({"kind": "run_stop", "ts": time.time(), "address_label": evidence_recorder.address_label})


def _retry_after(resp) -> int | None:
    """Return the timeout seconds a 403 reports, from ``Retry-After`` or else the ``TimeoutIP`` message."""
    header = resp.headers.get("Retry-After")
    if header and header.strip().isdigit():
        return int(header.strip())
    match = TIMEOUT_SECONDS_PATTERN.search(resp.text or "")
    return int(match.group(1)) if match else None


def _record_flood(path: str, api_key: str, prompt: str, sent: float, resp) -> None:
    """Record one rejected-prompt request: who sent it, when, and how the deployment answered.

    ``ts`` is the send time and ``done_ts`` the receive time; the checker needs both to tell a request that raced the
    timeout (sent before the response that set it arrived) from one that leaked through it.
    """
    body = _safe_json(resp) or {}
    evidence_recorder.write(
        {
            "kind": "flood",
            "ts": sent,
            "done_ts": time.time(),
            "address_label": evidence_recorder.address_label,
            "path": path,
            "key_hash": _sha256_hex(api_key)[:8],
            "status": resp.status_code,
            "rc": body.get("rc"),
            "retry_after": _retry_after(resp) if resp.status_code in (403, 429) else None,
            "prompt_chars": len(prompt),
            "prompt_sha256": _sha256_hex(prompt),
            "moderator": bool(api_key) and api_key == moderation_state["moderator_api_key"],
        },
    )


def _record_probe(name: str, resp) -> None:
    evidence_recorder.write(
        {
            "kind": "probe",
            "ts": time.time(),
            "name": name,
            "status": resp.status_code,
        },
    )


def configure_moderation(options) -> None:
    """Copy the moderation arguments into ``moderation_state`` at test start."""
    moderation_state["nsfw_model"] = options.moderation_nsfw_model
    moderation_state["model_token"] = options.moderation_model_token
    moderation_state["filter_tokens"] = [t.strip() for t in options.moderation_filter_tokens.split(",") if t.strip()]
    moderation_state["pad_chars"] = int(options.moderation_pad_chars)
    moderation_state["moderator_api_key"] = options.moderation_moderator_api_key
    moderation_state["listing_limit"] = int(options.moderation_listing_limit)
    moderation_state["note_chance"] = float(options.moderation_note_chance)


def _record(environment, resp, name: str) -> None:
    _record_expected(
        environment, "GET" if resp.request.method == "GET" else "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b"")
    )


def _padding(chars: int) -> str:
    """Return a negative prompt of ``chars`` random letters, the cheapest way to make a rejected prompt large."""
    if chars <= 0:
        return ""
    alphabet = "abcdefghijklmnopqrstuvwxyz "
    return " ### " + "".join(random.choice(alphabet) for _ in range(chars))


class RejectionFlooder(HttpUser):
    """Send prompts the filters reject, as a scripted abuser would, and record how the deployment answers.

    Model rejections (``--moderation-model-token`` on ``--moderation-nsfw-model``) are the path the timeout bounds;
    filter rejections (both ``--moderation-filter-tokens`` in one prompt) time out on the first and are sent less
    often. A 403 ``TimeoutIP`` means the countermeasure engaged; the user keeps sending so the run shows that it holds.
    """

    weight = 4
    fixed_count = 0
    wait_time = between(0.3, 0.8)

    def on_start(self) -> None:
        self.api_key = _pick_requestor_key()

    def _send(self, prompt: str, models: list[str], name: str, path: str) -> None:
        payload = {
            "prompt": prompt,
            "params": {"width": 512, "height": 512, "steps": 20},
            "models": models,
            "nsfw": True,
            "r2": True,
        }
        sent = time.time()
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            _record_flood(path, self.api_key, prompt, sent, resp)
            body = _safe_json(resp) or {}
            rc = body.get("rc")
            if resp.status_code == 400 and rc in EXPECTED_REJECTION_RCS:
                resp.success()
                _record(self.environment, resp, f"{name} [rejected]")
            elif resp.status_code == 403 and rc == "TimeoutIP":
                resp.success()
                _record(self.environment, resp, f"{name} [timed-out]")
                seen = moderation_state["timeouts_seen"]
                seen[self.api_key] = seen.get(self.api_key, 0) + 1
                time.sleep(min(float(resp.headers.get("Retry-After") or 5.0), 10.0))
            elif resp.status_code == 429:
                resp.success()
                _record(self.environment, resp, f"{name} [rate-limited]")
                time.sleep(min(float(resp.headers.get("Retry-After") or 2.0), 10.0))
            elif resp.ok:
                # The prompt got through: the filter tokens are not seeded or do not match. Surface it as a failure so
                # the run is not silently measuring nothing.
                resp.failure("prompt was accepted; the moderation filter tokens are not tripping the filter")
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @tag("moderation", "flood")
    @task(6)
    def model_rejection(self) -> None:
        token = moderation_state["model_token"]
        model = moderation_state["nsfw_model"]
        if not token or not model:
            return
        self._send(
            token + _padding(moderation_state["pad_chars"]),
            [model],
            "/api/v2/generate/async [model-rejection]",
            "model",
        )

    @tag("moderation", "flood")
    @task(1)
    def filter_rejection(self) -> None:
        tokens = moderation_state["filter_tokens"]
        if len(tokens) < 2:
            return
        self._send(
            " ".join(tokens) + _padding(moderation_state["pad_chars"]),
            _config.get("models", []),
            "/api/v2/generate/async [filter-rejection]",
            "filter",
        )


class ModeratorReviewer(HttpUser):
    """List evidence as a moderator, follow stored text links, verify digests, and attach the occasional note."""

    weight = 1
    fixed_count = 0
    wait_time = between(2, 5)

    def on_start(self) -> None:
        self.api_key = moderation_state["moderator_api_key"]
        self.cursor = None

    def _listing(self, params: dict) -> dict | None:
        if not self.api_key:
            return None
        with self.client.get(
            "/api/v2/operations/moderation/prompts",
            params=params,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/operations/moderation/prompts",
        ) as resp:
            if resp.status_code == 429:
                resp.success()
                _record(self.environment, resp, "/api/v2/operations/moderation/prompts [rate-limited]")
                time.sleep(min(float(resp.headers.get("Retry-After") or 5.0), 15.0))
                return None
            if not resp.ok:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                return None
            body = _safe_json(resp)
            if not body or "events" not in body:
                resp.failure("listing without events")
                return None
            resp.success()
            return body

    @tag("moderation", "review")
    @task(4)
    def list_latest(self) -> None:
        body = self._listing({"limit": moderation_state["listing_limit"]})
        if body:
            self.cursor = body.get("next_cursor")

    @tag("moderation", "review")
    @task(2)
    def list_next_page(self) -> None:
        if self.cursor is None:
            return
        body = self._listing({"limit": moderation_state["listing_limit"], "before_id": self.cursor})
        self.cursor = body.get("next_cursor") if body else None

    @tag("moderation", "review")
    @task(3)
    def reveal_stored_text(self) -> None:
        """Fetch one stored event's object and check it is what the listing says it is."""
        body = self._listing({"limit": 25})
        if not body:
            return
        stored = [e for e in body["events"] if e.get("text_state") == "stored" and e.get("text_url")]
        if not stored:
            return
        event = random.choice(stored)
        with self.client.get(event["text_url"], catch_response=True, name="text_url [stored object]") as resp:
            stages = _safe_json(resp) or {}
            evidence_recorder.write(
                {
                    "kind": "text_fetch",
                    "ts": time.time(),
                    "event_id": event["id"],
                    "status": resp.status_code,
                    "digest_ok": resp.ok and hashlib.sha256(resp.content).hexdigest() == event.get("text_sha256"),
                    "keys_ok": resp.ok and set(stages) == {"submitted_prompt", "moderation_prompt", "effective_prompt"},
                    "inline_text_absent": event.get("submitted_prompt") is None,
                },
            )
            if resp.status_code == 403:
                # The link outlived its window between the listing and the fetch; the client refetches by id.
                resp.success()
                _record(self.environment, resp, "text_url [expired]")
                return
            if not resp.ok:
                resp.failure(f"Status {resp.status_code} fetching text_url")
                return
            digest = hashlib.sha256(resp.content).hexdigest()
            if digest != event.get("text_sha256"):
                resp.failure("object digest differs from text_sha256")
            elif set(stages) != {"submitted_prompt", "moderation_prompt", "effective_prompt"}:
                resp.failure(f"object keys {sorted(stages)}")
            elif event.get("submitted_prompt") is not None:
                resp.failure("stored event still carries inline text")
            else:
                resp.success()

    @tag("moderation", "review", "notes")
    @task(1)
    def note_sometimes(self) -> None:
        if random.random() > moderation_state["note_chance"]:
            return
        body = self._listing({"limit": 10})
        if not body or not body["events"]:
            return
        event = random.choice(body["events"])
        with self.client.post(
            f"/api/v2/operations/moderation/prompts/{event['id']}/notes",
            json={"note": f"load test note {uuid.uuid4().hex[:8]}"},
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/operations/moderation/prompts/[id]/notes",
        ) as resp:
            if resp.status_code == 429:
                resp.success()
                _record(self.environment, resp, "notes [rate-limited]")
            elif resp.ok:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class ModerationProber(HttpUser):
    """Send what an attacker or a broken client would at the moderation surface and expect refusals."""

    weight = 1
    fixed_count = 0
    wait_time = between(1, 3)

    def _expect(self, resp, statuses: set[int], name: str, probe: str | None = None) -> None:
        if probe is not None:
            _record_probe(probe, resp)
        if resp.status_code in statuses:
            resp.success()
            _record(self.environment, resp, name)
        elif resp.status_code >= 500:
            resp.failure(f"Server error: {resp.status_code}: {resp.text[:200]}")
        else:
            resp.failure(f"Unexpected {resp.status_code} (wanted {sorted(statuses)})")

    @tag("moderation", "probe")
    @task(3)
    def listing_without_key(self) -> None:
        with self.client.get(
            "/api/v2/operations/moderation/prompts",
            headers={"Client-Agent": _config.get("client_agent", "stress")},
            catch_response=True,
            name="/api/v2/operations/moderation/prompts [no key]",
        ) as resp:
            # A missing apikey header fails request parsing (400) before any authorization check runs.
            self._expect(resp, {400, 401, 403}, "/api/v2/operations/moderation/prompts [no key]", "no_key")

    @tag("moderation", "probe")
    @task(2)
    def listing_with_requestor_key(self) -> None:
        with self.client.get(
            "/api/v2/operations/moderation/prompts",
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/operations/moderation/prompts [non-moderator]",
        ) as resp:
            self._expect(resp, {401, 403}, "/api/v2/operations/moderation/prompts [non-moderator]", "non_moderator")

    @tag("moderation", "probe")
    @task(2)
    def over_limit_page(self) -> None:
        key = moderation_state["moderator_api_key"]
        if not key:
            return
        with self.client.get(
            "/api/v2/operations/moderation/prompts",
            params={"limit": 101},
            headers=_headers(key),
            catch_response=True,
            name="/api/v2/operations/moderation/prompts [limit 101]",
        ) as resp:
            self._expect(resp, {400, 429}, "/api/v2/operations/moderation/prompts [limit 101]", "limit_101")

    @tag("moderation", "probe")
    @task(2)
    def tampered_text_link(self) -> None:
        """A presigned link for one object must not open another: change the key, keep the signature."""
        key = moderation_state["moderator_api_key"]
        if not key:
            return
        with self.client.get(
            "/api/v2/operations/moderation/prompts",
            params={"limit": 25},
            headers=_headers(key),
            catch_response=True,
            name="/api/v2/operations/moderation/prompts",
        ) as listing:
            _record_probe("listing", listing)
            if not listing.ok:
                listing.success() if listing.status_code == 429 else listing.failure(f"Status {listing.status_code}")
                return
            listing.success()
            body = _safe_json(listing) or {}
        stored = [e for e in body.get("events", []) if e.get("text_url")]
        if not stored:
            return
        event = random.choice(stored)
        other_id = event["id"] + random.randint(1, 50)
        tampered = event["text_url"].replace(f"evidence/{event['id']}.json", f"evidence/{other_id}.json")
        if tampered == event["text_url"]:
            return
        with self.client.get(tampered, catch_response=True, name="text_url [tampered key]") as resp:
            self._expect(resp, {403}, "text_url [tampered key]", "tampered_link")

    @tag("moderation", "probe")
    @task(1)
    def oversized_rejected_prompt(self) -> None:
        """A rejected prompt with a very long negative side must be clipped, never stored whole or refused by size."""
        token = moderation_state["model_token"]
        model = moderation_state["nsfw_model"]
        if not token or not model:
            return
        api_key = _pick_requestor_key()
        prompt = token + _padding(40000)
        payload = {
            "prompt": prompt,
            "params": {"width": 512, "height": 512, "steps": 20},
            "models": [model],
            "nsfw": True,
        }
        sent = time.time()
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(api_key),
            catch_response=True,
            name="/api/v2/generate/async [40k negative prompt]",
        ) as resp:
            _record_flood("oversized", api_key, prompt, sent, resp)
            self._expect(resp, {400, 403, 429}, "/api/v2/generate/async [40k negative prompt]")
