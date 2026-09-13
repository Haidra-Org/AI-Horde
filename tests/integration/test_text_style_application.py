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
from werkzeug.test import TestResponse

TEXT_MODELS = ["elinas/chronos-70b-v2"]

STYLE_PROMPT = "### Instruction:\n{p}\n\n### Response:\n"
STYLE_PARAMS: dict[str, Any] = {"max_length": 80, "max_context_length": 1024, "temperature": 0.7}

STYLE_KUDOS_SURCHARGE = 2
"""What a request pays on top of its params for generating under someone else's style."""

TEMPLATE_FIELDS = [
    {"name": "caption", "description": "What the picture shows", "required": True},
    {"name": "tags", "description": "Comma-separated tags", "required": False},
]

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


def post_request(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> TestResponse:
    """Send a text request and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt and the model list.

    Returns:
        The response, so a case can assert on a rejection as well as on a queued request.
    """
    request_body: dict[str, Any] = {
        "prompt": "a horde of cute stable robots repairing a mainframe",
        "models": TEXT_MODELS,
        "trusted_workers": True,
        "validated_backends": False,
    }
    request_body.update(body)
    return client.post("/api/v2/generate/text/async", json=request_body, headers=request_headers)


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
    response = post_request(client, request_headers, **body)
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


class TestTextStyleParameterPolicy:
    """Which of a request's params a style's parameter policy lets through."""

    def test_override_none_ignores_the_request_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text policy none", parameter_policy={"override": "none"})
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"max_length": 480}):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == STYLE_PARAMS["max_length"]

    def test_override_all_takes_the_request_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text policy all", parameter_policy={"override": "all"})
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "temperature": 1.4},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 480
                assert payload["temperature"] == 1.4

    def test_override_listed_takes_only_the_listed_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text policy listed",
            parameter_policy={"override": "listed", "overridable": ["max_length"]},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"max_length": 480}):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 480
                assert payload["max_context_length"] == STYLE_PARAMS["max_context_length"]

    def test_a_param_the_style_does_not_list_is_refused(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text policy listed refusal",
            parameter_policy={"override": "listed", "overridable": ["max_length"]},
        )
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"temperature": 1.4})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterNotOverridable"

    def test_a_param_above_the_ceiling_is_refused_rather_than_trimmed(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "text policy ceiling",
            parameter_policy={"override": "all", "ceilings": {"max_length": 256}},
        )
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"max_length": 512})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterAboveCeiling"

    def test_a_param_at_the_ceiling_is_accepted(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text policy ceiling boundary",
            parameter_policy={"override": "all", "ceilings": {"max_length": 256}},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"max_length": 256}):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 256

    def test_the_request_count_is_capped_even_under_none(self, client, request_headers: dict[str, str]) -> None:
        # The number of takes always comes from the request, so a ceiling on it applies under a mode
        # that lets no other param through.
        body = style_body("text policy takes ceiling", parameter_policy={"override": "none", "ceilings": {"n": 1}})
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"n": 3})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterAboveCeiling"


class TestTextStyleTemplateFields:
    """Filling the placeholders a text style declares."""

    prompt_with_fields = "### Instruction:\nDescribe {caption} ({tags}).\n{p}\n\n### Response:\n"

    def test_the_fields_are_formatted_into_the_prompt(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text fields",
            prompt=self.prompt_with_fields,
            template_fields=TEMPLATE_FIELDS,
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="keep it short",
                style=style_id,
                template_fields={"caption": "a lighthouse", "tags": "storm, night"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == ("### Instruction:\nDescribe a lighthouse (storm, night).\nkeep it short\n\n### Response:\n")

    def test_an_optional_field_left_out_formats_to_nothing(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text fields optional",
            prompt=self.prompt_with_fields,
            template_fields=TEMPLATE_FIELDS,
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="keep it short",
                style=style_id,
                template_fields={"caption": "a lighthouse"},
            ):
                payload = pop_payload(client, request_headers)

                assert "Describe a lighthouse ()." in payload["prompt"]

    def test_fields_without_a_style_are_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(client, request_headers, template_fields={"caption": "a lighthouse"})

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "TemplateFieldsRequireStyle"

    def test_a_field_the_style_does_not_declare_is_refused(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text fields unknown", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = post_request(
                client,
                request_headers,
                style=style_id,
                template_fields={"caption": "a lighthouse", "mood": "bleak"},
            )

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "TemplateFieldUnknown"

    def test_a_required_field_left_out_is_refused(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text fields missing", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, template_fields={"tags": "storm"})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "TemplateFieldMissing"
