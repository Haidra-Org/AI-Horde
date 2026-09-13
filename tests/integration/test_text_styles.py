# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The text style endpoints: creating a style and reading it back.

A style stores a prompt template, a params body and a model list. Its details are served through the
text style params model, so only the params that model declares come back out.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from flask.testing import FlaskClient

TEXT_MODELS = ["elinas/chronos-70b-v2"]

STYLE_PROMPT = "### Instruction:\n{p}\n\n### Response:\n"


@pytest.fixture(autouse=True, scope="module")
def _no_rate_limit() -> Iterator[None]:
    """Turn the per-path rate limit off for this module.

    Creating a style is capped at two calls a second and twenty an hour, which these cases would run
    through.

    Yields:
        Nothing. The previous setting is put back afterwards.
    """
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def style_body(name: str, **overrides: Any) -> dict[str, Any]:
    """Build the body that creates a text style.

    Args:
        name: The style name, which is unique per user.
        **overrides: Keys to add to or replace in the body.

    Returns:
        The creation body.
    """
    body: dict[str, Any] = {
        "name": name,
        "info": "A style used by the text style endpoint tests.",
        "prompt": STYLE_PROMPT,
        "params": {"max_length": 80, "max_context_length": 1024, "temperature": 0.7},
        "models": TEXT_MODELS,
        "public": False,
        "nsfw": False,
    }
    body.update(overrides)
    return body


@contextmanager
def created_style(
    client: FlaskClient,
    request_headers: dict[str, str],
    body: dict[str, Any],
) -> Iterator[str]:
    """Create a text style for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the style belongs to.
        body: The creation body.

    Yields:
        The id of the new style.
    """
    response = client.post("/api/v2/styles/text", json=body, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    style_id = response.get_json()["id"]
    try:
        yield style_id
    finally:
        client.delete(f"/api/v2/styles/text/{style_id}", headers=request_headers)


class TestTextStyleCreate:
    """Creating a text style and reading its details back."""

    def test_the_details_repeat_what_the_style_was_created_with(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        with created_style(client, request_headers, style_body("create and read back")) as style_id:
            response = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            details = response.get_json()
            assert details["name"] == "create and read back"
            assert details["info"] == "A style used by the text style endpoint tests."
            assert details["prompt"] == STYLE_PROMPT
            assert details["models"] == TEXT_MODELS
            assert details["public"] is False
            assert details["nsfw"] is False
            assert details["use_count"] == 0

    def test_the_params_carry_the_values_the_style_set(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("create params")) as style_id:
            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()

            assert details["params"]["temperature"] == 0.7
            # A param the style never set is not invented for it.
            assert "top_k" not in details["params"]


class TestTextStylePartialPatch:
    """A patch changes only the fields it carries and leaves the rest of the style alone."""

    patch_body = {"info": "Only the description changes in this patch."}

    def test_a_patch_without_a_prompt_keeps_the_prompt(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("partial patch prompt")) as style_id:
            patch_response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json=self.patch_body,
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
            assert details["prompt"] == STYLE_PROMPT
            assert details["info"] == self.patch_body["info"]

    def test_a_patch_without_a_name_keeps_the_name(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("partial patch name")) as style_id:
            patch_response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json=self.patch_body,
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
            assert details["name"] == "partial patch name"

    def test_a_patch_without_public_or_nsfw_keeps_them(self, client, request_headers: dict[str, str]) -> None:
        # Both values are the opposite of the creation parser's defaults, so a patch that reset them
        # to those defaults would fail this case.
        body = style_body("partial patch flags", public=False, nsfw=True)
        with created_style(client, request_headers, body) as style_id:
            patch_response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json=self.patch_body,
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
            assert details["public"] is False
            assert details["nsfw"] is True

    def test_a_patch_without_params_or_models_keeps_them(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("partial patch params")) as style_id:
            patch_response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json=self.patch_body,
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
            assert details["params"]["temperature"] == 0.7
            assert details["models"] == TEXT_MODELS

    def test_a_patch_can_assign_a_shared_key(self, client, request_headers: dict[str, str]) -> None:
        key_response = client.put(
            "/api/v2/sharedkeys",
            json={"kudos": 100, "name": "text style shared key"},
            headers=request_headers,
        )
        assert key_response.status_code == 200, key_response.get_data(as_text=True)
        shared_key_id = key_response.get_json()["id"]

        try:
            with created_style(client, request_headers, style_body("patch shared key")) as style_id:
                patch_response = client.patch(
                    f"/api/v2/styles/text/{style_id}",
                    json={"sharedkey": shared_key_id},
                    headers=request_headers,
                )
                assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

                details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
                assert details["shared_key"] is not None
                assert details["shared_key"]["id"] == shared_key_id
        finally:
            client.delete(f"/api/v2/sharedkeys/{shared_key_id}", headers=request_headers)
