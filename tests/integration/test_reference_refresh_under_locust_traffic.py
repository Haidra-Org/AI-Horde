# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise remote image-reference publication boundaries under concurrent request traffic."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from horde_model_reference import BaselineCapabilities, HordeBaselinePolicy, ImageBaselineRecord
from horde_model_reference.model_reference_records import ImageGenerationModelRecord
from werkzeug.serving import make_server

from horde import model_reference as model_reference_module
from tests.unit.model_reference_seed import BOOTSTRAP_BASELINE_CATALOG, make_image_record

pytestmark = pytest.mark.integration

CONTROL_MODEL = "reference-churn-control"
EXISTING_BASELINE_MODEL = "reference-churn-existing-baseline"
FUTURE_MODEL = "reference-churn-future-model"
FUTURE_BASELINE = "reference_churn_future_baseline"


class _StubTextResponse:
    @staticmethod
    def json() -> dict[str, Any]:
        return {}


class _QueryResult:
    def __init__(self, records: list[ImageGenerationModelRecord]) -> None:
        self._records = records

    def to_list(self) -> list[ImageGenerationModelRecord]:
        return self._records


@dataclass
class _RemoteReference:
    """A controllable PRIMARY and the replica-side catalog cache fetched from it."""

    baselines: dict[str, ImageBaselineRecord]
    models: dict[str, ImageGenerationModelRecord]
    local_baselines: dict[str, ImageBaselineRecord] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    baseline_captured: threading.Event = field(default_factory=threading.Event)
    release_baseline_fetch: threading.Event = field(default_factory=threading.Event)
    pause_next_baseline_fetch: bool = False
    failed_model_fetches: int = 0

    def refresh_baselines(self) -> bool:
        with self.lock:
            fetched = deepcopy(self.baselines)
            pause = self.pause_next_baseline_fetch
            self.pause_next_baseline_fetch = False
        if pause:
            self.baseline_captured.set()
            if not self.release_baseline_fetch.wait(timeout=5):
                raise TimeoutError("The test did not release the paused baseline fetch.")
        with self.lock:
            self.local_baselines = fetched
        return True

    def export_baselines(self) -> SimpleNamespace:
        with self.lock:
            return SimpleNamespace(baselines=deepcopy(self.local_baselines))

    def query_models(self, *args: Any, **kwargs: Any) -> _QueryResult:
        with self.lock:
            if self.failed_model_fetches:
                self.failed_model_fetches -= 1
                raise RuntimeError("simulated remote model-category failure")
            return _QueryResult(deepcopy(list(self.models.values())))

    def publish_baseline(self, record: ImageBaselineRecord) -> None:
        with self.lock:
            self.baselines[record.name] = record

    def publish_model(self, record: ImageGenerationModelRecord) -> None:
        with self.lock:
            self.models[record.name] = record


def _payload(model: str, *, qr_code: bool = False) -> dict[str, Any]:
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


def _quote(host: str, headers: dict[str, str], model: str, *, qr_code: bool = False) -> requests.Response:
    payload = _payload(model, qr_code=qr_code)
    payload["prompt"] = f"{payload['prompt']} probe-{time.monotonic_ns()}"
    response = requests.post(
        f"{host}/api/v2/generate/async",
        json=payload,
        headers=headers,
        timeout=5,
    )
    if response.status_code == 202:
        requests.delete(
            f"{host}/api/v2/generate/status/{response.json()['id']}",
            headers=headers,
            timeout=5,
        )
    return response


def _wait_for_traffic(request_count: list[int], minimum_requests: int) -> None:
    for _ in range(100):
        if request_count[0] >= minimum_requests:
            return
        time.sleep(0.05)
    raise AssertionError(f"Locust produced only {request_count[0]} requests")


