# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end coverage for the per-baseline image model policy.

The reference is pinned to one model per baseline of interest, so what the API accepts is decided by
the policy table rather than by whatever models the live reference happens to publish. A baseline with
no row of its own is represented too, because the permissive default is what keeps a model published
upstream ahead of a backend release requestable.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import Any

import pytest
from flask.testing import FlaskClient
from horde_model_reference.meta_consts import KNOWN_IMAGE_GENERATION_BASELINE
from PIL import Image

from tests.fixture_types import MakeApiUser
from tests.unit.model_reference_seed import seed_image_reference

BRIDGE_AGENT = "AI Horde Worker reGen:9.0.1-citests:https://github.com/Haidra-Org/horde-worker-reGen"

SD1_MODEL = "sd1_model"
SD2_MODEL = "sd2_model"
SDXL_MODEL = "sdxl_model"
CASCADE_MODEL = "cascade_model"
FLUX_MODEL = "flux_model"
ZIMAGE_MODEL = "zimage_model"
KREA2_MODEL = "krea2_model"
ANIMA_MODEL = "anima_model"
# A record the pending (beta) overlay contributed. Beta carries no flag of its own: a pending model is
# in the reference like any other, which is the whole of what "beta models need no backend change" means.
BETA_ONLY_MODEL = "beta_only_model"
# A baseline the catalog publishes no record for, so its capabilities are the conservative default and no
# bridge release names it.
FUTURE_MODEL = "future_model"
FUTURE_BASELINE = "some_future_baseline"

UNKNOWN_MODEL = "totally_unknown_model"
UNKNOWN_SUFFIXED_MODEL = "totally_unknown_model [SDXL]"

SEEDED_MODELS: dict[str, KNOWN_IMAGE_GENERATION_BASELINE | str] = {
    SD1_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_1,
    SD2_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_2_768,
    SDXL_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl,
    CASCADE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_cascade,
    FLUX_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.flux_1,
    ZIMAGE_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.z_image_turbo,
    KREA2_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.krea2_turbo,
    ANIMA_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.anima,
    BETA_ONLY_MODEL: KNOWN_IMAGE_GENERATION_BASELINE.stable_diffusion_xl,
    FUTURE_MODEL: FUTURE_BASELINE,
}


@pytest.fixture(autouse=True)
def _seeded_image_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the process-wide image reference to the policy fixtures for every test here."""
    seed_image_reference(monkeypatch, SEEDED_MODELS)


@pytest.fixture(autouse=True)
def _no_rate_limit() -> Iterator[None]:
    """Disable Flask-Limiter so the per-endpoint limits do not turn into spurious 429s."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _source_image_b64() -> str:
    source_image = Image.open("img_stable/0.jpg")
    buffer = BytesIO()
    source_image.save(buffer, format="Webp", quality=50, exact=True)
    return base64.b64encode(buffer.getvalue()).decode("utf8")


def _async_dict(models: list[str], *, params: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """Build a minimal txt2img request body for the given models."""
    request_body: dict[str, Any] = {
        "prompt": "a horde of policy-abiding robots",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 512,
            "height": 512,
            "steps": 8,
            "cfg_scale": 7.5,
            "sampler_name": "k_euler_a",
            **(params or {}),
        },
        "models": models,
    }
    request_body.update(overrides)
    return request_body


@contextmanager
def _submitted(client: FlaskClient, request_headers: dict[str, str], request_body: dict[str, Any]) -> Iterator[Any]:
    """Post an async request and cancel whatever waiting prompt it created."""
    response = client.post("/api/v2/generate/async", json=request_body, headers=request_headers)
    try:
        yield response
    finally:
        if response.status_code == 202:
            client.delete(f"/api/v2/generate/status/{response.get_json()['id']}", headers=request_headers)


def _assert_accepted(client: FlaskClient, request_headers: dict[str, str], request_body: dict[str, Any]) -> None:
    with _submitted(client, request_headers, request_body) as response:
        assert response.status_code == 202, response.get_data(as_text=True)


