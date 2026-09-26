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

from horde.apis.limiter_api import REQUEST_2SEC_LIMIT_PER_IP, REQUEST_90MIN_LIMIT_PER_IP
from horde.apis.v2.kobold import text_style_contract_vocabulary
from horde.apis.v2.kobold_styles import TEXT_STYLE_WRITE_WINDOW_RATE_LIMIT
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


def test_the_style_write_limits_are_the_ones_the_endpoints_declare(client):
    contract = client.get(CONTRACT_URL).get_json()

    assert contract["text"]["style_write_rate_limits"] == [
        TEXT_STYLE_WRITE_WINDOW_RATE_LIMIT,
        REQUEST_2SEC_LIMIT_PER_IP,
    ]
    assert contract["image"]["style_write_rate_limits"] == [
        REQUEST_90MIN_LIMIT_PER_IP,
        REQUEST_2SEC_LIMIT_PER_IP,
    ]


def test_repeated_requests_serve_an_unchanged_contract(client):
    # The contract is compiled once per process and handed to every caller, so anything that mutated it
    # on the way out would corrupt it for every later request rather than just its own.
    first = client.get(CONTRACT_URL).get_json()
    second = client.get(CONTRACT_URL).get_json()

    assert first == second
