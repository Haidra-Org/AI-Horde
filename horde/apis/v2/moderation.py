# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Expose moderator-only review of retained evidence, promotion candidates, and paused workers.

Every resource here requires a moderator ``apikey`` and answers with
``Cache-Control: private, no-store``: the payloads carry account identity,
IP addresses, and retained prompt text.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from flask_restx import Resource, inputs, reqparse

import horde.apis.limiter_api as lim
from horde import exceptions as e
from horde import r2
from horde.apis.v2.base import api, check_for_mod, models
from horde.classes.base.prompt_moderation import MAX_NOTE_CHARACTERS, EvidenceTextState, PromptModerationReason
from horde.countermeasures import CounterMeasures
from horde.database import moderation as moderation_db
from horde.database import prompt_moderation as evidence_db
from horde.flask import db
from horde.limiter import limiter
from horde.suspicions import Suspicions

PRIVATE_HEADERS: dict[str, str] = {"Cache-Control": "private, no-store"}
"""The caching headers of every response here.

The payloads carry account identity, addresses and prompt text, so no shared or browser cache may keep them.
"""
MAX_EVIDENCE_PAGE: int = 100
"""The most events one evidence page returns.

An event carries up to three prompt stages of ``MAX_EVIDENCE_CHARACTERS`` each, so 100 events bound a page near five
million characters of prompt text.
"""
MAX_OPERATIONS_PAGE: int = 500
"""The most rows one overview queue or suspicion history page returns.

Each overview row loads its account's or worker's relationships, so the bound caps the rows those reads return and the
time one request holds a database connection, while one page still covers the whole of a normal review queue.
"""
IP_SUBJECT_KEY_PATTERN: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")
"""The form of an address pseudonym, the hex HMAC-SHA256 digest ``CounterMeasures.ip_subject_key`` returns."""


def _moderator_limits() -> list[Any]:
    """Return the rate limits of a moderator resource: 120 reads and 30 writes a minute per API key and resource.

    Moderators share office addresses and VPN exits, so the count follows the key. Each limit is scoped to its
    resource's endpoint, so all of one resource's paths, such as the notes of every event, share one budget. A request
    without a key counts against the anonymous key's bucket, and the missing header refuses it anyway.
    """
    return [
        limiter.limit("120/minute", key_func=lim.get_request_api_key_per_method, methods=["GET"]),
        limiter.limit(
            "30/minute",
            key_func=lim.get_request_api_key_per_method,
            methods=[
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            ],
        ),
    ]


def _add_moderator_credentials(parser: reqparse.RequestParser) -> None:
    """Add the moderator key and client agent headers every resource here requires."""
    parser.add_argument("apikey", type=str, required=True, help="A mod API key.", location="headers")
    parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version.",
        location="headers",
    )