def _assert_rejected(
    client: FlaskClient,
    request_headers: dict[str, str],
    request_body: dict[str, Any],
    rc: str,
) -> None:
    with _submitted(client, request_headers, request_body) as response:
        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json().get("rc") == rc, response.get_data(as_text=True)


class TestPlainRequestAcceptance:
    @pytest.mark.parametrize("model_name", sorted(SEEDED_MODELS))
    def test_a_plain_txt2img_request_is_accepted_for_every_baseline(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_accepted(client, request_headers, _async_dict([model_name]))


class TestHiResFix:
    @pytest.mark.parametrize("model_name", [FLUX_MODEL, ZIMAGE_MODEL, KREA2_MODEL, ANIMA_MODEL])
    def test_hires_fix_is_rejected_on_a_baseline_whose_graph_lacks_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([model_name], params={"hires_fix": True}),
            "HiResMismatch",
        )

    def test_hires_fix_is_rejected_on_an_uncatalogued_baseline_no_bridge_renders_it_on(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([FUTURE_MODEL], params={"hires_fix": True}),
            "HiResMismatch",
        )

    @pytest.mark.parametrize("model_name", [SD1_MODEL, SDXL_MODEL, CASCADE_MODEL])
    def test_hires_fix_is_accepted_where_the_baseline_renders_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_accepted(client, request_headers, _async_dict([model_name], params={"hires_fix": True}))

    def test_the_strictest_baseline_in_a_multi_model_request_decides(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        # The job can be dispatched for either model, so a feature only one of them renders is refused.
        _assert_rejected(
            client,
            request_headers,
            _async_dict([SD1_MODEL, FLUX_MODEL], params={"hires_fix": True}),
            "HiResMismatch",
        )


class TestControlNet:
    @pytest.mark.object_storage
    @pytest.mark.usefixtures("object_store_ready")
    @pytest.mark.parametrize("model_name", [SDXL_MODEL, CASCADE_MODEL, FLUX_MODEL, ZIMAGE_MODEL, KREA2_MODEL, ANIMA_MODEL])
    def test_a_control_type_is_rejected_on_a_baseline_with_no_controlnet(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict(
                [model_name],
                params={"control_type": "canny"},
                source_image=_source_image_b64(),
                source_processing="img2img",
            ),
            "ControlNetMismatch",
        )

    def test_a_control_type_with_no_conditioned_model_is_rejected_before_the_source_image_is_read(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        # SD2 accepts a control map, so the rejection names the missing control type rather than the
        # baseline, and it lands ahead of the source-image requirement.
        _assert_rejected(
            client,
            request_headers,
            _async_dict([SD2_MODEL], params={"control_type": "normal"}),
            "ControlNetUnsupported",
        )

    @pytest.mark.object_storage
    @pytest.mark.usefixtures("object_store_ready")
    def test_a_control_type_is_rejected_on_an_uncatalogued_baseline_no_bridge_drives_it_on(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict(
                [FUTURE_MODEL],
                params={"control_type": "canny"},
                source_image=_source_image_b64(),
                source_processing="img2img",
            ),
            "ControlNetMismatch",
        )


class TestTransparency:
    def test_transparency_is_rejected_on_a_baseline_that_cannot_render_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([CASCADE_MODEL], params={"transparent": True}),
            "InvalidTransparencyModel",
        )

    def test_transparency_is_rejected_on_an_uncatalogued_baseline_no_bridge_renders_it_on(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([FUTURE_MODEL], params={"transparent": True}),
            "InvalidTransparencyModel",
        )

    @pytest.mark.parametrize("model_name", [SD1_MODEL, SDXL_MODEL])
    def test_transparency_is_accepted_where_the_baseline_renders_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_accepted(client, request_headers, _async_dict([model_name], params={"transparent": True}))


class TestQrCodeWorkflow:
    QR_CODE_PARAMS = {
        "workflow": "qr_code",
        "extra_texts": [{"text": "https://aihorde.net", "reference": "qr_code"}],
    }

    def test_the_qr_code_workflow_is_rejected_on_a_baseline_that_cannot_render_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([CASCADE_MODEL], params=self.QR_CODE_PARAMS),
            "ControlNetMismatch.",
        )

    @pytest.mark.parametrize("model_name", [SD1_MODEL, SDXL_MODEL])
    def test_the_qr_code_workflow_is_accepted_where_the_baseline_renders_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_accepted(client, request_headers, _async_dict([model_name], params=self.QR_CODE_PARAMS))

    def test_the_qr_code_workflow_is_rejected_while_a_baseline_record_is_missing(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([FUTURE_MODEL], params=self.QR_CODE_PARAMS),
            "ControlNetMismatch.",
        )