def test_remote_reference_churn_stays_coherent_under_locust_traffic(
    monkeypatch: pytest.MonkeyPatch,
    app,
    request_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """Add baselines and models between remote reads while normal quotes continue."""
    from horde.limiter import limiter

    control_baseline = deepcopy(BOOTSTRAP_BASELINE_CATALOG.baselines["stable_diffusion_1"])
    remote = _RemoteReference(
        baselines={control_baseline.name: control_baseline},
        models={CONTROL_MODEL: make_image_record(CONTROL_MODEL, control_baseline.name)},
    )
    loader = model_reference_module.model_reference
    previous_snapshot = loader._image_snapshot
    manager = model_reference_module._get_reference_manager()
    previous_limiter = limiter.enabled

    monkeypatch.setattr(manager, "refresh_image_baselines", remote.refresh_baselines)
    monkeypatch.setattr(manager.image_baseline_store, "export", remote.export_baselines)
    monkeypatch.setattr(manager, "query", remote.query_models)
    monkeypatch.setattr(model_reference_module, "_image_reference_source", lambda manager: "remote-test")
    monkeypatch.setattr(model_reference_module.requests, "get", lambda *args, **kwargs: _StubTextResponse())

    request_count = [0]

    @app.before_request
    def _count_reference_churn_requests() -> None:
        from flask import request

        if request.path == "/api/v2/generate/async" and request.headers.get("Client-Agent", "").startswith(
            "aihorde_reference_churn:",
        ):
            request_count[0] += 1

    server = make_server("127.0.0.1", 0, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    host = f"http://127.0.0.1:{server.server_port}"
    locustfile = Path(__file__).parents[1] / "stress" / "locustfile_reference_churn.py"
    child_env = os.environ.copy()
    child_env.update(
        {
            "LOCUST_REFERENCE_API_KEY": request_headers["apikey"],
            "LOCUST_REFERENCE_CONTROL_MODEL": CONTROL_MODEL,
            "LOCUST_REFERENCE_MODELS": ",".join((CONTROL_MODEL, EXISTING_BASELINE_MODEL, FUTURE_MODEL)),
        },
    )
    locust_process: subprocess.Popen[str] | None = None
    limiter.enabled = False

    try:
        loader.call_function()
        server_thread.start()
        locust_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "locust",
                "-f",
                str(locustfile),
                "--headless",
                "--host",
                host,
                "--users",
                "4",
                "--spawn-rate",
                "4",
                "--run-time",
                "8s",
                "--stop-timeout",
                "2",
                "--csv",
                str(tmp_path / "reference-churn"),
                "--only-summary",
            ],
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_traffic(request_count, 20)

        # A baseline by itself changes no model lookup or fallback-pricing behavior.
        before_baseline = _quote(host, request_headers, FUTURE_MODEL, qr_code=True)
        assert before_baseline.status_code == 202
        future_baseline = ImageBaselineRecord(
            name=FUTURE_BASELINE,
            capabilities=BaselineCapabilities(qr_code=False),
            horde_policy=HordeBaselinePolicy(kudos=5, kudos_qr_code=7, batching=3, ttl=2, resolution_floor=1024),
        )
        remote.publish_baseline(future_baseline)
        loader.call_function()
        after_baseline = _quote(host, request_headers, FUTURE_MODEL, qr_code=True)
        assert after_baseline.status_code == before_baseline.status_code
        assert FUTURE_MODEL not in (loader.reference or {})
        assert loader.baseline_record(future_baseline.name) == future_baseline

        # A model added between catalog and category reads is safe when it names an existing baseline.
        remote.publish_model(make_image_record(EXISTING_BASELINE_MODEL, control_baseline.name))
        loader.call_function()
        assert _quote(host, request_headers, EXISTING_BASELINE_MODEL).status_code == 202

        # Force the dangerous ordering: capture the old catalog, then publish a baseline and its
        # model before the category read. The loader's post-read retry must publish them together.
        second_baseline = future_baseline.model_copy(
            update={
                "name": f"{FUTURE_BASELINE}_second",
                "capabilities": BaselineCapabilities(qr_code=True),
                "horde_policy": HordeBaselinePolicy(kudos=8),
            },
        )
        second_model = make_image_record(FUTURE_MODEL, second_baseline.name)
        remote.pause_next_baseline_fetch = True
        refresh_thread = threading.Thread(target=loader.call_function)
        refresh_thread.start()
        assert remote.baseline_captured.wait(timeout=5)
        remote.publish_baseline(second_baseline)
        remote.publish_model(second_model)
        remote.release_baseline_fetch.set()
        refresh_thread.join(timeout=5)
        assert not refresh_thread.is_alive()
        coherent = _quote(host, request_headers, FUTURE_MODEL)
        assert coherent.status_code == 202, coherent.text
        assert loader.baseline_record(second_baseline.name) == second_baseline

        # A complete model-category outage after a policy edit leaves the previous pair serving.
        remote.publish_baseline(
            second_baseline.model_copy(
                update={
                    "capabilities": BaselineCapabilities(qr_code=False),
                    "horde_policy": HordeBaselinePolicy(kudos=12),
                },
            ),
        )
        remote.failed_model_fetches = 10
        loader.call_function()
        during_failure = _quote(host, request_headers, FUTURE_MODEL, qr_code=True)
        assert during_failure.status_code == 202

        loader.call_function()
        assert loader.baseline_record(second_baseline.name).horde_policy.kudos == 12
        recovered = _quote(host, request_headers, FUTURE_MODEL, qr_code=True)
        assert recovered.status_code == 400
        assert recovered.json()["rc"] == "ControlNetMismatch."

        _wait_for_traffic(request_count, 100)
        output, _ = locust_process.communicate(timeout=15)
        assert locust_process.returncode == 0, output
    finally:
        if locust_process is not None and locust_process.poll() is None:
            locust_process.terminate()
            try:
                locust_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                locust_process.kill()
                locust_process.communicate(timeout=5)
        server.shutdown()
        server_thread.join(timeout=5)
        limiter.enabled = previous_limiter
        loader._image_snapshot = previous_snapshot
