# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How an image style changes the request it is applied to.

An image style brings its own prompt template, params and model list. The template is filled in with
the request's prompt at ``{p}`` and its negative prompt at ``{np}``, with the two halves of the
request's prompt separated by ``###``. Everything else in the template is left as written, so a
wildcard or a workflow string in braces reaches the worker unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from tests.fixture_types import MakeApiUser

TEST_MODELS = ["Fustercluck", "AlbedoBase XL (SDXL)"]

DEFAULT_IMAGE_SIZE = 512
"""The size a request runs at when neither it nor its style sets one."""

STYLE_KUDOS_SURCHARGE = 2
"""What a request pays on top of its params for generating under a style."""

TEMPLATE_FIELDS = [
    {"name": "caption", "description": "What the picture shows", "required": True},
    {"name": "tags", "description": "Comma-separated tags", "required": False},
]

STYLE_PARAMS: dict[str, Any] = {
    "width": 512,
    "height": 512,
    "steps": 8,
    "cfg_scale": 7,
    "sampler_name": "k_euler_a",
}

WORKER_NAME = "CICD Style Dreamer"

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


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
    """Build the body that creates an image style.

    Args:
        name: The style name, which is unique per user.
        **overrides: Keys to add to or replace in the body.

    Returns:
        The creation body.
    """
    body: dict[str, Any] = {
        "name": name,
        "info": "A style used by the image style application tests.",
        "prompt": "{p}, impasto impressionism###no blur, {np}",
        "params": dict(STYLE_PARAMS),
        "models": TEST_MODELS,
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
    """Create an image style for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the style belongs to.
        body: The creation body.

    Yields:
        The id of the new style.
    """
    response = client.post("/api/v2/styles/image", json=body, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    style_id = response.get_json()["id"]
    try:
        yield style_id
    finally:
        client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)


def post_request(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> TestResponse:
    """Send an image request and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt and the model list.

    Returns:
        The response, so a case can assert on a rejection as well as on a queued request.
    """
    request_body: dict[str, Any] = {
        "prompt": "a horde of cute stable robots repairing a mainframe",
        "models": ["stable_diffusion"],
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
    }
    request_body.update(body)
    return client.post("/api/v2/generate/async", json=request_body, headers=request_headers)


def post_dry_run(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> TestResponse:
    """Ask for a quote on an image request and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt, the model list and the dry run flag.

    Returns:
        The response, so a case can assert on the resolved request as well as on the quote.
    """
    return post_request(client, request_headers, dry_run=True, **body)


@contextmanager
def queued_request(
    client: FlaskClient,
    request_headers: dict[str, str],
    **body: Any,
) -> Iterator[str]:
    """Queue an image request for the duration of the block and cancel it at the end.

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
        client.delete(f"/api/v2/generate/status/{request_id}", headers=request_headers)


def pop_payload(client: FlaskClient, request_headers: dict[str, str]) -> dict[str, Any]:
    """Take one job off the image queue as a worker and return what the worker was handed.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the worker owner's API key.

    Returns:
        The payload of the popped job.
    """
    response = client.post(
        "/api/v2/generate/pop",
        json={
            "name": WORKER_NAME,
            "models": TEST_MODELS,
            "bridge_agent": "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
            "nsfw": True,
            "amount": 1,
            "max_pixels": 4194304,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_sdxl_controlnet": True,
            "allow_lora": True,
        },
        headers=request_headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    popped = response.get_json()
    assert popped["id"] is not None, popped
    return popped["payload"]


class TestImageStyleParams:
    """Which params reach the worker when a style is applied."""

    def test_the_styles_params_replace_the_requests(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("image apply params")) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"width": 1024, "height": 1024, "steps": 30, "cfg_scale": 1.5, "sampler_name": "k_euler"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["width"] == STYLE_PARAMS["width"]
                assert payload["height"] == STYLE_PARAMS["height"]
                assert payload["ddim_steps"] == STYLE_PARAMS["steps"]
                assert payload["sampler_name"] == STYLE_PARAMS["sampler_name"]

    def test_the_number_of_takes_comes_from_the_request(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("image apply takes")) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"n": 2}) as request_id:
                check = client.get(f"/api/v2/generate/check/{request_id}", headers=request_headers)

                assert check.status_code == 200, check.get_data(as_text=True)
                assert check.get_json()["waiting"] == 2

    def test_width_and_height_come_from_the_request_when_the_style_sets_neither(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "image apply sizeless",
            params={"steps": 8, "cfg_scale": 7, "sampler_name": "k_euler_a"},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"width": 512, "height": 768, "steps": 30, "sampler_name": "k_euler"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["width"] == 512
                assert payload["height"] == 768
                assert payload["ddim_steps"] == 8
                assert payload["sampler_name"] == "k_euler_a"


class TestImageStylePrompt:
    """How the request's prompt is formatted into the style's template."""

    def test_the_prompt_and_the_negative_prompt_fill_their_placeholders(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        with created_style(client, request_headers, style_body("image apply prompt")) as style_id:
            with queued_request(client, request_headers, prompt="robots###organic", style=style_id):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "robots, impasto impressionism###no blur, organic"

    def test_a_template_without_a_split_gets_one_in_front_of_the_negative_prompt(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # The template has a {np} but no ###, so the negative prompt would otherwise run on from the
        # positive one and be generated from.
        body = style_body("image apply split", prompt="{p}, impasto impressionism {np}")
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, prompt="robots###organic", style=style_id):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "robots, impasto impressionism ###organic"

    def test_braces_in_the_template_reach_the_worker_as_written(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # Everything in braces other than {p} and {np} belongs to the worker, not to the horde: a
        # wildcard or a workflow string has to arrive with its braces intact.
        body = style_body("image apply braces", prompt="{p}, {red|green} rococo###{np}")
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, prompt="robots###blurry", style=style_id):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "robots, {red|green} rococo###blurry"


class TestImageStyleParameterPolicy:
    """Which of a request's params an image style's parameter policy lets through."""

    def test_override_none_ignores_the_request_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("image policy none", parameter_policy={"override": "none"})
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"steps": 30}):
                payload = pop_payload(client, request_headers)

                assert payload["ddim_steps"] == STYLE_PARAMS["steps"]

    def test_override_all_takes_the_request_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("image policy all", parameter_policy={"override": "all"})
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"steps": 30, "width": 768, "height": 768},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["ddim_steps"] == 30
                assert payload["width"] == 768
                assert payload["height"] == 768

    def test_override_listed_takes_only_the_listed_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "image policy listed",
            parameter_policy={"override": "listed", "overridable": ["steps"]},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"steps": 30}):
                payload = pop_payload(client, request_headers)

                assert payload["ddim_steps"] == 30
                assert payload["width"] == STYLE_PARAMS["width"]

    def test_a_param_the_style_does_not_list_is_ignored(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "image policy listed unlisted",
            parameter_policy={"override": "listed", "overridable": ["steps"]},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"steps": 30, "width": 768}):
                payload = pop_payload(client, request_headers)

                assert payload["ddim_steps"] == 30
                assert payload["width"] == STYLE_PARAMS["width"]

    def test_a_param_above_the_ceiling_is_refused_rather_than_trimmed(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "image policy ceiling",
            parameter_policy={"override": "all", "ceilings": {"steps": 20}},
        )
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"steps": 30})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterAboveCeiling"

    def test_a_param_at_the_ceiling_is_accepted(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "image policy ceiling boundary",
            parameter_policy={"override": "all", "ceilings": {"steps": 20}},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"steps": 20}):
                payload = pop_payload(client, request_headers)

                assert payload["ddim_steps"] == 20

    def test_the_request_count_is_capped_even_under_none(self, client, request_headers: dict[str, str]) -> None:
        # The number of takes always comes from the request, so a ceiling on it applies under a mode
        # that lets no other param through.
        body = style_body("image policy takes ceiling", parameter_policy={"override": "none", "ceilings": {"n": 1}})
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"n": 3})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterAboveCeiling"

    def test_a_style_with_a_policy_does_not_pass_the_size_through(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # Without a policy a style that sets no size takes the request's. With one, the policy decides,
        # so a mode that lets nothing through leaves the request on the horde's default size.
        body = style_body(
            "image policy sizeless",
            params={"steps": 8, "cfg_scale": 7, "sampler_name": "k_euler_a"},
            parameter_policy={"override": "none"},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"width": 512, "height": 768}):
                payload = pop_payload(client, request_headers)

                assert payload["width"] == DEFAULT_IMAGE_SIZE
                assert payload["height"] == DEFAULT_IMAGE_SIZE

    def test_a_policy_that_lets_the_size_through_uses_the_requests(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "image policy sizeless listed",
            params={"steps": 8, "cfg_scale": 7, "sampler_name": "k_euler_a"},
            parameter_policy={"override": "listed", "overridable": ["width", "height"]},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"width": 512, "height": 768}):
                payload = pop_payload(client, request_headers)

                assert payload["width"] == 512
                assert payload["height"] == 768


def client_default_params() -> dict[str, Any]:
    """Build the params body a generated client sends when the user sets nothing.

    Read from the image payload model at call time, so the body tracks the defaults the API documents.

    Returns:
        Every param of the image payload model that documents a default, set to that default.
    """
    from horde.apis.v2.stable import models

    return {
        parameter_name: parameter_field.default
        for parameter_name, parameter_field in models.input_model_generation_payload.resolved.items()
        if parameter_field.default is not None
    }


def payload_key(parameter_name: str) -> str:
    """Return the key a worker's payload carries a request param under.

    Args:
        parameter_name: The param as the request names it.

    Returns:
        The payload key, which differs only for ``steps``.
    """
    if parameter_name == "steps":
        return "ddim_steps"
    return parameter_name


class TestImageStyleGeneratedClientDefaults:
    """A request from a client that sends every param with a default, under the policy modes that read it."""

    style_params: dict[str, Any] = {
        "width": 768,
        "height": 640,
        "steps": 8,
        "cfg_scale": 5.0,
        "sampler_name": "k_euler",
        "karras": True,
    }
    """Differs from the documented default on each param it sets, so a payload shows which side won."""

    def test_the_style_differs_from_the_defaults_it_sets(self) -> None:
        client_params = client_default_params()

        for parameter_name, style_value in self.style_params.items():
            assert parameter_name in client_params, parameter_name
            assert client_params[parameter_name] != style_value, parameter_name

    def test_listed_takes_the_listed_param_and_the_style_keeps_the_rest(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "image client defaults listed",
            params=dict(self.style_params),
            parameter_policy={"override": "listed", "overridable": ["steps"]},
        )
        client_params = client_default_params()
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params=client_params):
                payload = pop_payload(client, request_headers)

                assert payload["ddim_steps"] == client_params["steps"]
                for parameter_name, style_value in self.style_params.items():
                    if parameter_name != "steps":
                        assert payload[payload_key(parameter_name)] == style_value, parameter_name

    def test_all_takes_every_param_the_client_sent(self, client, request_headers: dict[str, str]) -> None:
        # A client that fills in every default overrides each of those params under this mode.
        body = style_body(
            "image client defaults all",
            params=dict(self.style_params),
            parameter_policy={"override": "all"},
        )
        client_params = client_default_params()
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params=client_params):
                payload = pop_payload(client, request_headers)

                for parameter_name, client_value in client_params.items():
                    if parameter_name != "n":
                        assert payload[payload_key(parameter_name)] == client_value, parameter_name

    def test_none_keeps_every_param_of_the_style(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "image client defaults none",
            params=dict(self.style_params),
            parameter_policy={"override": "none"},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params=client_default_params()):
                payload = pop_payload(client, request_headers)

                for parameter_name, style_value in self.style_params.items():
                    assert payload[payload_key(parameter_name)] == style_value, parameter_name


class TestImageStyleTemplateFields:
    """Filling the placeholders an image style declares."""

    prompt_with_fields = "{p}, {caption} in {tags} style###{np}"

    def test_the_fields_are_formatted_into_the_prompt(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("image fields", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="robots###blurry",
                style=style_id,
                template_fields={"caption": "a lighthouse", "tags": "impasto"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "robots, a lighthouse in impasto style###blurry"

    def test_an_optional_field_left_out_formats_to_nothing(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "image fields optional",
            prompt=self.prompt_with_fields,
            template_fields=TEMPLATE_FIELDS,
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="robots###blurry",
                style=style_id,
                template_fields={"caption": "a lighthouse"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "robots, a lighthouse in  style###blurry"

    def test_braces_the_style_does_not_declare_reach_the_worker_as_written(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "image fields braces",
            prompt="{p}, {caption} {red|green} rococo###{np}",
            template_fields=TEMPLATE_FIELDS,
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="robots###blurry",
                style=style_id,
                template_fields={"caption": "a lighthouse"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "robots, a lighthouse {red|green} rococo###blurry"

    def test_fields_without_a_style_are_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(client, request_headers, template_fields={"caption": "a lighthouse"})

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "TemplateFieldsRequireStyle"

    def test_a_field_the_style_does_not_declare_is_refused(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("image fields unknown", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
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
        body = style_body("image fields missing", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, template_fields={"tags": "impasto"})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "TemplateFieldMissing"


class TestImageDryRunResolvedRequest:
    """What a dry run returns alongside the quote."""

    def test_a_request_without_a_style_resolves_to_itself(self, client, request_headers: dict[str, str]) -> None:
        response = post_dry_run(
            client,
            request_headers,
            prompt="robots###blurry",
            params={"width": 512, "height": 512, "steps": 8},
        )

        assert response.status_code == 200, response.get_data(as_text=True)
        resolved = response.get_json()["resolved"]
        assert resolved["prompt"] == "robots"
        assert resolved["negative_prompt"] == "blurry"
        assert resolved["style"] is None
        assert resolved["models"] == ["stable_diffusion"]
        assert resolved["params"]["width"] == 512

    def test_a_prompt_without_a_separator_has_no_negative_half(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        response = post_dry_run(client, request_headers, prompt="robots", params={"steps": 8})

        assert response.status_code == 200, response.get_data(as_text=True)
        resolved = response.get_json()["resolved"]
        assert resolved["prompt"] == "robots"
        assert resolved["negative_prompt"] is None

    def test_a_styled_request_reports_the_style_it_ran_under(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("image resolved style")) as style_id:
            response = post_dry_run(
                client,
                request_headers,
                prompt="robots###organic",
                style=style_id,
                params={"steps": 30},
            )

            assert response.status_code == 200, response.get_data(as_text=True)
            resolved = response.get_json()["resolved"]
            assert resolved["style"]["id"] == style_id
            assert resolved["style"]["name"] == "image resolved style"
            assert resolved["prompt"] == "robots, impasto impressionism"
            assert resolved["negative_prompt"] == "no blur, organic"
            assert resolved["params"]["steps"] == STYLE_PARAMS["steps"]

    def test_a_queued_request_carries_no_resolved_request(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(client, request_headers)

        assert response.status_code == 202, response.get_data(as_text=True)
        queued = response.get_json()
        assert "resolved" not in queued
        client.delete(f"/api/v2/generate/status/{queued['id']}", headers=request_headers)

    def test_a_style_that_declares_neither_is_quoted_as_it_was_before(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # The baseline sends exactly the style's params and models, and a cfg_scale no other case
        # uses, so the two are quoted under different cache keys. The style's params are used as they
        # are apart from n, and the style belongs to the requester, so the two come to the same price.
        unstyled = post_dry_run(
            client,
            request_headers,
            models=TEST_MODELS,
            params={**STYLE_PARAMS, "cfg_scale": 7.5},
        )
        assert unstyled.status_code == 200, unstyled.get_data(as_text=True)

        with created_style(client, request_headers, style_body("image compatibility")) as style_id:
            styled = post_dry_run(
                client,
                request_headers,
                style=style_id,
                params={"width": 1024, "height": 1024, "steps": 30},
            )
            assert styled.status_code == 200, styled.get_data(as_text=True)
            quote = styled.get_json()

            assert quote["resolved"]["params"] == {**STYLE_PARAMS, "n": 1}
            assert quote["kudos"] == unstyled.get_json()["kudos"]


class TestImageStyleTypeMismatch:
    """An image request refuses a style of the other type."""

    def test_a_text_style_is_refused(self, client, request_headers: dict[str, str]) -> None:
        text_style = {
            "name": "image request text style",
            "info": "A text style used by the image style application tests.",
            "prompt": "Answer plainly: {p}",
            "params": {"max_length": 80, "max_context_length": 1024},
            "models": ["elinas/chronos-70b-v2"],
            "public": True,
            "nsfw": False,
        }
        created = client.post("/api/v2/styles/text", json=text_style, headers=request_headers)
        assert created.status_code == 200, created.get_data(as_text=True)
        style_id = created.get_json()["id"]
        try:
            response = post_dry_run(client, request_headers, style=style_id)

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleMismatch"
        finally:
            client.delete(f"/api/v2/styles/text/{style_id}", headers=request_headers)


class TestImageStyleQuote:
    """What a quote for a styled request comes to."""

    def test_another_users_style_adds_a_surcharge(
        self,
        client,
        request_headers: dict[str, str],
        make_api_user,
    ) -> None:
        # The baseline sends exactly the style's params and models, with a cfg_scale no other case
        # uses so the two are quoted under different cache keys. cfg_scale does not change the price,
        # which comes from the size, the steps and the model, so the only difference left is the
        # surcharge.
        unstyled = post_dry_run(
            client,
            request_headers,
            models=TEST_MODELS,
            params={**STYLE_PARAMS, "cfg_scale": 6.5},
        )
        assert unstyled.status_code == 200, unstyled.get_data(as_text=True)

        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("image apply surcharge")) as style_id:
            styled = post_dry_run(client, request_headers, style=style_id)
            assert styled.status_code == 200, styled.get_data(as_text=True)

            assert styled.get_json()["kudos"] == unstyled.get_json()["kudos"] + STYLE_KUDOS_SURCHARGE

    def test_your_own_style_adds_no_surcharge(self, client, request_headers: dict[str, str]) -> None:
        # The surcharge pays the style's author, so a request generating under its own author's style
        # has nobody to pay and is quoted at what its params come to.
        unstyled = post_dry_run(
            client,
            request_headers,
            models=TEST_MODELS,
            params={**STYLE_PARAMS, "cfg_scale": 5.5},
        )
        assert unstyled.status_code == 200, unstyled.get_data(as_text=True)

        with created_style(client, request_headers, style_body("image apply own style")) as style_id:
            styled = post_dry_run(client, request_headers, style=style_id)
            assert styled.status_code == 200, styled.get_data(as_text=True)

            assert styled.get_json()["kudos"] == unstyled.get_json()["kudos"]


def settled_kudos(app: Flask, settle_kudos: Callable[[], int], user_id: int) -> float:
    """Fold every pending kudos posting and return the user's balance.

    Args:
        app: The Flask app, for the database session.
        settle_kudos: The fixture helper that folds pending ledger postings.
        user_id: The user whose balance to read.

    Returns:
        The user's balance once everything pending has been applied.
    """
    from horde.database import functions as database

    settle_kudos()
    with app.app_context():
        return database.find_user_by_id(user_id).kudos


class TestImageStyleAuthorCredit:
    """When the author of a style is paid for a request that runs under it."""

    def test_a_queued_request_pays_the_author_the_surcharge(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("image credit queued")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, request_headers, style=style_id):
                assert settled_kudos(app, settle_kudos, owner.id) == balance_before + STYLE_KUDOS_SURCHARGE

    def test_a_dry_run_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A quote includes the surcharge, but asking for one queues nothing and so pays nobody."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("image credit dry run")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            for _ in range(3):
                quote = post_dry_run(client, request_headers, style=style_id)
                assert quote.status_code == 200, quote.get_data(as_text=True)

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before

    def test_a_refused_request_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A request refused after the style resolved, here for a mask with no source image, pays no one."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("image credit refused")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            refused = post_request(client, request_headers, style=style_id, source_mask="aGVsbG8=")
            assert refused.status_code == 400, refused.get_data(as_text=True)
            assert refused.get_json()["rc"] == "SourceMaskUnnecessary"

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before


@contextmanager
def created_collection(client: FlaskClient, request_headers: dict[str, str], *, name: str, style_ids: list[str]) -> Iterator[str]:
    """Create a style collection for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the collection belongs to.
        name: The collection name.
        style_ids: The styles the collection holds.

    Yields:
        The id of the new collection.
    """
    response = client.post("/api/v2/collections", json={"name": name, "styles": style_ids}, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    collection_id = response.get_json()["id"]
    try:
        yield collection_id
    finally:
        client.delete(f"/api/v2/collections/{collection_id}", headers=request_headers)


class TestImageStyleCollection:
    """An image request can run under a collection, which draws one of its styles."""

    def test_a_dry_run_runs_under_one_of_the_collections_styles(self, client, request_headers: dict[str, str]) -> None:
        with (
            created_style(client, request_headers, style_body("image collection first")) as first_style_id,
            created_style(client, request_headers, style_body("image collection second")) as second_style_id,
            created_collection(
                client,
                request_headers,
                name="image collection dry run",
                style_ids=[first_style_id, second_style_id],
            ) as collection_id,
        ):
            response = post_dry_run(client, request_headers, style=collection_id)

            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["resolved"]["style"]["id"] in {first_style_id, second_style_id}

    def test_a_queued_request_counts_a_use_of_the_collection(self, client, request_headers: dict[str, str]) -> None:
        with (
            created_style(client, request_headers, style_body("image collection queued")) as style_id,
            created_collection(client, request_headers, name="image collection queued", style_ids=[style_id]) as collection_id,
        ):
            with queued_request(client, request_headers, style=collection_id):
                pass

            details = client.get(f"/api/v2/collections/{collection_id}", headers=request_headers).get_json()
            assert details["use_count"] == 1


def waiting_prompt_style_id(app: Flask, request_id: str) -> str | None:
    """Return the style a queued request's waiting prompt records.

    Args:
        app: The Flask app, for the database session.
        request_id: The queued request's id.

    Returns:
        The recorded style id as a string, or None when the request runs under no style.
    """
    from horde.classes.base.waiting_prompt import WaitingPrompt
    from horde.flask import db

    with app.app_context():
        style_id = db.session.query(WaitingPrompt.style_id).filter(WaitingPrompt.id == request_id).scalar()
        return str(style_id) if style_id is not None else None


class TestImageStyleAttribution:
    """A queued request records the style it runs under, for the generation statistics."""

    def test_a_request_under_a_style_records_it(self, app: Flask, client: FlaskClient, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("image attributed")) as style_id:
            with queued_request(client, request_headers, style=style_id) as request_id:
                assert waiting_prompt_style_id(app, request_id) == style_id

    def test_a_request_under_a_collection_records_the_drawn_style(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        with (
            created_style(client, request_headers, style_body("image attributed member")) as style_id,
            created_collection(client, request_headers, name="image attributed collection", style_ids=[style_id]) as collection_id,
        ):
            with queued_request(client, request_headers, style=collection_id) as request_id:
                assert waiting_prompt_style_id(app, request_id) == style_id

    def test_an_unstyled_request_records_no_style(self, app: Flask, client: FlaskClient, request_headers: dict[str, str]) -> None:
        with queued_request(client, request_headers) as request_id:
            assert waiting_prompt_style_id(app, request_id) is None
