# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The published style contract, served over HTTP.

What the endpoint serves has to be what the style endpoints and the request path actually enforce, so
the cases here read the same vocabulary objects the validators are built from and compare. The rest is
what only the HTTP layer can show: the route is registered, it needs no authentication, and the payload
comes back through Flask's JSON encoder unchanged.
"""

import re

import pytest
from flask import Flask, request
from flask_limiter.util import get_qualified_name
from limits import RateLimitItem, parse

from horde.apis.v2.kobold import text_style_contract_vocabulary
from horde.apis.v2.stable import image_style_contract_vocabulary
from horde.classes.kobold.request_fit import ContextFit
from horde.style_contract_document import SCHEMA_VERSION

CONTRACT_URL = "/api/v2/status/style_contract"

pytestmark = pytest.mark.integration


def test_the_endpoint_serves_without_authentication(client):
    # A client needs this before it has an API key, to know what a style may declare at all.
    response = client.get(CONTRACT_URL)

    assert response.status_code == 200


def test_the_payload_carries_a_section_per_style_type(client):
    contract = client.get(CONTRACT_URL).get_json()

    assert set(contract) == {"schema_version", "text", "image"}
    assert contract["schema_version"] == SCHEMA_VERSION


def test_the_text_vocabulary_is_the_one_the_endpoints_validate_against(client):
    text_contract = client.get(CONTRACT_URL).get_json()["text"]
    vocabulary = text_style_contract_vocabulary()

    assert set(text_contract["overridable_parameters"]) == set(vocabulary.overridable_parameter_names)
    assert set(text_contract["ceiling_parameters"]) == set(vocabulary.ceiling_bounds)
    for parameter_name, published_bounds in text_contract["ceiling_parameters"].items():
        bound = vocabulary.ceiling_bounds[parameter_name]
        assert published_bounds == {"minimum": bound.minimum, "maximum": bound.maximum}


def test_the_image_vocabulary_is_the_one_the_endpoints_validate_against(client):
    image_contract = client.get(CONTRACT_URL).get_json()["image"]
    vocabulary = image_style_contract_vocabulary()

    assert set(image_contract["overridable_parameters"]) == set(vocabulary.overridable_parameter_names)
    assert set(image_contract["ceiling_parameters"]) == set(vocabulary.ceiling_bounds)
    for parameter_name, published_bounds in image_contract["ceiling_parameters"].items():
        bound = vocabulary.ceiling_bounds[parameter_name]
        assert published_bounds == {"minimum": bound.minimum, "maximum": bound.maximum}


def test_the_horde_filled_placeholders_are_published_per_type(client):
    contract = client.get(CONTRACT_URL).get_json()

    assert contract["text"]["placeholders"] == ["p"]
    assert contract["image"]["placeholders"] == ["p", "np"]
    assert contract["text"]["declared_fields_are_placeholders"] is True
    assert contract["image"]["declared_fields_are_placeholders"] is True


def test_the_protected_pattern_matches_what_the_text_path_keeps(client):
    # Published as a regular expression so a client can check a template before sending it.
    contract = client.get(CONTRACT_URL).get_json()
    protected_patterns = contract["text"]["protected_patterns"]

    assert len(protected_patterns) == 1
    pattern = re.compile(protected_patterns[0]["pattern"])
    assert pattern.search("### Instruction:\n{{[INPUT]}}{p}{{[OUTPUT]}}")
    assert not pattern.search("{{[input]}}")
    assert protected_patterns[0]["description"]

    # An image style's braces are all literal, so it has nothing to protect.
    assert contract["image"]["protected_patterns"] == []
    assert contract["image"]["brace_handling"]
    assert contract["text"]["brace_handling"]


def test_context_fitting_is_published_for_text_only(client):
    contract = client.get(CONTRACT_URL).get_json()

    assert contract["text"]["context_fit_modes"] == [mode.value for mode in ContextFit]
    assert contract["image"]["context_fit_modes"] is None


STYLE_WRITE_ROUTES = [
    ("text", "/api/v2/styles/text", "POST", "style_create_rate_limits"),
    ("text", "/api/v2/styles/text/00000000-0000-0000-0000-000000000000", "PATCH", "style_modify_rate_limits"),
    ("text", "/api/v2/styles/text/00000000-0000-0000-0000-000000000000", "DELETE", "style_modify_rate_limits"),
    ("image", "/api/v2/styles/image", "POST", "style_create_rate_limits"),
    ("image", "/api/v2/styles/image/00000000-0000-0000-0000-000000000000", "PATCH", "style_modify_rate_limits"),
    ("image", "/api/v2/styles/image/00000000-0000-0000-0000-000000000000", "DELETE", "style_modify_rate_limits"),
]
"""Each style write a client can make: the style type, a path and method reaching it, and the contract field publishing its limits."""


def enforced_rate_limits(app: Flask, path: str, method: str) -> set[RateLimitItem]:
    """Return the limits the route serving a request is decorated with, as an ordinary address sees them.

    Args:
        app: The Flask app the routes are registered on.
        path: A path the route serves.
        method: The HTTP method of the request.

    Returns:
        Every limit applied to the request, resolved inside a request context so a limit that depends on
        the caller's address resolves to the ordinary one.
    """
    from horde.limiter import limiter

    with app.test_request_context(path, method=method):
        view = app.view_functions[request.url_rule.endpoint]
        return {limit.limit for limit in limiter.limit_manager.decorated_limits(get_qualified_name(view))}


@pytest.mark.parametrize(("style_type", "path", "method", "contract_field"), STYLE_WRITE_ROUTES)
def test_each_style_write_publishes_the_limits_its_route_enforces(
    app: Flask,
    client,
    style_type: str,
    path: str,
    method: str,
    contract_field: str,
) -> None:
    contract = client.get(CONTRACT_URL).get_json()

    published = {parse(limit) for limit in contract[style_type][contract_field]}

    assert published == enforced_rate_limits(app, path, method)


def test_both_style_types_are_held_to_the_same_write_limits(client) -> None:
    """A client pacing its writes can do so the same way for either style type."""
    contract = client.get(CONTRACT_URL).get_json()

    for contract_field in ("style_create_rate_limits", "style_modify_rate_limits"):
        assert contract["text"][contract_field] == contract["image"][contract_field]


def test_repeated_requests_serve_an_unchanged_contract(client):
    # The contract is compiled once per process and handed to every caller, so anything that mutated it
    # on the way out would corrupt it for every later request rather than just its own.
    first = client.get(CONTRACT_URL).get_json()
    second = client.get(CONTRACT_URL).get_json()

    assert first == second