class TestRemix:
    def test_remix_is_rejected_on_a_baseline_that_cannot_render_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([SD1_MODEL], source_processing="remix"),
            "InvalidRemix",
        )

    @pytest.mark.parametrize("model_name", [CASCADE_MODEL])
    def test_remix_is_accepted_where_the_baseline_renders_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
        model_name: str,
    ) -> None:
        _assert_accepted(client, request_headers, _async_dict([model_name], source_processing="remix"))

    def test_remix_is_rejected_while_a_baseline_record_is_missing(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([FUTURE_MODEL], source_processing="remix"),
            "InvalidRemix",
        )


class TestFlowShift:
    def test_flow_shift_is_rejected_where_the_graph_would_ignore_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([ZIMAGE_MODEL], params={"flow_shift": 3.0}),
            "FlowShiftInapplicable",
        )

    def test_flow_shift_is_rejected_on_an_uncatalogued_baseline_no_bridge_shifts(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_rejected(
            client,
            request_headers,
            _async_dict([FUTURE_MODEL], params={"flow_shift": 3.0}),
            "FlowShiftInapplicable",
        )

    def test_flow_shift_is_accepted_where_the_graph_applies_it(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        _assert_accepted(client, request_headers, _async_dict([FLUX_MODEL], params={"flow_shift": 3.0}))


class TestStyleSubstitutedModels:
    def test_the_style_model_is_what_the_policy_is_applied_to(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        # A style replaces the model list, so the effective model decides. The request names a
        # remix-capable model, and the style's SD1 model is the one the remix rule is read against.
        style_body = {
            "name": "sd1 policy style",
            "info": "sd1 policy style",
            "public": True,
            "prompt": "{p}###{np}",
            "nsfw": False,
            "params": {
                "width": 512,
                "height": 512,
                "steps": 8,
                "cfg_scale": 7.5,
                "sampler_name": "k_euler_a",
            },
            "models": [SD1_MODEL],
        }
        style_response = client.post("/api/v2/styles/image", json=style_body, headers=request_headers)
        assert style_response.status_code < 400, style_response.get_data(as_text=True)
        style_id = style_response.get_json()["id"]

        try:
            _assert_rejected(
                client,
                request_headers,
                _async_dict([CASCADE_MODEL], style=style_id, source_processing="remix"),
                "InvalidRemix",
            )
        finally:
            client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)

    def test_a_style_naming_a_model_that_cannot_render_its_params_is_refused_at_creation(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        # Style creation runs the same validator, so a style is never stored in a shape no request
        # using it could be accepted in.
        style_body = {
            "name": "flux hires policy style",
            "info": "flux hires policy style",
            "public": True,
            "prompt": "{p}###{np}",
            "nsfw": False,
            "params": {
                "width": 512,
                "height": 512,
                "steps": 8,
                "cfg_scale": 7.5,
                "sampler_name": "k_euler_a",
                "hires_fix": True,
            },
            "models": [FLUX_MODEL],
        }
        style_response = client.post("/api/v2/styles/image", json=style_body, headers=request_headers)
        assert style_response.status_code == 400, style_response.get_data(as_text=True)
        assert style_response.get_json().get("rc") == "HiResMismatch", style_response.get_data(as_text=True)


def _image_pop_dict(worker_name: str, models: list[str]) -> dict[str, Any]:
    return {
        "name": worker_name,
        "models": models,
        "bridge_agent": BRIDGE_AGENT,
        "nsfw": True,
        "amount": 1,
        "max_pixels": 4194304,
        "allow_img2img": True,
        "allow_painting": True,
        "allow_unsafe_ipaddr": True,
        "allow_post_processing": True,
        "allow_controlnet": True,
        "allow_lora": True,
    }


def _worker_models(client: FlaskClient, api_key: str, worker_name: str) -> list[str]:
    headers = {"apikey": api_key, "Client-Agent": BRIDGE_AGENT}
    by_name = client.get(f"/api/v2/workers/name/{worker_name}", headers=headers)
    assert by_name.status_code == 200, by_name.get_data(as_text=True)
    worker_id = by_name.get_json()["id"]
    details = client.get(f"/api/v2/workers/{worker_id}", headers=headers)
    assert details.status_code == 200, details.get_data(as_text=True)
    return details.get_json()["models"]


class TestWorkerModelAdvertising:
    def test_a_worker_may_offer_any_model_the_reference_carries(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        # A pending model is in the reference like any other, so no customizer role is involved, and a
        # baseline with no policy row is no reason to strip a model either.
        owner = make_api_user(trusted=True, kudos=100)
        headers = {"apikey": owner.api_key, "Client-Agent": BRIDGE_AGENT}
        offered_models = [BETA_ONLY_MODEL, FUTURE_MODEL]

        pop = client.post(
            "/api/v2/generate/pop",
            json=_image_pop_dict("Beta Dreamer", offered_models),
            headers=headers,
        )
        assert pop.status_code == 200, pop.get_data(as_text=True)
        assert sorted(_worker_models(client, owner.api_key, "Beta Dreamer")) == sorted(offered_models)

    def test_a_worker_offering_only_unrecognised_models_is_refused(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100)
        pop = client.post(
            "/api/v2/generate/pop",
            json=_image_pop_dict("Stranger Dreamer", [UNKNOWN_MODEL]),
            headers={"apikey": owner.api_key, "Client-Agent": BRIDGE_AGENT},
        )
        assert pop.status_code == 400, pop.get_data(as_text=True)
        assert "unrecognised models" in pop.get_json()["message"], pop.get_data(as_text=True)

    def test_a_customizer_may_offer_a_model_the_reference_has_never_heard_of(
        self,
        client: FlaskClient,
        make_api_user: MakeApiUser,
    ) -> None:
        owner = make_api_user(trusted=True, kudos=100, customizer=True)
        pop = client.post(
            "/api/v2/generate/pop",
            json=_image_pop_dict("Customizer Dreamer", [UNKNOWN_SUFFIXED_MODEL]),
            headers={"apikey": owner.api_key, "Client-Agent": BRIDGE_AGENT},
        )
        assert pop.status_code == 200, pop.get_data(as_text=True)
        assert _worker_models(client, owner.api_key, "Customizer Dreamer") == [UNKNOWN_SUFFIXED_MODEL]


class TestKudosQuote:
    def _quote(self, client: FlaskClient, request_headers: dict[str, str], model_name: str) -> int:
        response = client.post(
            "/api/v2/generate/async",
            json=_async_dict([model_name], dry_run=True),
            headers=request_headers,
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        return response.get_json()["kudos"]

    def test_two_baselines_priced_alike_quote_alike_and_both_exceed_sd1(
        self,
        client: FlaskClient,
        request_headers: dict[str, str],
    ) -> None:
        krea2_quote = self._quote(client, request_headers, KREA2_MODEL)
        zimage_quote = self._quote(client, request_headers, ZIMAGE_MODEL)
        sd1_quote = self._quote(client, request_headers, SD1_MODEL)

        assert krea2_quote == zimage_quote
        assert krea2_quote > sd1_quote
