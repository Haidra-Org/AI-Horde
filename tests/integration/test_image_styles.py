# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later


import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from tests.fixture_types import MakeApiUser

TEST_MODELS = ["Fustercluck", "AlbedoBase XL (SDXL)"]

STYLE_PROMPT = "{p}, impasto impressionism###no blur, {np}"

POLICY = {
    "override": "listed",
    "overridable": ["width", "height"],
    "ceilings": {"width": 1024, "height": 1024},
}

TEMPLATE_FIELDS = [
    {"name": "caption", "description": "What the picture shows", "required": True},
    {"name": "tags", "description": "Comma-separated tags", "required": False},
]

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


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
    """Build the body that creates an image style.

    Args:
        name: The style name, which is unique per user.
        **overrides: Keys to add to or replace in the body.

    Returns:
        The creation body.
    """
    body: dict[str, Any] = {
        "name": name,
        "info": "A style used by the image style endpoint tests.",
        "prompt": STYLE_PROMPT,
        "params": {"width": 512, "height": 512, "steps": 8, "cfg_scale": 7, "sampler_name": "k_euler"},
        "models": TEST_MODELS,
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


def post_style(client: FlaskClient, request_headers: dict[str, str], body: dict[str, Any]) -> TestResponse:
    """Attempt to create an image style and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the style would belong to.
        body: The creation body.

    Returns:
        The response, for a case that expects a rejection.
    """
    return client.post("/api/v2/styles/image", json=body, headers=request_headers)


def test_styled_image_gen(client, request_headers: dict[str, str]) -> None:
    print("test_styled_image_gen")
    style_dict = {
        "name": "impasto test",
        "info": "impasto test",
        "public": True,
        "prompt": "{p}, impasto impressionism###no blur, {np}",
        "nsfw": False,
        "params": {
            "width": 1024,
            "height": 512,
            "steps": 8,
            "cfg_scale": 7,
            "sampler_name": "k_euler_a",
        },
        "models": TEST_MODELS,
        "loras": [{"name": "247778", "is_version": True}],
    }

    style_req = client.post("/api/v2/styles/image", json=style_dict, headers=request_headers)
    assert style_req.status_code < 400, style_req.get_data(as_text=True)
    style_results = style_req.get_json()
    style_id = style_results["id"]

    try:
        async_dict = {
            "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe###organic",
            "nsfw": True,
            "censor_nsfw": False,
            "r2": True,
            "shared": True,
            "trusted_workers": True,
            "params": {
                "width": 1024,
                "height": 1024,
                "steps": 8,
                "cfg_scale": 1.5,
                "sampler_name": "k_euler",
            },
            "models": ["stable_diffusion"],
            "style": style_id,
        }

        async_req = client.post("/api/v2/generate/async", json=async_dict, headers=request_headers)
        assert async_req.status_code < 400, async_req.get_data(as_text=True)
        async_results = async_req.get_json()
        req_id = async_results["id"]

        pop_dict = {
            "name": "CICD Fake Dreamer",
            "models": TEST_MODELS,
            "bridge_agent": "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen",
            "nsfw": True,
            "amount": 10,
            "max_pixels": 4194304,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_sdxl_controlnet": True,
            "allow_lora": True,
        }
        pop_req = client.post("/api/v2/generate/pop", json=pop_dict, headers=request_headers)
        try:
            assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
        except AssertionError as err:
            client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
            print("Request cancelled")
            raise err

        pop_results = pop_req.get_json()

        job_id = pop_results["id"]
        try:
            assert job_id is not None, pop_results
            assert pop_results["payload"]["sampler_name"] == "k_euler_a"
            assert pop_results["payload"]["width"] == 1024
            assert pop_results["payload"]["height"] == 512
            assert pop_results["payload"]["prompt"] == (
                "a horde of cute stable robots in a sprawling server room repairing a massive mainframe, impasto impressionism"
                "###no blur, organic"
            )
        except AssertionError as err:
            client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
            print("Request cancelled")
            raise err

        submit_dict = {
            "id": job_id,
            "generation": "R2",
            "state": "ok",
            "seed": 0,
        }
        submit_req = client.post("/api/v2/generate/submit", json=submit_dict, headers=request_headers)
        assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
        submit_results = submit_req.get_json()
        assert submit_results["reward"] > 0

        retrieve_req = client.get(f"/api/v2/generate/status/{req_id}", headers=request_headers)
        assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
        retrieve_results = retrieve_req.get_json()

        assert len(retrieve_results["generations"]) == 1
        gen = retrieve_results["generations"][0]
        assert len(gen["gen_metadata"]) == 0
        assert gen["seed"] == "0"
        assert gen["worker_name"] == "CICD Fake Dreamer"
        assert gen["model"] in TEST_MODELS
        assert gen["state"] == "ok"
        assert retrieve_results["kudos"] > 1
        assert retrieve_results["done"] is True

        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
    except AssertionError as err:
        client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)
        raise err

    client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)


