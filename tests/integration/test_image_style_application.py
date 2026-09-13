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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from flask.testing import FlaskClient

TEST_MODELS = ["Fustercluck", "AlbedoBase XL (SDXL)"]

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
    response = client.post("/api/v2/generate/async", json=request_body, headers=request_headers)
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
