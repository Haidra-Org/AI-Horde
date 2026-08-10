# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cross-repo wire-contract client, executed under the local horde_sdk venv.

Reads a JSON job description on stdin, performs real HTTP pops against a live
AI-Horde server, and validates the responses with the local horde_sdk response
models. Emits a single-line JSON verdict on stdout. It imports only horde_sdk,
requests, and the standard library: never the AI-Horde ``horde`` package.
"""

from __future__ import annotations

import json
import sys

import requests
from horde_sdk.ai_horde_api.apimodels.alchemy.pop import AlchemyJobPopResponse, AlchemyPopRequest
from horde_sdk.ai_horde_api.apimodels.generate.async_ import ImageGenerateAsyncRequest, ImageGenerationInputPayload
from horde_sdk.ai_horde_api.apimodels.generate.pop import (
    ImageGenerateJobPopRequest,
    ImageGenerateJobPopResponse,
)
from horde_sdk.generation_parameters.alchemy.consts import KNOWN_ANNOTATION_CONTROL_TYPES


def _headers(cfg: dict) -> dict:
    return {"apikey": cfg["apikey"], "Client-Agent": cfg["client_agent"]}


def _check_alchemy(cfg: dict) -> dict:
    body = cfg["alchemy_pop_body"]

    request_model = AlchemyPopRequest(
        apikey=cfg["apikey"],
        name=body["name"],
        priority_usernames=[],
        forms=body["forms"],
        annotation_types=body["annotation_types"],
        amount=1,
    )
    reserialized = request_model.model_dump(by_alias=True, exclude_none=True, mode="json")
    if reserialized.get("annotation_types") != body["annotation_types"]:
        raise AssertionError(f"SDK AlchemyPopRequest dropped annotation_types: {reserialized!r}")

    response = requests.post(cfg["base_url"] + "/api/v2/interrogate/pop", json=body, headers=_headers(cfg), timeout=30)
    response.raise_for_status()
    raw = response.json()

    parsed = AlchemyJobPopResponse.model_validate(raw)
    forms = parsed.forms or []
    if not forms:
        raise AssertionError(f"alchemy pop returned no forms: {raw!r}")
    form = forms[0]
    if form.form != "annotation":
        raise AssertionError(f"unexpected form: {form.form!r} in {raw!r}")
    if form.payload is None or form.payload.control_type is None:
        raise AssertionError(f"SDK did not parse control_type from payload: {raw!r}")
    if form.payload.control_type != KNOWN_ANNOTATION_CONTROL_TYPES.canny:
        raise AssertionError(f"control_type did not round-trip: {form.payload.control_type!r}")
    if not form.r2_upload:
        raise AssertionError(f"annotation pop missing r2_upload: {raw!r}")

    return {
        "form": str(form.form),
        "control_type": str(form.payload.control_type),
        "control_type_is_enum": isinstance(form.payload.control_type, KNOWN_ANNOTATION_CONTROL_TYPES),
        "has_r2_upload": bool(form.r2_upload),
        "request_roundtrip_annotation_types": reserialized.get("annotation_types"),
    }


def _check_image_request(cfg: dict) -> dict:
    params = ImageGenerationInputPayload(**cfg["image_request_params"])
    request_model = ImageGenerateAsyncRequest(
        apikey=cfg["apikey"],
        prompt="SDK sampler request round trip",
        params=params,
        models=["stable_diffusion"],
    )
    reserialized = request_model.model_dump(by_alias=True, exclude_none=True, mode="json")["params"]
    for field, expected in cfg["image_request_params"].items():
        if reserialized.get(field) != expected:
            raise AssertionError(f"SDK ImageGenerateAsyncRequest changed {field}: {reserialized!r}")
    return {field: reserialized[field] for field in cfg["image_request_params"]}


def _check_image_pops(cfg: dict) -> dict:
    popped_payloads = []
    extended_controlnet_roundtrip = False

    for case in cfg["image_pop_cases"]:
        body = case["body"]
        request_model = ImageGenerateJobPopRequest(
            apikey=cfg["apikey"],
            name=body["name"],
            priority_usernames=[],
            models=body["models"],
            bridge_agent=body["bridge_agent"],
            amount=body["amount"],
            max_pixels=body["max_pixels"],
            allow_controlnet=body.get("allow_controlnet", False),
            allow_extended_controlnet=body["allow_extended_controlnet"],
        )
        reserialized = request_model.model_dump(by_alias=True, exclude_none=True, mode="json")
        if reserialized.get("allow_extended_controlnet") is not True:
            raise AssertionError(f"SDK ImageGenerateJobPopRequest dropped allow_extended_controlnet: {reserialized!r}")
        extended_controlnet_roundtrip = True

        response = requests.post(cfg["base_url"] + "/api/v2/generate/pop", json=body, headers=_headers(cfg), timeout=30)
        response.raise_for_status()
        raw = response.json()
        parsed = ImageGenerateJobPopResponse.model_validate(raw)
        if parsed.id_ is None:
            raise AssertionError(f"image pop matched no job: {raw!r}")

        payload = parsed.payload.model_dump(by_alias=True, exclude_none=True, mode="json")
        for field, expected in case["expected_payload"].items():
            if payload.get(field) != expected:
                raise AssertionError(f"SDK image pop changed {field}: {payload!r}")
        popped_payloads.append({field: payload[field] for field in case["expected_payload"]})

    return {
        "payloads": popped_payloads,
        "request_roundtrip_allow_extended_controlnet": extended_controlnet_roundtrip,
    }


def main() -> int:
    cfg = json.load(sys.stdin)
    verdict = {
        "alchemy": _check_alchemy(cfg),
        "image_request": _check_image_request(cfg),
        "image": _check_image_pops(cfg),
        "ok": True,
    }
    sys.stdout.write(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
