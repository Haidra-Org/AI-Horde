# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ``horde`` extension object of a chat request, shared by the native chat route and the OpenAI-compatible front.

Both the native ``POST /v2/generate/chat/async`` route and the ``horde.oai`` front validate a request's ``horde``
object against [`ChatHordeOptions`][horde.chat_contract.ChatHordeOptions], so the two cannot disagree on which keys
exist or what values they take. The module imports only pydantic and the standard library, so the front can import it
without the Flask application, its database or its configuration.

``dry_run`` and ``operation_key`` belong to native callers: the front refuses both in a public request body (see
[`INTERNAL_HORDE_OPTION_KEYS`][horde.chat_contract.INTERNAL_HORDE_OPTION_KEYS]) and supplies the operation key itself.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

__all__ = [
    "CHAT_REQUEST_CONFLICT_ERROR_TYPE",
    "INTERNAL_HORDE_OPTION_KEYS",
    "MAX_CHAT_BODY_BYTES",
    "MAX_CHAT_MESSAGE_COUNT",
    "MAX_CHAT_TEXT_BYTES",
    "ChatContextFit",
    "ChatHordeOptions",
]

MAX_CHAT_MESSAGE_COUNT: int = 1024
"""The most messages one chat request may carry. A conversation longer than this outgrows any served context long
before it reaches the cap, so the cap only refuses bodies built to make validation and flattening expensive."""

MAX_CHAT_TEXT_BYTES: int = 1024 * 1024
"""The most UTF-8 bytes the message texts of one chat request may hold together. 1 MiB is several times the text of
the largest context a worker serves, so no servable conversation reaches it, while the flattened estimate,
moderation and the stored messages stay bounded."""

MAX_CHAT_BODY_BYTES: int = 2 * 1024 * 1024
"""The most bytes a chat request body may have. Twice the text limit leaves room for JSON structure and escaping of
a body at the text limit, and bounds what is read before the body is parsed."""

CHAT_REQUEST_CONFLICT_ERROR_TYPE: str = "chat_request_conflict"
"""The pydantic error type raised for two keys whose values contradict each other.

Callers map it to the ``ChatRequestConflict`` return code rather than to a generic validation failure, so a client can
tell a contradiction from a malformed value.
"""

INTERNAL_HORDE_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "dry_run",
        "operation_key",
    },
)
"""The ``horde`` keys only native callers may send; the public front refuses them by name."""


class ChatContextFit(StrEnum):
    """The ways a chat request's context length is fitted to its messages."""

    GROW = "grow"
    """Grow the context up to what an online chat worker serves."""

    REJECT = "reject"
    """Refuse a request whose messages do not fit the requested context."""


class ChatHordeOptions(BaseModel):
    """Represents the ``horde`` extension object of a chat request: options OpenAI has no key for.

    Every field defaults to the behaviour of a request that omits it, so a caller forwarding only the keys a client set
    does not change the request's meaning.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    only: list[str] | None = Field(default=None, min_length=1)
    """Worker ids the request is limited to; cannot be combined with ``ignore``."""

    ignore: list[str] | None = Field(default=None, min_length=1)
    """Worker ids that never serve the request; cannot be combined with ``only``."""

    trusted_workers: StrictBool = False
    """Limit the request to trusted workers."""

    validated_backends: StrictBool = False
    """Limit the request to workers running a validated inference backend."""

    slow_workers: StrictBool = True
    """Allow workers below the speed threshold to serve the request."""

    max_context_length: StrictInt | None = Field(default=None, ge=1)
    """An explicit context length in tokens, in place of the one fitted to the messages."""

    context_fit: ChatContextFit = ChatContextFit.GROW
    """What to do when the messages do not fit the context."""

    dry_run: StrictBool = False
    """Return the kudos cost without queueing; native callers only."""

    operation_key: UUID | None = None
    """Identifies one submission so a repeated submit returns the existing request; native callers only."""

    @field_validator("ignore")
    @classmethod
    def validate_ignore_excludes_only(cls, ignore: list[str] | None, info: ValidationInfo) -> list[str] | None:
        """Validate that ``ignore`` is not combined with ``only``.

        Raises:
            PydanticCustomError: Both lists are set, with the type
                [`CHAT_REQUEST_CONFLICT_ERROR_TYPE`][horde.chat_contract.CHAT_REQUEST_CONFLICT_ERROR_TYPE].
        """
        if ignore is not None and info.data.get("only") is not None:
            raise PydanticCustomError(
                CHAT_REQUEST_CONFLICT_ERROR_TYPE,
                "'only' and 'ignore' cannot both be set.",
            )
        return ignore
