# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the ``Client-Agent`` the front builds from a client's identifying headers."""

from __future__ import annotations

from horde.consts import HORDE_VERSION
from horde.oai.attribution import (
    CLIENT_AGENT_NAME,
    FRONT_MARKER,
    MAX_APPLICATION_CHARACTERS,
    MAX_CLIENT_AGENT_CHARACTERS,
    build_client_agent,
)

SDK_USER_AGENT: str = "OpenAI/Python 1.109.1"
"""The user agent the pinned OpenAI Python SDK sends."""


def test_a_title_names_the_application_ahead_of_the_other_headers() -> None:
    client_agent = build_client_agent(
        user_agent=SDK_USER_AGENT,
        referer="https://chat.example.org/session/1",
        title="Example Chat",
        openrouter_title="Example Chat (OpenRouter)",
        standard_referer="https://other.example.org/",
    )

    assert client_agent == f"Example Chat:0:{SDK_USER_AGENT}{FRONT_MARKER}"


def test_the_openrouter_title_names_the_application_without_an_x_title() -> None:
    client_agent = build_client_agent(
        user_agent=SDK_USER_AGENT,
        referer="https://chat.example.org/",
        title=None,
        openrouter_title="Example Chat",
        standard_referer=None,
    )

    assert client_agent == f"Example Chat:0:{SDK_USER_AGENT}{FRONT_MARKER}"


def test_the_referer_host_names_the_application_without_a_title() -> None:
    client_agent = build_client_agent(
        user_agent=SDK_USER_AGENT,
        referer="https://Chat.Example.org:8443/session/1?q=1",
        title="   ",
        openrouter_title=None,
        standard_referer="https://other.example.org/",
    )

    assert client_agent == f"chat.example.org:0:{SDK_USER_AGENT}{FRONT_MARKER}"


def test_a_referer_without_a_scheme_still_names_its_host() -> None:
    client_agent = build_client_agent(
        user_agent=SDK_USER_AGENT, referer="chat.example.org/page", title=None, openrouter_title=None, standard_referer=None
    )

    assert client_agent == f"chat.example.org:0:{SDK_USER_AGENT}{FRONT_MARKER}"


def test_the_standard_referer_names_the_application_after_the_openrouter_referer() -> None:
    client_agent = build_client_agent(
        user_agent=SDK_USER_AGENT,
        referer=None,
        title=None,
        openrouter_title=None,
        standard_referer="https://web.example.org/chat",
    )

    assert client_agent == f"web.example.org:0:{SDK_USER_AGENT}{FRONT_MARKER}"


def test_the_front_marker_tells_front_traffic_from_the_applications_direct_calls() -> None:
    client_agent = build_client_agent(
        user_agent=SDK_USER_AGENT, referer=None, title="Example Chat", openrouter_title=None, standard_referer=None
    )

    assert client_agent.endswith(" via horde-oai-front")


def test_only_a_user_agent_attributes_the_request_to_the_front_and_keeps_the_user_agent() -> None:
    client_agent = build_client_agent(user_agent=SDK_USER_AGENT, referer=None, title=None, openrouter_title=None, standard_referer=None)

    assert client_agent == f"{CLIENT_AGENT_NAME}:{HORDE_VERSION}:{SDK_USER_AGENT}{FRONT_MARKER}"


def test_no_headers_fall_back_to_the_front_identity() -> None:
    client_agent = build_client_agent(user_agent=None, referer=None, title=None, openrouter_title=None, standard_referer=None)

    assert client_agent == f"{CLIENT_AGENT_NAME}:{HORDE_VERSION}:unknown{FRONT_MARKER}"


def test_headers_with_nothing_usable_fall_back_to_the_front_identity() -> None:
    client_agent = build_client_agent(
        user_agent="\x00\x1b:", referer="https://", title="::", openrouter_title="\t\n", standard_referer="::"
    )

    assert client_agent == f"{CLIENT_AGENT_NAME}:{HORDE_VERSION}:unknown{FRONT_MARKER}"


def test_oversized_parts_are_truncated_to_the_documented_bounds() -> None:
    client_agent = build_client_agent(user_agent="u" * 5000, referer=None, title="t" * 500, openrouter_title=None, standard_referer=None)

    application, version, contact = client_agent.split(":")
    assert len(client_agent) == MAX_CLIENT_AGENT_CHARACTERS
    assert application == "t" * MAX_APPLICATION_CHARACTERS
    assert version == "0"
    assert contact == "u" * (MAX_CLIENT_AGENT_CHARACTERS - MAX_APPLICATION_CHARACTERS - len("0") - 2 - len(FRONT_MARKER)) + FRONT_MARKER


def test_an_oversized_user_agent_alone_stays_within_the_bound() -> None:
    client_agent = build_client_agent(user_agent="u" * 5000, referer=None, title=None, openrouter_title=None, standard_referer=None)

    assert len(client_agent) == MAX_CLIENT_AGENT_CHARACTERS
    assert client_agent.startswith(f"{CLIENT_AGENT_NAME}:{HORDE_VERSION}:uuu")
    assert client_agent.endswith(FRONT_MARKER)


def test_control_characters_and_colons_are_removed_so_the_value_keeps_three_fields() -> None:
    client_agent = build_client_agent(
        user_agent="Evil\r\nClient-Agent: forged:9:x\x00​",
        referer=None,
        title="My\x07 App:v2\u0085",
        openrouter_title=None,
        standard_referer=None,
    )

    assert client_agent == f"My Appv2:0:EvilClient-Agent forged9x{FRONT_MARKER}"
    assert client_agent.count(":") == 2
    assert all(character.isprintable() for character in client_agent)


def test_non_ascii_attribution_is_safe_for_the_native_http_header() -> None:
    import httpx

    agent = build_client_agent(
        user_agent="SDK/1 café", referer="https://éxample.org/", title="café", openrouter_title=None, standard_referer=None
    )
    request = httpx.Request("POST", "https://horde.example/", headers={"Client-Agent": agent})
    assert request.headers["Client-Agent"].isascii()
    assert "caf" in agent
    assert agent.endswith(FRONT_MARKER)