def test_image_style_patch_only_changes_the_fields_it_carries(client, request_headers: dict[str, str]) -> None:
    """A patch that carries one field leaves every other field of the style as it was."""
    style_dict = {
        "name": "partial patch image",
        "info": "A style used to check that a patch leaves the rest alone.",
        "public": False,
        "nsfw": True,
        "prompt": "{p}, impasto impressionism###no blur, {np}",
        "params": {"width": 512, "height": 512, "steps": 8, "cfg_scale": 7, "sampler_name": "k_euler"},
        "models": TEST_MODELS,
    }

    style_req = client.post("/api/v2/styles/image", json=style_dict, headers=request_headers)
    assert style_req.status_code < 400, style_req.get_data(as_text=True)
    style_id = style_req.get_json()["id"]

    try:
        patch_req = client.patch(
            f"/api/v2/styles/image/{style_id}",
            json={"info": "Only the description changes in this patch."},
            headers=request_headers,
        )
        assert patch_req.status_code < 400, patch_req.get_data(as_text=True)

        details = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers).get_json()
        assert details["info"] == "Only the description changes in this patch."
        assert details["name"] == "partial patch image"
        assert details["prompt"] == "{p}, impasto impressionism###no blur, {np}"
        assert details["public"] is False
        assert details["nsfw"] is True
        assert details["params"] == style_dict["params"]
        assert sorted(details["models"]) == sorted(TEST_MODELS)
    finally:
        client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)


