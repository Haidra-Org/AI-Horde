# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust workload for image-reference changes made while request traffic is active."""

from __future__ import annotations

import os
import random
from typing import Any

from locust import HttpUser, between, task

_CONTROL_MODEL = os.environ["LOCUST_REFERENCE_CONTROL_MODEL"]
_MODELS = tuple(model.strip() for model in os.environ["LOCUST_REFERENCE_MODELS"].split(",") if model.strip())
_API_KEY = os.environ["LOCUST_REFERENCE_API_KEY"]


def _payload(model: str, *, qr_code: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "width": 512,
        "height": 512,
        "steps": 8,
        "cfg_scale": 7.5,
        "sampler_name": "k_euler_a",
    }
    if qr_code:
        params["workflow"] = "qr_code"
        params["extra_texts"] = [{"text": "https://aihorde.net", "reference": "qr_code"}]
    return {
        "prompt": "an organically load-tested reference refresh",
        "models": [model],
        "params": params,
        "nsfw": False,
        "r2": True,
        "trusted_workers": False,
    }


class ReferenceChurnRequester(HttpUser):
    """Mix a stable control with models that may appear while the run is active."""

    wait_time = between(0.01, 0.04)

    @task
    def quote(self) -> None:
        model = random.choice(_MODELS)
        qr_code = model != _CONTROL_MODEL and random.random() < 0.5
        with self.client.post(
            "/api/v2/generate/async",
            json=_payload(model, qr_code=qr_code),
            headers={"apikey": _API_KEY, "Client-Agent": "aihorde_reference_churn:1:test"},
            catch_response=True,
            name="/api/v2/generate/async [reference-churn]",
        ) as response:
            body = response.json() if response.content else {}
            if response.status_code == 202:
                response.success()
                request_id = body.get("id")
                if request_id:
                    with self.client.delete(
                        f"/api/v2/generate/status/{request_id}",
                        headers={"apikey": _API_KEY},
                        catch_response=True,
                        name="/api/v2/generate/status/[id] [reference-churn-cancel]",
                    ) as cancellation:
                        if cancellation.status_code in {200, 404, 410}:
                            cancellation.success()
                        else:
                            cancellation.failure(f"status={cancellation.status_code} body={cancellation.text[:200]}")
            elif model != _CONTROL_MODEL and response.status_code == 400 and body.get("rc") in {"UnsupportedModel", "ControlNetMismatch."}:
                # Readers may legitimately see either side of a snapshot boundary.
                response.success()
            else:
                response.failure(f"model={model} qr={qr_code}: status={response.status_code} body={body}")
