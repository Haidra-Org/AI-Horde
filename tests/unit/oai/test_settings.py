# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's settings: environment loading, validation and credential-free diagnostics."""

from __future__ import annotations

import os

import pytest
from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_TEXT_BYTES
from pydantic import ValidationError

from horde.chat_contract import MAX_CHAT_BODY_BYTES, MAX_CHAT_MESSAGE_COUNT
from horde.oai.settings import DEFAULT_HORDE_URL, FrontSettings, load_settings


@pytest.fixture(autouse=True)
def clear_front_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``HORDE_OAI_*`` variable and leave the working directory's ``.env`` unread."""
    for variable_name in list(os.environ):
        if variable_name.startswith("HORDE_OAI_"):
            monkeypatch.delenv(variable_name)
    monkeypatch.setitem(FrontSettings.model_config, "env_file", None)


def test_settings_repr_omits_secrets_without_changing_runtime_values() -> None:
    settings = FrontSettings(proxy_passkey="secret-passkey", redis_url="redis://user:secret-password@localhost:6379/0")
    for display in (str(settings), repr(settings)):
        assert "secret-passkey" not in display
        assert "secret-password" not in display
    assert settings.proxy_passkey == "secret-passkey"
    assert settings.redis_url == "redis://user:secret-password@localhost:6379/0"


def test_defaults_match_the_documented_values() -> None:
    settings = load_settings()

    assert settings.horde_urls == (DEFAULT_HORDE_URL,)
    assert settings.max_inflight == 100
    assert settings.max_inflight_per_source == 5
    assert settings.nonstream_timeout_seconds == 300
    assert settings.stream_timeout_seconds == 600
    assert settings.final_status_timeout_seconds == 2
    assert settings.cancel_timeout_seconds == 10
    assert settings.heartbeat_seconds == 5
    assert settings.fallback_poll_seconds == 15
    assert settings.fail_fast_grace_seconds == 20
    assert settings.submit_recovery_attempts == 2
    assert settings.max_body_bytes == MAX_CHAT_BODY_BYTES
    assert settings.max_message_count == MAX_CHAT_MESSAGE_COUNT
    assert settings.max_text_bytes == MAX_CHAT_TEXT_BYTES
    assert settings.redis_url is None
    assert settings.redis_liveness_seconds == 30
    assert settings.proxy_passkey is None


def test_settings_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HORDE_OAI_HORDE_URLS", " http://a.test/api/ , http://b.test/api,")
    monkeypatch.setenv("HORDE_OAI_MAX_INFLIGHT", "7")
    monkeypatch.setenv("HORDE_OAI_STREAM_TIMEOUT_SECONDS", "42.5")
    monkeypatch.setenv("HORDE_OAI_FORWARDED_ALLOW_IPS", "10.0.0.1,10.0.0.2")
    monkeypatch.setenv("HORDE_OAI_PROXY_PASSKEY", "operator-passkey")
    monkeypatch.setenv("HORDE_OAI_REDIS_LIVENESS_SECONDS", "12.5")

    settings = load_settings()

    assert settings.horde_urls == ("http://a.test/api", "http://b.test/api")
    assert settings.max_inflight == 7
    assert settings.stream_timeout_seconds == 42.5
    assert settings.forwarded_allow_ips == "10.0.0.1,10.0.0.2"
    assert settings.proxy_passkey == "operator-passkey"
    assert settings.redis_liveness_seconds == 12.5


@pytest.mark.parametrize(
    ("variable_name", "invalid_value"),
    [
        ("HORDE_OAI_MAX_INFLIGHT", "many"),
        ("HORDE_OAI_MAX_INFLIGHT", "0"),
        ("HORDE_OAI_PORT", "70000"),
        ("HORDE_OAI_NONSTREAM_TIMEOUT_SECONDS", "-1"),
        ("HORDE_OAI_REDIS_LIVENESS_SECONDS", "0"),
        ("HORDE_OAI_HORDE_URLS", " , "),
    ],
)
def test_invalid_environment_value_fails_naming_the_setting(
    monkeypatch: pytest.MonkeyPatch, variable_name: str, invalid_value: str
) -> None:
    monkeypatch.setenv(variable_name, invalid_value)

    with pytest.raises(ValidationError) as raised:
        load_settings()

    assert variable_name.removeprefix("HORDE_OAI_").lower() in str(raised.value)
