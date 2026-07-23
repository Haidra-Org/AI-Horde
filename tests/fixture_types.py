# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared typing helpers for the test-suite fixtures.

Provides call-signature Protocols for the object-factory fixtures so consuming
tests can annotate the injected factories precisely instead of falling back to
``Callable[..., ...]``. The legacy ORM models (``User``, ``UserRole``) are
untyped, so factory keyword overrides are honestly typed as ``Any``.

Public members:
    ApiUser: Immutable view of a registered user returned by ``make_api_user``.
    MakeUser: Call signature of the ``make_user`` fixture factory.
    MakeUserRole: Call signature of the ``make_user_role`` fixture factory.
    MakeApiUser: Call signature of the ``make_api_user`` fixture factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from horde.classes.base.user import User, UserRole
    from horde.enums import UserRoleTypes

__all__ = [
    "ApiUser",
    "MakeApiUser",
    "MakeUser",
    "MakeUserRole",
]


@dataclass(frozen=True)
class ApiUser:
    """Represents the registered user created by the ``make_api_user`` factory.

    Carries only the fields endpoint tests need to act as the user: its database
    id, plaintext API key, username, and unique alias. Immutable because every
    consumer treats it as a read-only handle.
    """

    id: int
    api_key: str
    username: str
    alias: str


class MakeUser(Protocol):
    """Call signature of the ``make_user`` fixture factory."""

    def __call__(self, **overrides: Any) -> User:
        """Build and persist a ``User``, applying column keyword overrides."""
        ...


class MakeUserRole(Protocol):
    """Call signature of the ``make_user_role`` fixture factory."""

    def __call__(self, user: User, role_type: UserRoleTypes, *, value: bool = True) -> UserRole:
        """Attach a ``UserRole`` of ``role_type`` to ``user``."""
        ...


class MakeApiUser(Protocol):
    """Call signature of the ``make_api_user`` fixture factory."""

    def __call__(self, *, trusted: bool = False, moderator: bool = False, kudos: int = 0) -> ApiUser:
        """Create a registered user at the requested privilege and kudos level."""
        ...
