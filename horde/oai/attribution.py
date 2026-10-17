# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Build the ``Client-Agent`` the front sends the Horde from the client's identifying headers.

The Horde stores the ``Client-Agent`` on every request and its generation statistics, and reads it as
``name:version:contact``. [`build_client_agent`][horde.oai.attribution.build_client_agent] fills it as
``<application>:<version>:<user agent>``:

- the application is the first usable of ``X-Title``, ``X-OpenRouter-Title``, the host of ``HTTP-Referer`` (the
  headers OpenRouter-style clients send to name themselves) and the host of the standard ``Referer``; without one it
  is [`CLIENT_AGENT_NAME`][horde.oai.attribution.CLIENT_AGENT_NAME];
- the version is ``0`` for a named application, since those headers carry no version, and the front's
  ``HORDE_VERSION`` when the application is the front;
- the contact is the client's ``User-Agent``, or ``unknown`` without one, followed by
  [`FRONT_MARKER`][horde.oai.attribution.FRONT_MARKER].

Statistics must be able to tell traffic through the front from direct native calls by the same application, which
send the same application name, so the contact always ends with the front marker.

Every part has control and format characters and colons removed and whitespace runs collapsed, so a client cannot
add a field or break a log line. The whole value is at most
[`MAX_CLIENT_AGENT_CHARACTERS`][horde.oai.attribution.MAX_CLIENT_AGENT_CHARACTERS]; the user agent is truncated
first, so the front marker is always kept.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

from horde.consts import HORDE_VERSION

__all__ = [
    "CLIENT_AGENT_NAME",
    "FRONT_MARKER",
    "MAX_APPLICATION_CHARACTERS",
    "MAX_CLIENT_AGENT_CHARACTERS",
    "UNKNOWN_CONTACT",
    "UNVERSIONED_APPLICATION_VERSION",
    "build_client_agent",
    "sanitise_client_agent_part",
]

CLIENT_AGENT_NAME: str = "horde-oai-front"
"""The application name of a request that sends no usable ``X-Title``, ``X-OpenRouter-Title`` or ``HTTP-Referer``."""

FRONT_MARKER: str = f" via {CLIENT_AGENT_NAME}"
"""The suffix of every contact part, marking the request as sent through the front."""

UNVERSIONED_APPLICATION_VERSION: str = "0"
"""The version of an application named by its attribution headers, which carry no version."""

UNKNOWN_CONTACT: str = "unknown"
"""The contact part of a request that sends no usable ``User-Agent``; the Horde's own default uses the same word."""

MAX_CLIENT_AGENT_CHARACTERS: int = 256
"""The most characters of the whole ``Client-Agent``.

The value is stored with every request and on every indexed generation statistics row, and statistics group by the
whole string, so a bound keeps one client from writing long values per request. 256 holds a long application name
and a typical SDK or browser user agent.
"""

MAX_APPLICATION_CHARACTERS: int = 80
"""The most characters of the application part, so a long title still leaves room for the user agent."""

CLIENT_AGENT_SEPARATOR: str = ":"
"""The separator between the ``Client-Agent`` fields; it is removed from every part."""

REMOVED_CHARACTER_CATEGORY_PREFIX: str = "C"
"""The Unicode general category prefix of control, format, surrogate, private-use and unassigned characters."""


def sanitise_client_agent_part(raw_part: str | None, *, max_characters: int) -> str:
    """Return ``raw_part`` fit for one ``Client-Agent`` field, or an empty string when nothing usable remains.

    Args:
        raw_part: A header value, or None when the header is absent.
        max_characters: The most characters kept, counted after cleaning.

    Returns:
        The ASCII value without control, format or other ``C*`` category characters and colons, whitespace runs collapsed to
        one space, stripped and truncated.
    """
    if raw_part is None:
        return ""
    kept_characters = (
        character
        for character in raw_part
        if character.isascii()
        and character != CLIENT_AGENT_SEPARATOR
        and not unicodedata.category(character).startswith(REMOVED_CHARACTER_CATEGORY_PREFIX)
    )
    collapsed_part = " ".join("".join(kept_characters).split())
    return collapsed_part[:max_characters].strip()


def _referer_host(referer: str | None) -> str | None:
    """Return the host of a ``HTTP-Referer`` or ``Referer`` value, accepting one without a scheme, or None when it has none."""
    if referer is None or not referer.strip():
        return None
    stripped_referer = referer.strip()
    if "//" not in stripped_referer:
        stripped_referer = f"//{stripped_referer}"
    try:
        return urlsplit(stripped_referer).hostname
    except ValueError:
        return None


def build_client_agent(
    *,
    user_agent: str | None,
    referer: str | None,
    title: str | None,
    openrouter_title: str | None,
    standard_referer: str | None,
) -> str:
    """Create the ``Client-Agent`` for a request from its identifying headers.

    Args:
        user_agent: The ``User-Agent`` header, or None.
        referer: The ``HTTP-Referer`` header, or None.
        title: The ``X-Title`` header, or None.
        openrouter_title: The ``X-OpenRouter-Title`` header, or None.
        standard_referer: The standard ``Referer`` header, or None; used after ``HTTP-Referer``.

    Returns:
        ``<application>:<version>:<user agent> via horde-oai-front``, at most
        [`MAX_CLIENT_AGENT_CHARACTERS`][horde.oai.attribution.MAX_CLIENT_AGENT_CHARACTERS] characters; with no usable
        header, ``horde-oai-front:<HORDE_VERSION>:unknown via horde-oai-front``.
    """
    application = ""
    for candidate in (title, openrouter_title, _referer_host(referer), _referer_host(standard_referer)):
        application = sanitise_client_agent_part(candidate, max_characters=MAX_APPLICATION_CHARACTERS)
        if application:
            break
    version = UNVERSIONED_APPLICATION_VERSION
    if not application:
        application = CLIENT_AGENT_NAME
        version = HORDE_VERSION
    remaining_characters = (
        MAX_CLIENT_AGENT_CHARACTERS - len(application) - len(version) - 2 * len(CLIENT_AGENT_SEPARATOR) - len(FRONT_MARKER)
    )
    user_agent_part = sanitise_client_agent_part(user_agent, max_characters=remaining_characters) or UNKNOWN_CONTACT
    return CLIENT_AGENT_SEPARATOR.join((application, version, f"{user_agent_part}{FRONT_MARKER}"))
