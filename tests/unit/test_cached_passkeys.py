# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for the proxy passkey check that decides whether a request's ``Proxied-For`` header is trusted.

A trusted proxy's ``Proxied-For`` header replaces the request's address for IP timeouts, suspicion and moderation
records, so only a request carrying a known passkey may set it.
"""

from __future__ import annotations

import pytest
from flask import Flask

from horde.apis import request_utils
from horde.database.classes import CachedPasskeys

KNOWN_PASSKEY: str = "known-proxy-passkey"
"""The proxy passkey of the one service account in the cache."""

PASSKEY_OWNER_ID: int = 7
"""The id of the service account that owns ``KNOWN_PASSKEY``."""

PROXY_ADDRESS: str = "198.51.100.7"
"""The address the request arrives from, the proxy's own."""

END_USER_ADDRESS: str = "203.0.113.9"
"""The address the request names in ``Proxied-For``."""


def _cached_passkeys(passkeys: dict[int, str]) -> CachedPasskeys:
    """Return a passkey cache holding ``passkeys`` without starting its refresh thread."""
    cache = CachedPasskeys.__new__(CachedPasskeys)
    cache.passkeys = passkeys
    return cache


def test_known_passkey_is_known() -> None:
    assert _cached_passkeys({PASSKEY_OWNER_ID: KNOWN_PASSKEY}).is_passkey_known(KNOWN_PASSKEY)


def test_unknown_passkey_is_not_known_while_other_passkeys_exist() -> None:
    assert not _cached_passkeys({PASSKEY_OWNER_ID: KNOWN_PASSKEY}).is_passkey_known("guessed-passkey")


@pytest.mark.parametrize("passkey", [None, ""])
def test_missing_passkey_is_not_known(passkey: str | None) -> None:
    assert not _cached_passkeys({PASSKEY_OWNER_ID: KNOWN_PASSKEY}).is_passkey_known(passkey)


def test_no_passkey_is_known_without_cached_passkeys() -> None:
    assert not _cached_passkeys({}).is_passkey_known(KNOWN_PASSKEY)


def test_non_ascii_passkey_is_not_known() -> None:
    assert not _cached_passkeys({PASSKEY_OWNER_ID: KNOWN_PASSKEY}).is_passkey_known("pässkey")


def test_known_passkey_resolves_to_its_owner() -> None:
    cache = _cached_passkeys({1: "other-proxy-passkey", PASSKEY_OWNER_ID: KNOWN_PASSKEY})
    assert cache.get_passkey_owner(KNOWN_PASSKEY) == PASSKEY_OWNER_ID


@pytest.mark.parametrize("passkey", [None, "", "guessed-passkey"])
def test_unknown_or_missing_passkey_has_no_owner(passkey: str | None) -> None:
    assert _cached_passkeys({PASSKEY_OWNER_ID: KNOWN_PASSKEY}).get_passkey_owner(passkey) is None


@pytest.mark.parametrize(
    ("passkey", "expected_address"),
    [
        (KNOWN_PASSKEY, END_USER_ADDRESS),
        ("guessed-passkey", PROXY_ADDRESS),
    ],
)
def test_proxied_for_is_trusted_only_with_a_known_passkey(
    monkeypatch: pytest.MonkeyPatch,
    passkey: str,
    expected_address: str,
) -> None:
    monkeypatch.setattr(request_utils, "get_cached_passkeys", lambda: _cached_passkeys({PASSKEY_OWNER_ID: KNOWN_PASSKEY}))
    headers = {"Proxy-Authorization": passkey, "Proxied-For": END_USER_ADDRESS}
    with Flask(__name__).test_request_context(headers=headers, environ_base={"REMOTE_ADDR": PROXY_ADDRESS}):
        assert request_utils.get_remoteaddr() == expected_address