def _parse_utc_bound(value: str | None, field: str) -> datetime | None:
    """Convert an ISO 8601 bound with an explicit timezone into a naive UTC datetime.

    Raises:
        e.BadRequest: The value is not ISO 8601 or carries no timezone.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise e.BadRequest(f"{field} must be an ISO 8601 timestamp", rc="InvalidModerationTimeRange") from err
    if parsed.tzinfo is None:
        raise e.BadRequest(f"{field} must carry an explicit timezone", rc="InvalidModerationTimeRange")
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _assert_page_bounds(limit: int, before_id: int | None, maximum: int) -> None:
    """Reject a page size or descending cursor outside the documented range.

    Raises:
        e.BadRequest: The page size or the cursor is out of range.
    """
    if not 1 <= limit <= maximum:
        raise e.BadRequest(f"limit must be between 1 and {maximum}", rc="InvalidOperationsLimit")
    if before_id is not None and before_id < 1:
        raise e.BadRequest("before_id must be positive", rc="InvalidOperationsCursor")


def _parse_ipaddr_filter(value: str | None) -> str | None:
    """Parse the ``ipaddr`` filter into an IP subject; a missing or blank value restricts nothing.

    Raises:
        e.BadRequest: The value is neither an address nor an IPv6 network of /64 or narrower.
    """
    if value is None or not value.strip():
        return None
    try:
        return CounterMeasures.parse_ip_subject(value)
    except ValueError as err:
        raise e.BadRequest(
            "ipaddr must be an IP address or an IPv6 network of /64 or narrower",
            rc="InvalidModerationAddressFilter",
        ) from err


def _assert_ip_subject_key(value: str | None) -> str | None:
    """Validate the ``ip_subject_key`` filter.

    Raises:
        e.BadRequest: The value is not 64 lowercase hexadecimal characters.
    """
    if value is not None and IP_SUBJECT_KEY_PATTERN.fullmatch(value) is None:
        raise e.BadRequest("ip_subject_key must be 64 lowercase hexadecimal characters", rc="InvalidModerationAddressFilter")
    return value


def _assert_note(note: str) -> None:
    if not note.strip() or len(note) > MAX_NOTE_CHARACTERS or "\x00" in note:
        raise e.BadRequest(
            f"note must be 1 to {MAX_NOTE_CHARACTERS} characters and contain no NUL characters",
            rc="InvalidModerationNote",
        )


def _moderation_reasons(values: list[str] | None) -> list[PromptModerationReason] | None:
    """Validate repeated evidence reason filters.

    Raises:
        e.BadRequest: A value is not a moderation reason.
    """
    if values is None:
        return None
    reasons = []
    for value in values:
        try:
            reasons.append(PromptModerationReason(value))
        except ValueError as err:
            raise e.BadRequest(f"Unknown moderation reason {value}", rc="InvalidModerationReason") from err
    return reasons


class OperationsPromptEvents(Resource):
    """Return actionable evidence; ordinary filter replacements are not events."""

    decorators = _moderator_limits()

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)
    get_parser.add_argument("limit", type=int, default=50, help="Rows per page (1-100).", location="args")
    get_parser.add_argument("before_id", type=int, required=False, help="Exclusive descending cursor.", location="args")
    get_parser.add_argument(
        "event_id",
        type=int,
        required=False,
        help="Restrict to one event; the event_id of a moderator alert link.",
        location="args",
    )
    get_parser.add_argument("user_id", type=int, required=False, help="Restrict to one account.", location="args")
    get_parser.add_argument(
        "proxied_account",
        type=str,
        required=False,
        help="Restrict to one proxied account.",
        location="args",
    )
    get_parser.add_argument(
        "ipaddr",
        type=str,
        required=False,
        help="Restrict to one IP subject, including events whose address retention removed; an IPv6 address selects its /64.",
        location="args",
    )
    get_parser.add_argument(
        "ip_subject_key",
        type=str,
        required=False,
        help="Restrict to one address pseudonym, the ip_subject_key of an event.",
        location="args",
    )
    get_parser.add_argument("worker_id", type=str, required=False, help="Restrict to one reporting worker.", location="args")
    get_parser.add_argument(
        "reason",
        type=str,
        action="append",
        required=False,
        help="Restrict to events of any of these reasons; repeatable.",
        location="args",
    )
    get_parser.add_argument("since", type=str, required=False, help="Inclusive ISO 8601 lower bound.", location="args")
    get_parser.add_argument("until", type=str, required=False, help="Exclusive ISO 8601 upper bound.", location="args")

    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_prompt_moderation_events,
        code=200,
        description="Retained prompt moderation evidence",
    )
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def get(self) -> tuple[dict[str, Any], int, dict[str, str]]:
        """Return a bounded evidence page filtered by subject, time, or ID range."""
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsPromptEvents")
        _assert_page_bounds(self.args.limit, self.args.before_id, MAX_EVIDENCE_PAGE)
        since = _parse_utc_bound(self.args.since, "since")
        until = _parse_utc_bound(self.args.until, "until")
        if since and until and since >= until:
            raise e.BadRequest("since must be earlier than until", rc="InvalidModerationTimeRange")
        if self.args.event_id is not None and self.args.event_id < 1:
            raise e.BadRequest("event_id must be positive", rc="InvalidModerationEventID")
        events = evidence_db.get_prompt_events(
            limit=self.args.limit,
            before_id=self.args.before_id,
            event_id=self.args.event_id,
            user_id=self.args.user_id,
            proxied_account=self.args.proxied_account,
            ip_subject=_parse_ipaddr_filter(self.args.ipaddr),
            ip_subject_key=_assert_ip_subject_key(self.args.ip_subject_key),
            worker_id=self.args.worker_id,
            reasons=_moderation_reasons(self.args.reason),
            since=since,
            until=until,
        )
        # Signing a link is local computation, so a full page costs no object storage round trip.
        for event in events["events"]:
            event["text_url"] = r2.evidence_text_url(event["id"]) if event["text_state"] == EvidenceTextState.STORED else None
        policy = evidence_db.MODERATION_RETENTION_POLICY
        events["retention"] = {
            "text_days": policy.text_days,
            "ipaddr_days": policy.ipaddr_days,
            "ceiling_days": policy.ceiling_days,
        }
        # The page is fully materialized; release the pooled connection before marshalling.
        db.session.remove()
        return events, 200, PRIVATE_HEADERS


class OperationsPromptNotes(Resource):
    """Attach moderator notes to an event without modifying the evidence."""

    decorators = _moderator_limits()

    post_parser = reqparse.RequestParser()
    _add_moderator_credentials(post_parser)
    # Not required here: _assert_note refuses a missing note too, so every invalid note reports InvalidModerationNote.
    post_parser.add_argument(
        "note",
        type=str,
        required=False,
        help=f"Note text, 1 to {MAX_NOTE_CHARACTERS} characters.",
        location="json",
    )

    @api.expect(post_parser, models.input_model_prompt_moderation_note)
    @api.marshal_with(models.response_model_prompt_moderation_note, code=201, description="The stored note")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Moderation Event Not Found", models.response_model_error)
    def post(self, event_id: int) -> tuple[evidence_db.NoteRecord, int, dict[str, str]]:
        """Add a note to an event.

        Args:
            event_id: Evidence identifier returned by the event listing.

        Returns:
            The stored note.

        Raises:
            e.BadRequest: The note is missing, empty, too long, or carries a NUL byte.
            e.ModerationEventNotFound: The event does not exist.
        """
        self.args = self.post_parser.parse_args()
        moderator = check_for_mod(self.args.apikey, "POST OperationsPromptNotes")
        note = self.args.note or ""
        _assert_note(note)
        author_id = moderator.id
        # add_prompt_note owns its own transaction; release this request's first.
        db.session.remove()
        stored = evidence_db.add_prompt_note(event_id=event_id, author_id=author_id, note=note)
        if stored is None:
            raise e.ModerationEventNotFound(event_id)
        return stored, 201, PRIVATE_HEADERS


def _suspicion_ids(values: list[int] | None) -> list[int] | None:
    """Validate repeated suspicion reason codes.

    Raises:
        e.BadRequest: A code is not a known suspicion reason.
    """
    for value in values or []:
        try:
            Suspicions(value)
        except ValueError as err:
            raise e.BadRequest(f"Unknown suspicion_id {value}", rc="InvalidSuspicionID") from err
    return values


class OperationsModeration(Resource):
    """Expose current promotion and paused-worker review queues to moderators."""

    decorators = _moderator_limits()

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)
    get_parser.add_argument(
        "limit",
        default=100,
        type=int,
        required=False,
        help="Maximum rows returned in each review section (1-500).",
        location="args",
    )
    get_parser.add_argument(
        "worker_type",
        choices=list(moderation_db.WORKER_TYPE_IDENTITIES),
        action="append",
        required=False,
        help="Paused workers of these types; repeatable.",
        location="args",
    )
    get_parser.add_argument(
        "suspicion_id",
        type=int,
        action="append",
        required=False,
        help="Paused workers carrying any of these suspicion reasons; repeatable.",
        location="args",
    )
    get_parser.add_argument(
        "online",
        type=inputs.boolean,
        required=False,
        help="Paused workers that checked in within five minutes (true) or not (false).",
        location="args",
    )
    get_parser.add_argument(
        "sort",
        choices=list(moderation_db.PausedWorkerSort),
        default=moderation_db.PausedWorkerSort.LAST_CHECK_IN,
        help="Paused-worker order.",
        location="args",
    )

    @api.expect(get_parser)
    @api.marshal_with(models.response_model_moderation_overview, code=200, description="Moderation operations review")
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def get(self) -> tuple[moderation_db.ModerationOverview, int, dict[str, str]]:
        """Return promotion exceptions and paused workers for moderator review."""
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsModeration")
        _assert_page_bounds(self.args.limit, None, MAX_OPERATIONS_PAGE)
        overview = moderation_db.get_moderation_overview(
            limit=self.args.limit,
            worker_types=self.args.worker_type,
            suspicion_ids=_suspicion_ids(self.args.suspicion_id),
            online=self.args.online,
            sort=moderation_db.PausedWorkerSort(self.args.sort),
        )
        # The queues are fully materialized; release the pooled connection before marshalling.
        db.session.remove()
        return overview, 200, PRIVATE_HEADERS


class OperationsWorkerSuspicionEvents(Resource):
    """Expose retained worker suspicion history to moderators."""

    decorators = _moderator_limits()

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)
    get_parser.add_argument("limit", default=100, type=int, required=False, help="Rows per page (1-500).", location="args")
    get_parser.add_argument("before_id", type=int, required=False, help="Exclusive descending cursor.", location="args")
    get_parser.add_argument("worker_id", type=str, required=False, help="Restrict to one worker UUID.", location="args")
    get_parser.add_argument("user_id", type=int, required=False, help="Restrict to one owning account.", location="args")

    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_worker_suspicion_events,
        code=200,
        description="Retained worker suspicion history",
    )
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def get(self) -> tuple[moderation_db.WorkerSuspicionEventsPage, int, dict[str, str]]:
        """Return a cursor-paginated page of worker suspicion history, newest first."""
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsWorkerSuspicionEvents")
        _assert_page_bounds(self.args.limit, self.args.before_id, MAX_OPERATIONS_PAGE)
        if self.args.worker_id is not None:
            try:
                uuid.UUID(self.args.worker_id)
            except ValueError as err:
                raise e.BadRequest("worker_id must be a UUID", rc="InvalidWorkerID") from err
        page = moderation_db.get_worker_suspicion_events(
            limit=self.args.limit,
            before_id=self.args.before_id,
            worker_id=self.args.worker_id,
            user_id=self.args.user_id,
        )
        # The page is fully materialized; release the pooled connection before marshalling.
        db.session.remove()
        return page, 200, PRIVATE_HEADERS
