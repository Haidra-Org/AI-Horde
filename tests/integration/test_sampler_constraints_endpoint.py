# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The published sampler constraints document, served over HTTP.

The document's contents are covered by ``tests/unit/test_sampler_constraints.py``. What only the HTTP
layer can show is that the route is registered, that it needs no authentication, and that the payload
survives Flask's JSON encoder, which is stricter than the shape checks: an unbounded knob maximum is an
infinity, and infinity has no JSON representation.
"""

import json

import pytest

from horde.consts import KNOWN_SAMPLERS

CONSTRAINTS_URL = "/api/v2/status/sampler_constraints"

pytestmark = pytest.mark.integration


def test_the_endpoint_serves_without_authentication(client):
    # A client needs this before it has an API key, to know what settings to offer at all.
    response = client.get(CONSTRAINTS_URL)
    assert response.status_code == 200


def test_the_payload_survives_the_json_encoder(client):
    response = client.get(CONSTRAINTS_URL)
    document = response.get_json()

    assert isinstance(document, dict)
    assert set(document) == {
        "samplers",
        "schema_version",
        "execution_contracts",
        "hard_constraints",
        "recommendations",
        "advisories",
        "work_accounting",
        "presentation_tiers",
    }


def test_every_accepted_sampler_is_served(client):
    document = client.get(CONSTRAINTS_URL).get_json()

    assert set(document["samplers"]) == set(KNOWN_SAMPLERS)


def test_an_unbounded_maximum_is_served_as_null(client):
    # Infinity is not valid JSON. A client parsing this strictly would fail on the churn window.
    document = client.get(CONSTRAINTS_URL).get_json()
    tmax = document["samplers"]["k_euler"]["accepted_settings"]["sampler_s_tmax"]

    assert tmax["maximum"] is None
    assert tmax["default"] is None


def test_the_published_swagger_uses_swagger_2_schema_keywords(client):
    response = client.get("/api/swagger.json")
    assert response.status_code == 200

    swagger = response.get_json()
    schema = swagger["definitions"]["SamplerConstraintsDocument"]
    rendered = json.dumps(schema)

    assert swagger["swagger"] == "2.0"
    assert '"anyOf"' not in rendered
    assert '"const"' not in rendered
    assert '"propertyNames"' not in rendered
    assert '"$defs"' not in rendered
    assert rendered.count('"x-nullable"') == 4


def test_the_hard_constraints_are_served(client):
    document = client.get(CONSTRAINTS_URL).get_json()
    hard = document["hard_constraints"]

    assert {"sampler": "dpmpp_3m_sde", "scheduler": "normal"} in hard["rejected_sampler_scheduler_pairings"]
    assert set(hard["scheduler_baseline_applicability"]["align_your_steps"]) == {
        "stable_diffusion_1",
        "stable_diffusion_xl",
    }


def test_recommendations_are_served_with_their_provenance(client):
    document = client.get(CONSTRAINTS_URL).get_json()

    assert document["recommendations"]
    for recommendation in document["recommendations"]:
        assert recommendation["provenance"] in {"upstream_author", "community", "measured", "user_ruled"}


def test_the_presentation_tier_is_served(client):
    document = client.get(CONSTRAINTS_URL).get_json()

    assert set(document["presentation_tiers"]["recommended"]) == {
        "k_euler",
        "k_euler_a",
        "k_dpmpp_2m",
        "dpmpp_2m_sde",
        "k_dpmpp_sde",
        "lcm",
        "uni_pc",
        "DDIM",
    }
    for sampler, entry in document["samplers"].items():
        assert entry["presentation_tier"] in {"recommended", "advanced"}, sampler


def test_both_measured_cost_ratios_are_served_with_their_basis(client):
    # Served as bare numbers they would read like prices, which they are not.
    document = client.get(CONSTRAINTS_URL).get_json()
    work_accounting = document["work_accounting"]

    assert work_accounting["measured_cost_ratio_provenance"] == "measured"
    assert work_accounting["authoritative_field"] == "work_profile"
    assert work_accounting["measured_cost_ratio_source"].endswith(".json")
    assert work_accounting["measured_cost_ratio_sd15_note"]
    assert work_accounting["measured_cost_ratio_sdxl_note"]

    euler = document["samplers"]["k_euler"]
    assert euler["measured_cost_ratio_sd15"] == 1.0
    assert euler["measured_cost_ratio_sdxl"] == 1.0
    assert document["samplers"]["k_dpm_adaptive"]["measured_cost_ratio_sdxl"] is None
    assert document["samplers"]["k_dpm_adaptive"]["work_profile"] == {
        "kind": "adaptive",
        "estimated_work_units_per_request": 40,
        "finite_ceiling_contract_versions": ["1.0"],
    }
    assert document["schema_version"] == "1.0"
    assert document["execution_contracts"]["1.0"] == {
        "version": "1.0",
        "guarantees": [
            {
                "name": "bounded_dpm_adaptive_v1",
                "sampler": "k_dpm_adaptive",
                "maximum_solver_iterations": {
                    "trajectory_multiplier_numerator": 5,
                    "trajectory_multiplier_denominator": 4,
                    "rounding": "ceiling",
                },
                "work_units_per_solver_iteration_source": "sampler_order",
            },
        ],
    }
