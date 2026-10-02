# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The text style endpoints: creating a style and reading it back.

A style stores a prompt template, a params body and a model list. Its details are served through the
text style params model, so only the params that model declares come back out.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import pytest
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from tests.fixture_types import MakeApiUser

TEXT_MODELS = ["elinas/chronos-70b-v2"]

STYLE_PROMPT = "### Instruction:\n{p}\n\n### Response:\n"

POLICY = {
    "override": "listed",
    "overridable": ["max_length", "max_context_length"],
    "ceilings": {"max_length": 512, "max_context_length": 4096},
}

TEMPLATE_FIELDS = [
    {"name": "caption", "description": "What the picture shows", "required": True},
    {"name": "tags", "description": "Comma-separated tags", "required": False},
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


def post_style(client: FlaskClient, request_headers: dict[str, str], body: dict[str, Any]) -> TestResponse:
    """Attempt to create a text style and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the style would belong to.
        body: The creation body.

    Returns:
        The response, for a case that expects a rejection.
    """
    return client.post("/api/v2/styles/text", json=body, headers=request_headers)


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


@contextmanager
def created_image_style(client: FlaskClient, request_headers: dict[str, str], name: str) -> Iterator[str]:
    """Create an image style for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the style belongs to.
        name: The style name.

    Yields:
        The id of the new style.
    """
    body = {
        "name": name,
        "info": "An image style used by the text style endpoint tests.",
        "prompt": "{p}, watercolour###{np}",
        "params": {"steps": 8, "width": 512, "height": 512},
        "models": ["stable_diffusion"],
        "public": False,
        "nsfw": False,
    }
    response = client.post("/api/v2/styles/image", json=body, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    style_id = response.get_json()["id"]
    try:
        yield style_id
    finally:
        client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)


class TestStyleCollectionTypes:
    """A collection holds styles of one type only."""

    def test_a_collection_mixing_image_and_text_styles_is_refused(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # Names, rather than ids, keep each style unique to this case across reruns.
        suffix = uuid.uuid4().hex[:8]
        text_name = f"collection text {suffix}"
        image_name = f"collection image {suffix}"
        with (
            created_style(client, request_headers, style_body(text_name)),
            created_image_style(client, request_headers, image_name),
        ):
            response = client.post(
                "/api/v2/collections",
                json={"name": f"mixed collection {suffix}", "styles": [text_name, image_name]},
                headers=request_headers,
            )

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleMismatch"

    def test_a_collection_mixing_styles_named_by_id_is_refused(self, client, request_headers: dict[str, str]) -> None:
        suffix = uuid.uuid4().hex[:8]
        with (
            created_style(client, request_headers, style_body(f"collection text id {suffix}")) as text_style_id,
            created_image_style(client, request_headers, f"collection image id {suffix}") as image_style_id,
        ):
            response = client.post(
                "/api/v2/collections",
                json={"name": f"mixed id collection {suffix}", "styles": [text_style_id, image_style_id]},
                headers=request_headers,
            )

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleMismatch"

    def test_a_collection_of_styles_named_by_id_takes_their_type(self, client, request_headers: dict[str, str]) -> None:
        suffix = uuid.uuid4().hex[:8]
        with created_style(client, request_headers, style_body(f"collection by id {suffix}")) as style_id:
            created = client.post(
                "/api/v2/collections",
                json={"name": f"id collection {suffix}", "styles": [style_id]},
                headers=request_headers,
            )
            assert created.status_code == 200, created.get_data(as_text=True)
            collection_id = created.get_json()["id"]
            try:
                details = client.get(f"/api/v2/collections/{collection_id}", headers=request_headers).get_json()

                assert details["type"] == "text"
            finally:
                client.delete(f"/api/v2/collections/{collection_id}", headers=request_headers)

    def test_a_patch_that_leaves_the_styles_alone_is_accepted(self, client, request_headers: dict[str, str]) -> None:
        suffix = uuid.uuid4().hex[:8]
        with created_style(client, request_headers, style_body(f"collection rename {suffix}")) as style_id:
            created = client.post(
                "/api/v2/collections",
                json={"name": f"before rename {suffix}", "styles": [style_id]},
                headers=request_headers,
            )
            assert created.status_code == 200, created.get_data(as_text=True)
            collection_id = created.get_json()["id"]
            try:
                patched = client.patch(
                    f"/api/v2/collections/{collection_id}",
                    json={"name": f"after rename {suffix}"},
                    headers=request_headers,
                )

                assert patched.status_code == 200, patched.get_data(as_text=True)
                details = client.get(f"/api/v2/collections/{collection_id}", headers=request_headers).get_json()
                assert details["name"] == f"after rename {suffix}"
                assert details["type"] == "text"
            finally:
                client.delete(f"/api/v2/collections/{collection_id}", headers=request_headers)


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


class TestTextStyleContract:
    """A text style declaring a parameter policy or template fields."""

    def test_the_declarations_are_stored_and_served(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract create", parameter_policy=POLICY, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers)

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
        with created_style(client, request_headers, style_body("contract absent")) as style_id:
            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()

            # Both keys are always present, so a client never has to tell an absent key from a null one.
            assert details["parameter_policy"] is None
            assert details["template_fields"] is None
            assert details["prompt"] == STYLE_PROMPT

    def test_the_params_a_policy_can_cap_are_served(self, client, request_headers: dict[str, str]) -> None:
        # Without these a client cannot size a request against the style it is about to use.
        with created_style(client, request_headers, style_body("contract sizes")) as style_id:
            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()

            assert details["params"]["max_length"] == 80
            assert details["params"]["max_context_length"] == 1024

    def test_a_ceiling_on_the_request_count_is_accepted(self, client, request_headers: dict[str, str]) -> None:
        # The number of takes always comes from the request, so a text style can cap it under any mode.
        policy = {"override": "none", "ceilings": {"n": 4}}
        with created_style(client, request_headers, style_body("contract takes ceiling", parameter_policy=policy)) as style_id:
            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()

            assert details["parameter_policy"]["ceilings"] == {"n": 4}

    def test_a_patch_replaces_the_declarations(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract patch", parameter_policy=POLICY, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            replacement_policy = {"override": "all", "ceilings": {"max_length": 256}}
            replacement_fields = [{"name": "caption", "description": "A one-line caption", "required": False}]
            patch_response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json={"parameter_policy": replacement_policy, "template_fields": replacement_fields},
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
            assert details["parameter_policy"] == replacement_policy
            assert details["template_fields"] == replacement_fields

    def test_a_patch_that_omits_them_leaves_them_alone(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract patch omitted", parameter_policy=POLICY, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            patch_response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json={"info": "Only the description changes in this patch."},
                headers=request_headers,
            )
            assert patch_response.status_code == 200, patch_response.get_data(as_text=True)

            details = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers).get_json()
            assert details["parameter_policy"] == POLICY
            assert details["template_fields"] == TEMPLATE_FIELDS


class TestTextStyleContractRejections:
    """Declarations the text style endpoints refuse."""

    def test_overridable_outside_listed_mode_is_misplaced(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract misplaced", parameter_policy={"override": "all", "overridable": ["max_length"]})
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "StylePolicyOverridableMisplaced"

    def test_listed_mode_without_a_list_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract listed without list", parameter_policy={"override": "listed"})
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_an_unknown_overridable_parameter_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract unknown param", parameter_policy={"override": "listed", "overridable": ["max_tokens"]})
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_the_request_count_cannot_be_made_overridable(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("contract overridable n", parameter_policy={"override": "listed", "overridable": ["n"]})
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    @pytest.mark.parametrize("ceiling", [15, 4097])
    def test_a_ceiling_outside_the_parameter_range_is_rejected(
        self,
        client,
        request_headers: dict[str, str],
        ceiling: int,
    ) -> None:
        body = style_body(
            f"contract ceiling {ceiling}",
            parameter_policy={"override": "all", "ceilings": {"max_length": ceiling}},
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_ceiling_on_a_parameter_a_text_style_cannot_cap_is_rejected(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # width belongs to image requests, and temperature is a text param no text style may cap.
        for parameter_name in ("width", "temperature"):
            body = style_body(
                f"contract ceiling {parameter_name}",
                parameter_policy={"override": "all", "ceilings": {parameter_name: 2}},
            )
            response = post_style(client, request_headers, body)

            assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_ceiling_on_a_parameter_the_mode_does_not_hand_over_is_rejected(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        for mode_name, policy in (
            ("none", {"override": "none", "ceilings": {"max_length": 512}}),
            ("listed", {"override": "listed", "overridable": ["max_length"], "ceilings": {"max_context_length": 4096}}),
        ):
            body = style_body(f"contract inert ceiling {mode_name}", parameter_policy=policy)
            response = post_style(client, request_headers, body)

            assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_reserved_template_field_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract reserved field",
            template_fields=[{"name": "p", "description": "The prompt the horde already fills"}],
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_malformed_template_field_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract malformed field",
            template_fields=[{"name": "Caption Text", "description": "Not a lower snake_case identifier"}],
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_a_repeated_template_field_is_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract repeated field",
            template_fields=[
                {"name": "caption", "description": "The first declaration"},
                {"name": "caption", "description": "The second declaration"},
            ],
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)

    def test_more_template_fields_than_the_limit_are_rejected(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "contract too many fields",
            template_fields=[{"name": f"field_{index}", "description": "ok"} for index in range(17)],
        )
        response = post_style(client, request_headers, body)

        assert response.status_code == 400, response.get_data(as_text=True)


class TestTextStyleWriteReturnCodes:
    """The return code a refused text style write reports."""

    def test_a_prompt_without_the_prompt_placeholder_is_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_style(client, request_headers, style_body("missing placeholder", prompt="No placeholder here."))

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "StylePromptMissingVars"

    def test_a_shared_key_that_does_not_exist_is_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_style(client, request_headers, style_body("unknown shared key", sharedkey=str(uuid.uuid4())))

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "SharedKeyInvalid"

    def test_a_prompt_field_with_a_format_spec_is_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_style(client, request_headers, style_body("format spec", prompt="{p} {p:>20000}"))

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "StylePromptFieldInvalid"

    def test_a_patch_to_a_prompt_with_attribute_access_is_refused(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("attribute access patch")) as style_id:
            response = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json={"prompt": "{p} {p.__class__}"},
                headers=request_headers,
            )

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StylePromptFieldInvalid"

    def test_a_shared_key_without_kudos_is_refused(self, client, request_headers: dict[str, str]) -> None:
        with created_shared_key(client, request_headers, "empty style key", kudos=0) as shared_key_id:
            response = post_style(client, request_headers, style_body("empty shared key", sharedkey=shared_key_id))

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "SharedKeyEmpty"

    def test_reading_a_text_style_through_the_image_route_is_refused(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        with created_style(client, request_headers, style_body("read through image route")) as style_id:
            response = client.get(f"/api/v2/styles/image/{style_id}", headers=request_headers)

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleGetMistmatch"


@contextmanager
def created_shared_key(
    client: FlaskClient,
    request_headers: dict[str, str],
    name: str,
    *,
    kudos: int = 100,
) -> Iterator[str]:
    """Create a shared key for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the shared key belongs to.
        name: The shared key's name.
        kudos: The shared key's kudos budget.

    Yields:
        The id of the new shared key.
    """
    response = client.put("/api/v2/sharedkeys", json={"kudos": kudos, "name": name}, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    shared_key_id = response.get_json()["id"]
    try:
        yield shared_key_id
    finally:
        client.delete(f"/api/v2/sharedkeys/{shared_key_id}", headers=request_headers)


class TestTextStyleSharedKeyVisibility:
    """The shared key a text style generates under is served to its owner only."""

    def test_the_owner_gets_the_shared_key_by_id(self, client, request_headers: dict[str, str]) -> None:
        with (
            created_shared_key(client, request_headers, "text owner by id") as shared_key_id,
            created_style(client, request_headers, style_body("text owner by id", sharedkey=shared_key_id)) as style_id,
        ):
            response = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["shared_key"]["id"] == shared_key_id
            # The owner's copy must not be kept by any cache between the horde and the owner.
            assert response.headers["Cache-Control"] == "private, no-store"

    def test_the_owner_gets_the_shared_key_by_name(self, client, request_headers: dict[str, str]) -> None:
        style_name = f"text owner by name {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "text owner by name") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)),
        ):
            response = client.get(f"/api/v2/styles/text_by_name/{style_name}", headers=request_headers)

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
        style_name = f"text other user {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "text other user") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)) as style_id,
        ):
            by_id = client.get(f"/api/v2/styles/text/{style_id}", headers=other_user_headers)
            by_name = client.get(f"/api/v2/styles/text_by_name/{style_name}", headers=other_user_headers)

            assert by_id.status_code == 200, by_id.get_data(as_text=True)
            assert by_id.get_json()["shared_key"] is None
            assert by_name.status_code == 200, by_name.get_data(as_text=True)
            assert by_name.get_json()["shared_key"] is None

    def test_an_anonymous_caller_gets_null(self, client, request_headers: dict[str, str]) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        style_name = f"text anonymous {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "text anonymous") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)) as style_id,
        ):
            by_id = client.get(f"/api/v2/styles/text/{style_id}", headers=anonymous_headers)
            by_name = client.get(f"/api/v2/styles/text_by_name/{style_name}", headers=anonymous_headers)

            assert by_id.status_code == 200, by_id.get_data(as_text=True)
            assert by_id.get_json()["shared_key"] is None
            assert by_name.status_code == 200, by_name.get_data(as_text=True)
            assert by_name.get_json()["shared_key"] is None

    def test_the_anonymous_key_is_served_as_an_anonymous_caller(self, client, request_headers: dict[str, str]) -> None:
        """A generated client sends the anonymous key when none is configured, and gets the public body from the shared cache."""
        anonymous_key_headers = {"apikey": "0000000000", "Client-Agent": request_headers["Client-Agent"]}
        style_name = f"text anonymous key {uuid.uuid4().hex[:8]}"
        with (
            created_shared_key(client, request_headers, "text anonymous key") as shared_key_id,
            created_style(client, request_headers, style_body(style_name, sharedkey=shared_key_id)) as style_id,
        ):
            by_id = client.get(f"/api/v2/styles/text/{style_id}", headers=anonymous_key_headers)
            by_name = client.get(f"/api/v2/styles/text_by_name/{style_name}", headers=anonymous_key_headers)

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
            created_shared_key(client, request_headers, "text cache order") as shared_key_id,
            created_style(client, request_headers, style_body("text cache order", sharedkey=shared_key_id)) as style_id,
        ):
            anonymous_response = client.get(f"/api/v2/styles/text/{style_id}", headers=anonymous_headers)
            owner_response = client.get(f"/api/v2/styles/text/{style_id}", headers=request_headers)
            later_anonymous_response = client.get(f"/api/v2/styles/text/{style_id}", headers=anonymous_headers)

            assert anonymous_response.get_json()["shared_key"] is None
            assert owner_response.get_json()["shared_key"]["id"] == shared_key_id
            assert later_anonymous_response.get_json()["shared_key"] is None

    def test_the_style_list_serves_null_to_the_owner(self, client, request_headers: dict[str, str]) -> None:
        list_tag = f"sharedkeylist{uuid.uuid4().hex[:8]}"
        body_overrides = {"public": True, "tags": [list_tag]}
        with (
            created_shared_key(client, request_headers, "text list") as shared_key_id,
            created_style(
                client,
                request_headers,
                style_body("text list", sharedkey=shared_key_id, **body_overrides),
            ) as style_id,
        ):
            response = client.get(f"/api/v2/styles/text?tag={list_tag}", headers=request_headers)

            assert response.status_code == 200, response.get_data(as_text=True)
            listed_styles = {listed_style["id"]: listed_style for listed_style in response.get_json()}
            assert listed_styles[style_id]["shared_key"] is None


class TestTextStyleReadCache:
    """An anonymous read of a style reflects a write to it straight away, though such reads are cached."""

    def test_a_patch_is_served_by_id_and_by_name(self, client, request_headers: dict[str, str]) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        owner_alias = client.get("/api/v2/find_user", headers=request_headers).get_json()["username"]
        name = f"cached read {uuid.uuid4().hex[:8]}"
        read_paths = [
            f"/api/v2/styles/text_by_name/{name}",
            # The alias carries a '#', which a URL would otherwise read as the start of a fragment.
            f"/api/v2/styles/text_by_name/{quote(f'{owner_alias}::{name}')}",
        ]
        with created_style(client, request_headers, style_body(name)) as style_id:
            read_paths.append(f"/api/v2/styles/text/{style_id}")
            for path in read_paths:
                assert client.get(path, headers=anonymous_headers).status_code == 200

            patched = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json={"info": "Info written after the first read."},
                headers=request_headers,
            )
            assert patched.status_code == 200, patched.get_data(as_text=True)

            for path in read_paths:
                assert client.get(path, headers=anonymous_headers).get_json()["info"] == "Info written after the first read."

    def test_a_renamed_style_is_not_served_under_its_old_name(self, client, request_headers: dict[str, str]) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        old_name = f"before rename {uuid.uuid4().hex[:8]}"
        with created_style(client, request_headers, style_body(old_name)) as style_id:
            assert client.get(f"/api/v2/styles/text_by_name/{old_name}", headers=anonymous_headers).status_code == 200

            patched = client.patch(
                f"/api/v2/styles/text/{style_id}",
                json={"name": f"after rename {uuid.uuid4().hex[:8]}"},
                headers=request_headers,
            )
            assert patched.status_code == 200, patched.get_data(as_text=True)

            assert client.get(f"/api/v2/styles/text_by_name/{old_name}", headers=anonymous_headers).status_code == 404

    def test_a_deleted_style_is_not_served(self, client, request_headers: dict[str, str]) -> None:
        anonymous_headers = {"Client-Agent": request_headers["Client-Agent"]}
        created = post_style(client, request_headers, style_body(f"deleted read {uuid.uuid4().hex[:8]}"))
        assert created.status_code == 200, created.get_data(as_text=True)
        style_id = created.get_json()["id"]
        assert client.get(f"/api/v2/styles/text/{style_id}", headers=anonymous_headers).status_code == 200

        deleted = client.delete(f"/api/v2/styles/text/{style_id}", headers=request_headers)
        assert deleted.status_code == 200, deleted.get_data(as_text=True)

        assert client.get(f"/api/v2/styles/text/{style_id}", headers=anonymous_headers).status_code == 404
