# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat submit route holds an address to the chat limits, apart from the text submit limits."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

import horde.apis.limiter_api as lim
from horde.config import Config
from horde.limiter import limiter

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

pytestmark: pytest.MarkDecorator = pytest.mark.unit

CHAT_SUBMIT_PATH = "/api/v2/generate/chat/async"
"""The chat submit route."""

TEXT_SUBMIT_PATH = "/api/v2/generate/text/async"
"""The text submit route."""

TEST_CHAT_LIMIT = "1/hour"
"""A chat limit one submit exhausts. An hour window keeps the second submit inside it however slowly the test runs."""


def unique_address() -> dict[str, str]:
    """Return a WSGI environment with an address no other test uses, so limit counters never carry over.

    Returns:
        The environment to pass as ``environ_base``.
    """
    return {"REMOTE_ADDR": f"10.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 254 + 1}"}


@pytest.fixture(autouse=True)
def enabled_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable rate limits, which another test may have turned off."""
    monkeypatch.setattr(limiter, "enabled", True)


class TestChatSubmitRateLimit:
    def test_chat_limit_refuses_the_second_chat_submit_while_text_submits_pass(
        self,
        client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With the chat per-second limit set to one submit, the second chat submit from an address returns 429, and a
        text submit from the same address is not limited."""
        monkeypatch.setattr(Config, "chat_request_2sec_limit_per_ip", TEST_CHAT_LIMIT)
        environ = unique_address()

        first_chat = client.post(CHAT_SUBMIT_PATH, json={}, environ_base=environ)
        second_chat = client.post(CHAT_SUBMIT_PATH, json={}, environ_base=environ)
        first_text = client.post(TEXT_SUBMIT_PATH, json={}, environ_base=environ)
        second_text = client.post(TEXT_SUBMIT_PATH, json={}, environ_base=environ)

        assert first_chat.status_code != 429
        assert second_chat.status_code == 429
        assert first_text.status_code != 429
        assert second_text.status_code != 429

    def test_unset_chat_limits_equal_the_text_limits(
        self,
        app: Flask,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no chat limits set, an address is held to the text submit limits on the chat submit route."""
        monkeypatch.setattr(Config, "chat_request_2sec_limit_per_ip", None)
        monkeypatch.setattr(Config, "chat_request_90min_limit_per_ip", None)

        with app.test_request_context(CHAT_SUBMIT_PATH, method="POST", environ_base=unique_address()):
            chat_limits = (lim.get_chat_request_2sec_limit_per_ip(), lim.get_chat_request_90min_limit_per_ip())
            text_limits = (lim.get_request_2sec_limit_per_ip(), lim.get_request_90min_limit_per_ip())

        assert chat_limits == text_limits == (lim.REQUEST_2SEC_LIMIT_PER_IP, lim.REQUEST_90MIN_LIMIT_PER_IP)
