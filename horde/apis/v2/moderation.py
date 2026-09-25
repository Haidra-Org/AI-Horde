# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Expose moderator-only operational review over prompt evidence and worker state.

Every resource here requires a moderator ``apikey`` and answers with
``Cache-Control: private, no-store``: the payloads carry account identity,
IP addresses, and retained prompt text.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from flask_restx import Resource, reqparse

from horde import exceptions as e
from horde.apis.v2.base import api, check_for_mod, models
from horde.classes.base.prompt_moderation import PromptModerationReason, PromptReviewStatus
from horde.database import moderation as moderation_db
from horde.database.prompt_moderation import get_prompt_events, review_prompt_event
from horde.flask import db
from horde.limiter import limiter
from horde.suspicions import Suspicions

PRIVATE_HEADERS: dict[str, str] = {"Cache-Control": "private, no-store"}
MAX_REVIEW_NOTE_CHARACTERS: int = 2000
MAX_EVIDENCE_PAGE: int = 100
MAX_OPERATIONS_PAGE: int = 500


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

    Args:
        value: Submitted timestamp, or None when the bound was not given.
        field: Argument name, used in the client error message.

    Returns:
        The equivalent naive UTC datetime, or None.

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

    Args:
        limit: Requested page size.
        before_id: Requested exclusive descending cursor, if any.
        maximum: Largest page this listing serves.

    Raises:
        e.BadRequest: The page size or the cursor is out of range.
    """
    if not 1 <= limit <= maximum:
        raise e.BadRequest(f"limit must be between 1 and {maximum}", rc="InvalidOperationsLimit")
    if before_id is not None and before_id < 1:
        raise e.BadRequest("before_id must be positive", rc="InvalidOperationsCursor")


class OperationsPromptEvents(Resource):
    """Return actionable evidence; ordinary filter replacements are not events."""

    decorators = [limiter.limit("30/minute")]

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)
    get_parser.add_argument("limit", type=int, default=50, help="Rows per page (1-100).", location="args")
    get_parser.add_argument("before_id", type=int, required=False, help="Exclusive descending cursor.", location="args")
    get_parser.add_argument("user_id", type=int, required=False, help="Restrict to one account.", location="args")
    get_parser.add_argument(
        "reason",
        choices=[reason.value for reason in PromptModerationReason],
        required=False,
        help="Restrict to one evidence reason.",
        location="args",
    )
    get_parser.add_argument(
        "outcome",
        choices=["rejected", "censored"],
        required=False,
        help="Restrict to one moderation outcome.",
        location="args",
    )
    get_parser.add_argument(
        "status",
        choices=[status.value for status in PromptReviewStatus],
        required=False,
        help="Restrict to one review disposition.",
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
        """Return a bounded evidence page filtered by account, reason, time, or disposition."""
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsPromptEvents")
        _assert_page_bounds(self.args.limit, self.args.before_id, MAX_EVIDENCE_PAGE)
        since = _parse_utc_bound(self.args.since, "since")
        until = _parse_utc_bound(self.args.until, "until")
        if since and until and since >= until:
            raise e.BadRequest("since must be earlier than until", rc="InvalidModerationTimeRange")
        events = get_prompt_events(
            limit=self.args.limit,
            before_id=self.args.before_id,
            user_id=self.args.user_id,
            reason=PromptModerationReason(self.args.reason) if self.args.reason else None,
            outcome=self.args.outcome,
            status=PromptReviewStatus(self.args.status) if self.args.status else None,
            since=since,
            until=until,
        )
        # The page is fully materialized; release the pooled connection before marshalling.
        db.session.remove()
        return events, 200, PRIVATE_HEADERS


class OperationsPromptReview(Resource):
    """Record moderator attribution separately from immutable evidence."""

    decorators = [limiter.limit("30/minute")]

    patch_parser = reqparse.RequestParser()
    _add_moderator_credentials(patch_parser)
    patch_parser.add_argument(
        "status",
        choices=[status.value for status in PromptReviewStatus],
        required=True,
        nullable=False,
        help="New disposition; pending reopens the event.",
        location="json",
    )
    patch_parser.add_argument(
        "note",
        type=str,
        default="",
        nullable=False,
        help="Review note, at most 2000 characters.",
        location="json",
    )

    @api.expect(patch_parser, models.input_model_prompt_moderation_review, validate=True)
    @api.marshal_with(
        models.response_model_prompt_moderation_review,
        code=200,
        description="The saved disposition",
    )
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    @api.response(404, "Moderation Event Not Found", models.response_model_error)
    def patch(self, event_id: int) -> tuple[dict[str, Any], int, dict[str, str]]:
        """Set the current disposition of an event.

        Args:
            event_id: Evidence identifier returned by the event listing.

        Returns:
            The saved disposition.

        Raises:
            e.BadRequest: The note is too long or carries a NUL byte.
            e.ModerationEventNotFound: Retention already removed the event.
        """
        self.args = self.patch_parser.parse_args()
        moderator = check_for_mod(self.args.apikey, "PATCH OperationsPromptReview")
        if len(self.args.note) > MAX_REVIEW_NOTE_CHARACTERS or "\x00" in self.args.note:
            raise e.BadRequest(
                f"note must be at most {MAX_REVIEW_NOTE_CHARACTERS} characters and contain no NUL characters",
                rc="InvalidModerationNote",
            )
        reviewer_id = moderator.id
        # review_prompt_event owns its own transaction; release this request's first.
        db.session.remove()
        if not review_prompt_event(
            event_id=event_id,
            reviewer_id=reviewer_id,
            status=PromptReviewStatus(self.args.status),
            note=self.args.note,
        ):
            raise e.ModerationEventNotFound(event_id)
        return {"id": event_id, "status": self.args.status, "reviewer_id": reviewer_id}, 200, PRIVATE_HEADERS


class OperationsModeration(Resource):
    """Expose current promotion and paused-worker review queues to moderators."""

    decorators = [limiter.limit("30/minute")]

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
        overview = moderation_db.get_moderation_overview(limit=self.args.limit)
        # The queues are fully materialized; release the pooled connection before marshalling.
        db.session.remove()
        return overview, 200, PRIVATE_HEADERS


class OperationsWorkerSuspicionEvents(Resource):
    """Expose immutable worker suspicion history to moderators."""

    decorators = [limiter.limit("30/minute")]

    get_parser = reqparse.RequestParser()
    _add_moderator_credentials(get_parser)
    get_parser.add_argument("limit", default=100, type=int, required=False, help="Rows per page (1-500).", location="args")
    get_parser.add_argument("before_id", type=int, required=False, help="Exclusive descending cursor.", location="args")
    get_parser.add_argument("worker_id", type=str, required=False, help="Restrict to one worker UUID.", location="args")
    get_parser.add_argument("user_id", type=int, required=False, help="Restrict to one owning account.", location="args")
    get_parser.add_argument("suspicion_id", type=int, required=False, help="Restrict to one suspicion reason.", location="args")

    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_worker_suspicion_events,
        code=200,
        description="Immutable worker suspicion event history",
    )
    @api.response(400, "Validation Error", models.response_model_error)
    @api.response(401, "Invalid API Key", models.response_model_error)
    @api.response(403, "Access Denied", models.response_model_error)
    def get(self) -> tuple[moderation_db.WorkerSuspicionEventsPage, int, dict[str, str]]:
        """Return a cursor-paginated worker suspicion audit stream."""
        self.args = self.get_parser.parse_args()
        check_for_mod(self.args.apikey, "GET OperationsWorkerSuspicionEvents")
        _assert_page_bounds(self.args.limit, self.args.before_id, MAX_OPERATIONS_PAGE)
        if self.args.worker_id is not None:
            try:
                uuid.UUID(self.args.worker_id)
            except ValueError as err:
                raise e.BadRequest("worker_id must be a UUID", rc="InvalidWorkerID") from err
        if self.args.suspicion_id is not None:
            try:
                Suspicions(self.args.suspicion_id)
            except ValueError as err:
                raise e.BadRequest("Unknown suspicion_id", rc="InvalidSuspicionID") from err
        page = moderation_db.get_worker_suspicion_events(
            limit=self.args.limit,
            before_id=self.args.before_id,
            worker_id=self.args.worker_id,
            user_id=self.args.user_id,
            suspicion_id=self.args.suspicion_id,
        )
        # The page is fully materialized; release the pooled connection before marshalling.
        db.session.remove()
        return page, 200, PRIVATE_HEADERS
