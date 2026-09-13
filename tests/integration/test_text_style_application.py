# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a text style changes the request it is applied to.

A style brings its own prompt template, params and model list, and a request that uses one generates
under all three. The cases here pin that down from the outside: what a worker is handed after a
style has been applied, how many takes the request still gets to choose, and what the quote comes to.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from flask.testing import FlaskClient

TEXT_MODELS = ["elinas/chronos-70b-v2"]

STYLE_PROMPT = "### Instruction:\n{p}\n\n### Response:\n"
STYLE_PARAMS: dict[str, Any] = {"max_length": 80, "max_context_length": 1024, "temperature": 0.7}

STYLE_KUDOS_SURCHARGE = 2
"""What a request pays on top of its params for generating under someone else's style."""

WORKER_NAME = "CICD Style Scribe"


@pytest.fixture(autouse=True, scope="module")
def _no_rate_limit() -> Iterator[None]:
    """Turn the per-path rate limit off for this module.

    Creating a style is capped at two calls a second and the generate endpoints at ten, which these
    cases would run through.

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
        "info": "A style used by the text style application tests.",
        "prompt": STYLE_PROMPT,
        "params": dict(STYLE_PARAMS),
        "models": TEXT_MODELS,
        "public": True,
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


@contextmanager
def queued_request(
    client: FlaskClient,
    request_headers: dict[str, str],
    **body: Any,
) -> Iterator[str]:
    """Queue a text request for the duration of the block and cancel it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt and the model list.

    Yields:
        The id of the queued request.
    """
    request_body: dict[str, Any] = {
        "prompt": "a horde of cute stable robots repairing a mainframe",
        "models": TEXT_MODELS,
        "trusted_workers": True,
        "validated_backends": False,
    }
    request_body.update(body)
    response = client.post("/api/v2/generate/text/async", json=request_body, headers=request_headers)
    assert response.status_code == 202, response.get_data(as_text=True)
    request_id = response.get_json()["id"]
    try:
        yield request_id
    finally:
        client.delete(f"/api/v2/generate/text/status/{request_id}", headers=request_headers)


def pop_payload(client: FlaskClient, request_headers: dict[str, str]) -> dict[str, Any]:
    """Take one job off the text queue as a worker and return what the worker was handed.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the worker owner's API key.

    Returns:
        The payload of the popped job.
    """
    response = client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": WORKER_NAME,
            "models": TEXT_MODELS,
            "bridge_agent": request_headers["Client-Agent"],
            "amount": 1,
            "max_context_length": 4096,
            "max_length": 512,
        },
        headers=request_headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    popped = response.get_json()
    assert popped["id"] is not None, popped
    return popped["payload"]


def dry_run_kudos(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> float:
    """Ask for a quote on a text request and return it.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt, the model list and the dry run flag.

    Returns:
        The quoted kudos.
    """
    request_body: dict[str, Any] = {
        "prompt": "a horde of cute stable robots repairing a mainframe",
        "models": TEXT_MODELS,
        "dry_run": True,
    }
    request_body.update(body)
    response = client.post("/api/v2/generate/text/async", json=request_body, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["kudos"]


class TestTextStyleParams:
    """Which params reach the worker when a style is applied."""

    def test_the_styles_params_replace_the_requests(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text apply params")) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "max_context_length": 4096, "temperature": 1.4},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == STYLE_PARAMS["max_length"]
                assert payload["max_context_length"] == STYLE_PARAMS["max_context_length"]
                assert payload["temperature"] == STYLE_PARAMS["temperature"]

    def test_the_prompt_is_formatted_into_the_template(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text apply prompt")) as style_id:
            with queued_request(client, request_headers, prompt="describe a lighthouse", style=style_id):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "### Instruction:\ndescribe a lighthouse\n\n### Response:\n"

    def test_the_number_of_takes_comes_from_the_request(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text apply takes")) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"n": 2}) as request_id:
                status = client.get(f"/api/v2/generate/text/status/{request_id}", headers=request_headers)

                assert status.status_code == 200, status.get_data(as_text=True)
                assert status.get_json()["waiting"] == 2


class TestTextStyleQuote:
    """What a quote for a styled request comes to."""

    def test_a_dry_run_returns_a_quote(self, client, request_headers: dict[str, str]) -> None:
        quote = dry_run_kudos(client, request_headers, params={"max_length": 80, "max_context_length": 1024})

        assert quote > 0

    def test_another_users_style_adds_a_surcharge(
        self,
        client,
        request_headers: dict[str, str],
        make_api_user,
    ) -> None:
        # The baseline sends exactly the params the style carries, so the only difference between the
        # two quotes is the style itself. The styled request sends a temperature no other case uses:
        # quotes are cached against the params they were computed from, and temperature changes that
        # key without changing the price, which comes from max_length, max_context_length and the model.
        unstyled_quote = dry_run_kudos(client, request_headers, params=dict(STYLE_PARAMS))

        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text apply surcharge")) as style_id:
            styled_quote = dry_run_kudos(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "temperature": 1.3},
            )

            assert styled_quote == unstyled_quote + STYLE_KUDOS_SURCHARGE