class TestImageStyleContract:
    """An image style declaring a parameter policy or template fields."""

    def test_the_declarations_are_stored_and_served(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract create image", parameter_policy=POLICY, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            details = response.get_json()
            assert details["parameter_policy"] == POLICY
            assert details["template_fields"] == TEMPLATE_FIELDS
            assert details["updated"] is not None

    def test_a_style_declaring_neither_serves_both_keys_as_null(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        with created_style(client, request_headers, style_body("contract absent image")) as style_id:
            details = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers).get_json()

            assert details["parameter_policy"] is None
            assert details["template_fields"] is None
            assert details["prompt"] == STYLE_PROMPT

    def test_a_ceiling_on_steps_is_accepted(self, client, request_headers: dict[str, str]) -> None:
        policy = {"override": "all", "ceilings": {"steps": 30}}
        body = style_body("contract steps ceiling", parameter_policy=policy)
        with created_style(client, request_headers, body) as style_id:
            details = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers).get_json()

            assert details["parameter_policy"]["ceilings"] == {"steps": 30}

    def test_a_patch_replaces_the_declarations(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract patch image", parameter_policy=POLICY, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            replacement_policy = {"override": "all", "ceilings": {"width": 768}}
            replacement_fields = [{"name": "caption", "description": "A one-line caption", "required": False}]
            patch_response = client.patch(
                f"/api/v2/styles/image/{style_id}",
                json={"parameter_policy": replacement_policy, "template_fields": replacement_fields},
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers).get_json()
            assert details["parameter_policy"] == replacement_policy
            assert details["template_fields"] == replacement_fields

    def test_a_patch_that_omits_them_leaves_them_alone(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract patch omitted image", parameter_policy=POLICY, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            patch_response = client.patch(
                f"/api/v2/styles/image/{style_id}",
                json={"info": "Only the description changes in this patch."},
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers).get_json()
            assert details["parameter_policy"] == POLICY
            assert details["template_fields"] == TEMPLATE_FIELDS


class TestImageStyleContractRejections:
    """Declarations the image style endpoints refuse."""

    def test_overridable_outside_listed_mode_is_misplaced(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract misplaced image", parameter_policy={"override": "all", "overridable": ["width"]})
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "StylePolicyOverridableMisplaced"

    def test_an_unknown_overridable_parameter_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract unknown param image",
            parameter_policy={"override": "listed", "overridable": ["max_length"]},
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_the_request_count_cannot_be_made_overridable(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract overridable n image", parameter_policy={"override": "listed", "overridable": ["n"]})
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    @pytest.mark.parametrize("ceiling", [63, 3073])
    def test_a_ceiling_outside_the_parameter_range_is_rejected(
        self,
        client,
        request_headers: dict[str, str],
        ceiling: int,
    ) -> None:
        body = style_body(
            f"contract ceiling image {ceiling}",
            parameter_policy={"override": "all", "ceilings": {"width": ceiling}},
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_ceiling_on_a_parameter_an_image_style_cannot_cap_is_rejected(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # max_length belongs to text requests, and cfg_scale is an image param no image style may cap.
        for parameter_name in ("max_length", "cfg_scale"):
            body = style_body(
                f"contract ceiling image {parameter_name}",
                parameter_policy={"override": "all", "ceilings": {parameter_name: 8}},
            )
            response = post_style(client, request_headers, body)

            assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_ceiling_on_a_parameter_the_mode_does_not_hand_over_is_rejected(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        for mode_name, policy in (
            ("none", {"override": "none", "ceilings": {"width": 1024}}),
            ("listed", {"override": "listed", "overridable": ["width"], "ceilings": {"steps": 30}}),
        ):
            body = style_body(f"contract inert ceiling image {mode_name}", parameter_policy=policy)
            response = post_style(client, request_headers, body)

            assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_reserved_template_field_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract reserved field image",
            template_fields=[{"name": "np", "description": "The negative prompt the horde already fills"}],
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_repeated_template_field_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract repeated field image",
            template_fields=[
                {"name": "caption", "description": "The first declaration"},
                {"name": "caption", "description": "The second declaration"},
            ],
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)


@contextmanager
def created_shared_key(client: FlaskClient, request_headers: dict[str, str], name: str) -> Iterator[str]:
    """Create a shared key for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the shared key belongs to.
        name: The shared key's name.

    Yields:
        The id of the new shared key.
    """
    response = client.put("/api/v2/sharedkeys", json={"kudos": 100, "name": name}, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    shared_key_id = response.get_json()["id"]
    try:
        yield shared_key_id
    finally:
        client.delete(f"/api/v2/sharedkeys/{shared_key_id}", headers=request_headers)


class TestImageStyleSharedKeyVisibility:
    """The shared key a image style generates under is served to its owner only."""

    def test_the_owner_gets_the_shared_key_by_id(self, client, request_headers: dict[str, str]) -> None:
        with (
            created_shared_key(client, request_headers, "image owner by id") as shared_key_id,
            created_style(client, request_headers, style_body("image owner by id", sharedkey=shared_key_id)) as style_id,
        ):
            response = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["shared_key"]["id"] == shared_key_id
            # The owner's copy must not be kept by any cache between the horde and the owner.
            assert response.headers["Cache-Control"] == "private, no-store"

    def test_the_owner_gets_the_shared_key_by_name(self, client, request_headers: dict[str, str]) -> None:
        style_name = f"image owner by name {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "image owner by name") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)),
        ):
            response = client.get(f"/api/v2/styles/image_by_name/{style_name}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["shared_key"]["id"] == shared_key_id
            assert response.headers["Cache-Control"] == "private, no-store"

    def test_another_user_gets_null(
        self,
        client,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
    ) -> None:
        other_user = make_api_user()
        other_user_headers = {**request_headers, "apikey": other_user.api_key}
        style_name = f"image other user {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "image other user") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)) as style_id,
        ):
            by_id = client.get(f"/api/v2/styles/image/{style_id}", headers=other_user_headers)
            by_name = client.get(f"/api/v2/styles/image_by_name/{style_name}", headers=other_user_headers)

            assert by_id.status_code == 200, by_id.get_data(as_text=True)
            assert by_id.get_json()["shared_key"] is None
            assert by_name.status_code == 200, by_name.get_data(as_text=True)
            assert by_name.get_json()["shared_key"] is None

    def test_an_anonymous_caller_gets_null(self, client, request_headers: dict[str, str]) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        style_name = f"image anonymous {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "image anonymous") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)) as style_id,
        ):
            by_id = client.get(f"/api/v2/styles/image/{style_id}", headers=anonymous_headers)
            by_name = client.get(f"/api/v2/styles/image_by_name/{style_name}", headers=anonymous_headers)

            assert by_id.status_code == 200, by_id.get_data(as_text=True)
            assert by_id.get_json()["shared_key"] is None
            assert by_name.status_code == 200, by_name.get_data(as_text=True)
            assert by_name.get_json()["shared_key"] is None

    def test_the_anonymous_key_is_served_as_an_anonymous_caller(self, client, request_headers: dict[str, str]) -> None:
        """A generated client sends the anonymous key when none is configured, and gets the public body from the shared cache."""
        anonymous_key_headers = {"apikey": "0000000000", "Client-Agent": request_headers["Client-Agent"]}
        style_name = f"image anonymous key {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "image anonymous key") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)) as style_id,
        ):
            by_id = client.get(f"/api/v2/styles/image/{style_id}", headers=anonymous_key_headers)
            by_name = client.get(f"/api/v2/styles/image_by_name/{style_name}", headers=anonymous_key_headers)

            assert by_id.status_code == 200, by_id.get_data(as_text=True)
            assert by_id.get_json()["shared_key"] is None
            assert "Cache-Control" not in by_id.headers
            assert by_name.status_code == 200, by_name.get_data(as_text=True)
            assert by_name.get_json()["shared_key"] is None
            assert "Cache-Control" not in by_name.headers

    def test_the_cached_anonymous_response_is_not_served_to_the_owner(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        with (
            created_shared_key(client, request_headers, "image cache order") as shared_key_id,
            created_style(client, request_headers, style_body("image cache order", sharedkey=shared_key_id)) as style_id,
        ):
            anonymous_response = client.get(f"/api/v2/styles/image/{style_id}", headers=anonymous_headers)
            owner_response = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers)
            later_anonymous_response = client.get(f"/api/v2/styles/image/{style_id}", headers=anonymous_headers)

            assert anonymous_response.get_json()["shared_key"] is None
            assert owner_response.get_json()["shared_key"]["id"] == shared_key_id
            assert later_anonymous_response.get_json()["shared_key"] is None

    def test_the_style_list_serves_null_to_the_owner(self, client, request_headers: dict[str, str]) -> None:
        list_tag = f"sharedkeylist{uuid.uuid4().hex[:8]}"
        body_overrides = {"public": True, "tags": [list_tag]}
        with (
            created_shared_key(client, request_headers, "image list") as shared_key_id,
            created_style(
                client,
                request_headers,
                style_body("image list", sharedkey=shared_key_id, **body_overrides),
            ) as style_id,
        ):
            response = client.get(f"/api/v2/styles/image?tag={list_tag}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            listed_styles = {listed_style["id"]: listed_style for listed_style in response.get_json()}
            assert listed_styles[style_id]["shared_key"] is None


class TestImageStyleReadCache:
    """An anonymous read of an image style reflects a change to its examples straight away."""

    def test_an_added_example_is_served(self, client, request_headers: dict[str, str]) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, request_headers, style_body("cached example read")) as style_id:
            before = client.get(f"/api/v2/styles/image/{style_id}", headers=anonymous_headers)
            assert before.status_code == 200, before.get_data(as_text=True)
            assert before.get_json()["examples"] == []

            added = client.post(
                f"/api/v2/styles/image/{style_id}/example",
                json={"url": "https://example.com/cached-example.webp", "primary": False},
                headers=request_headers,
            )
            assert added.status_code == 200, added.get_data(as_text=True)

            after = client.get(f"/api/v2/styles/image/{style_id}", headers=anonymous_headers)
            assert [example["url"] for example in after.get_json()["examples"]] == ["https://example.com/cached-example.webp"]
