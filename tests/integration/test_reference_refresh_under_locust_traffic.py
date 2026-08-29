# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise remote image-reference publication boundaries under concurrent request traffic."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
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

pytestmark = [pytest.mark.integration, pytest.mark.object_storage]

CONTROL_MODEL = "reference-churn-control"
EXISTING_BASELINE_MODEL = "reference-churn-existing-baseline"
FUTURE_MODEL = "reference-churn-future-model"
HELD_MODEL = "reference-churn-held-model"
FUTURE_BASELINE = "reference_churn_future_baseline"
DIRECT_WORKER = "ReferenceEpochWorker-held-contract"
EXPECTED_OLD_POLICY_TTL = 184
"""(30 fixed + 8 sampler work units * 2) * the epoch's explicit ttl multiplier of 4."""
EXPECTED_NEW_POLICY_TTL = 150
"""The standard minimum lease after the replacement policy returns to the default ttl multiplier."""

HTTP_SESSION = requests.Session()


class _StubTextResponse:
    @staticmethod
    def json() -> dict[str, Any]:
        return {}


class _QueryResult:
    def __init__(self, records: list[ImageGenerationModelRecord]) -> None:
        self._records = records

    def to_list(self) -> list[ImageGenerationModelRecord]:
        return self._records


@dataclass(frozen=True)
class _PoppedRequest:
    request_id: str
    job_id: str
    model: str
    ttl: int


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

    def remove_model(self, model_name: str) -> None:
        with self.lock:
            self.models.pop(model_name, None)


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
    deadline = time.monotonic() + 5
    while True:
        response = HTTP_SESSION.post(
            f"{host}/api/v2/generate/async",
            json=payload,
            headers=headers,
            timeout=5,
        )
        if response.status_code != 429 or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if response.status_code == 202:
        HTTP_SESSION.delete(
            f"{host}/api/v2/generate/status/{response.json()['id']}",
            headers=headers,
            timeout=5,
        )
    return response


def _pop_direct_request(
    host: str,
    headers: dict[str, str],
    model: str,
    *,
    expected_ttl: int,
) -> _PoppedRequest:
    request_response = HTTP_SESSION.post(
        f"{host}/api/v2/generate/async",
        json=_payload(model),
        headers=headers,
        timeout=5,
    )
    assert request_response.status_code == 202, request_response.text
    request_id = request_response.json()["id"]
    pop_response = HTTP_SESSION.post(
        f"{host}/api/v2/generate/pop",
        json={
            "name": DIRECT_WORKER,
            "models": [model],
            "bridge_agent": "AI Horde Worker reGen:17.0.0-held-contract:https://github.com/Haidra-Org/horde-worker-reGen",
            "nsfw": True,
            "amount": 1,
            "max_pixels": 4194304,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_extended_controlnet": True,
            "allow_lora": True,
        },
        headers=headers,
        timeout=5,
    )
    assert pop_response.status_code == 200, pop_response.text
    body = pop_response.json()
    assert body["model"] == model
    assert body["ttl"] == expected_ttl
    return _PoppedRequest(request_id=request_id, job_id=body["id"], model=model, ttl=body["ttl"])


def _complete_direct_request(
    host: str,
    headers: dict[str, str],
    popped: _PoppedRequest,
    *,
    seed: int,
) -> float:
    submit_response = HTTP_SESSION.post(
        f"{host}/api/v2/generate/submit",
        json={"id": popped.job_id, "generation": "R2", "state": "ok", "seed": seed},
        headers=headers,
        timeout=5,
    )
    assert submit_response.status_code == 200, submit_response.text
    assert submit_response.json()["reward"] > 0
    status_response = HTTP_SESSION.get(
        f"{host}/api/v2/generate/status/{popped.request_id}",
        headers=headers,
        timeout=5,
    )
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()
    assert status["done"] is True
    assert status["faulted"] is False
    assert len(status["generations"]) == 1
    generation = status["generations"][0]
    assert generation["model"] == popped.model
    assert generation["worker_name"] == DIRECT_WORKER
    assert generation["seed"] == str(seed)
    assert generation["state"] == "ok"
    return float(submit_response.json()["reward"])


def _write_epoch(
    config_path: Path,
    *,
    epoch: str,
    request_models: tuple[str, ...],
    worker_models: tuple[str, ...],
    submission_batch: int,
    submit_probability: float,
    generation_delay_ms: int,
    stop: bool = False,
) -> None:
    config = {
        "epoch": epoch,
        "request_models": request_models,
        "worker_models": worker_models,
        "submission_batch": submission_batch,
        "submit_probability": submit_probability,
        "generation_delay_ms": generation_delay_ms,
        "max_pending": 16,
        "stop": stop,
    }
    temporary_path = config_path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(config), encoding="utf-8")
    temporary_path.replace(config_path)


def _read_evidence(evidence_path: Path) -> list[dict[str, Any]]:
    if not evidence_path.exists():
        return []
    return [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines() if line]


def _wait_for_completions(
    evidence_path: Path,
    process: subprocess.Popen[str],
    *,
    epoch: str,
    expected: dict[str, int],
    timeout: float = 60,
) -> Counter[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise AssertionError(f"Locust exited during epoch {epoch} with {process.returncode}:\n{output}")
        completions = Counter(
            str(record["model"])
            for record in _read_evidence(evidence_path)
            if record.get("event") == "request_completed" and record.get("epoch") == epoch
        )
        if all(completions[model] >= count for model, count in expected.items()):
            return completions
        time.sleep(0.05)
    raise AssertionError(f"Epoch {epoch} completion deficit: expected={expected}, actual={dict(completions)}")


def _locust_totals(csv_prefix: Path) -> tuple[int, int]:
    with csv_prefix.with_name(f"{csv_prefix.name}_stats.csv").open(encoding="utf-8", newline="") as stats_file:
        aggregate = next(row for row in csv.DictReader(stats_file) if row["Name"] == "Aggregated")
    return int(aggregate["Request Count"]), int(aggregate["Failure Count"])


def test_remote_reference_churn_stays_coherent_under_locust_traffic(
    monkeypatch: pytest.MonkeyPatch,
    app,
    object_store_ready: None,
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

    server = make_server("127.0.0.1", 0, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    host = f"http://127.0.0.1:{server.server_port}"
    locustfile = Path(__file__).parents[1] / "stress" / "locustfile_reference_churn.py"
    epoch_config_path = tmp_path / "reference-epoch.json"
    evidence_path = tmp_path / "reference-evidence.jsonl"
    _write_epoch(
        epoch_config_path,
        epoch="control-low",
        request_models=(CONTROL_MODEL,),
        worker_models=(CONTROL_MODEL,),
        submission_batch=1,
        submit_probability=0.4,
        generation_delay_ms=10,
    )
    child_env = os.environ.copy()
    child_env.update(
        {
            "LOCUST_REFERENCE_API_KEY": request_headers["apikey"],
            "LOCUST_REFERENCE_EPOCH_CONFIG": str(epoch_config_path),
            "LOCUST_REFERENCE_EVIDENCE": str(evidence_path),
            "LOCUST_REFERENCE_REQUESTERS": "4",
            "LOCUST_REFERENCE_WORKERS": "3",
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
                "7",
                "--spawn-rate",
                "7",
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
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="control-low",
            expected={CONTROL_MODEL: 3},
        )

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
        _write_epoch(
            epoch_config_path,
            epoch="baseline-only-low",
            request_models=(CONTROL_MODEL,),
            worker_models=(CONTROL_MODEL,),
            submission_batch=1,
            submit_probability=0.6,
            generation_delay_ms=20,
        )
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="baseline-only-low",
            expected={CONTROL_MODEL: 3},
        )

        # A model added between catalog and category reads is safe when it names an existing baseline.
        remote.publish_model(make_image_record(EXISTING_BASELINE_MODEL, control_baseline.name))
        loader.call_function()
        assert _quote(host, request_headers, EXISTING_BASELINE_MODEL).status_code == 202
        _write_epoch(
            epoch_config_path,
            epoch="existing-model-medium",
            request_models=(CONTROL_MODEL, EXISTING_BASELINE_MODEL),
            worker_models=(CONTROL_MODEL, EXISTING_BASELINE_MODEL),
            submission_batch=2,
            submit_probability=0.9,
            generation_delay_ms=35,
        )
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="existing-model-medium",
            expected={CONTROL_MODEL: 3, EXISTING_BASELINE_MODEL: 3},
        )

        # Force the dangerous ordering: capture the old catalog, then publish a baseline and its
        # model before the category read. The loader's post-read retry must publish them together.
        second_baseline = future_baseline.model_copy(
            update={
                "name": f"{FUTURE_BASELINE}_second",
                "capabilities": BaselineCapabilities(qr_code=True),
                "horde_policy": HordeBaselinePolicy(kudos=8, ttl=4),
            },
        )
        second_model = make_image_record(FUTURE_MODEL, second_baseline.name)
        remote.pause_next_baseline_fetch = True
        refresh_thread = threading.Thread(target=loader.call_function)
        refresh_thread.start()
        assert remote.baseline_captured.wait(timeout=5)
        remote.publish_baseline(second_baseline)
        remote.publish_model(second_model)
        remote.publish_model(make_image_record(HELD_MODEL, second_baseline.name))
        remote.release_baseline_fetch.set()
        refresh_thread.join(timeout=5)
        assert not refresh_thread.is_alive()
        coherent = _quote(host, request_headers, FUTURE_MODEL)
        assert coherent.status_code == 202, coherent.text
        assert loader.baseline_record(second_baseline.name) == second_baseline
        _complete_direct_request(
            host,
            request_headers,
            _pop_direct_request(
                host,
                request_headers,
                HELD_MODEL,
                expected_ttl=EXPECTED_OLD_POLICY_TTL,
            ),
            seed=101,
        )
        held_across_policy_change = _pop_direct_request(
            host,
            request_headers,
            HELD_MODEL,
            expected_ttl=EXPECTED_OLD_POLICY_TTL,
        )
        _write_epoch(
            epoch_config_path,
            epoch="interleaved-model-burst",
            request_models=(CONTROL_MODEL, EXISTING_BASELINE_MODEL, FUTURE_MODEL),
            worker_models=(CONTROL_MODEL, EXISTING_BASELINE_MODEL, FUTURE_MODEL),
            submission_batch=4,
            submit_probability=1.0,
            generation_delay_ms=100,
        )
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="interleaved-model-burst",
            expected={CONTROL_MODEL: 4, EXISTING_BASELINE_MODEL: 4, FUTURE_MODEL: 4},
        )

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
        _write_epoch(
            epoch_config_path,
            epoch="remote-outage-heavy",
            request_models=(CONTROL_MODEL, EXISTING_BASELINE_MODEL, FUTURE_MODEL),
            worker_models=(CONTROL_MODEL, EXISTING_BASELINE_MODEL, FUTURE_MODEL),
            submission_batch=3,
            submit_probability=1.0,
            generation_delay_ms=60,
        )
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="remote-outage-heavy",
            expected={CONTROL_MODEL: 3, EXISTING_BASELINE_MODEL: 3, FUTURE_MODEL: 3},
        )

        loader.call_function()
        assert loader.baseline_record(second_baseline.name).horde_policy.kudos == 12
        recovered = _quote(host, request_headers, FUTURE_MODEL, qr_code=True)
        assert recovered.status_code == 400
        assert recovered.json()["rc"] == "ControlNetMismatch."
        _complete_direct_request(
            host,
            request_headers,
            held_across_policy_change,
            seed=202,
        )
        _write_epoch(
            epoch_config_path,
            epoch="policy-recovery-medium",
            request_models=(CONTROL_MODEL, FUTURE_MODEL),
            worker_models=(CONTROL_MODEL, FUTURE_MODEL),
            submission_batch=2,
            submit_probability=0.8,
            generation_delay_ms=30,
        )
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="policy-recovery-medium",
            expected={CONTROL_MODEL: 3, FUTURE_MODEL: 3},
        )

        _complete_direct_request(
            host,
            request_headers,
            _pop_direct_request(
                host,
                request_headers,
                HELD_MODEL,
                expected_ttl=EXPECTED_NEW_POLICY_TTL,
            ),
            seed=303,
        )
        held_across_model_removal = _pop_direct_request(
            host,
            request_headers,
            HELD_MODEL,
            expected_ttl=EXPECTED_NEW_POLICY_TTL,
        )
        remote.remove_model(HELD_MODEL)
        loader.call_function()
        assert HELD_MODEL not in (loader.reference or {})
        _complete_direct_request(
            host,
            request_headers,
            held_across_model_removal,
            seed=404,
        )

        remote.remove_model(EXISTING_BASELINE_MODEL)
        loader.call_function()
        assert EXISTING_BASELINE_MODEL not in (loader.reference or {})
        _write_epoch(
            epoch_config_path,
            epoch="model-retired-cooldown",
            request_models=(CONTROL_MODEL, FUTURE_MODEL),
            worker_models=(CONTROL_MODEL, FUTURE_MODEL),
            submission_batch=1,
            submit_probability=0.5,
            generation_delay_ms=10,
        )
        _wait_for_completions(
            evidence_path,
            locust_process,
            epoch="model-retired-cooldown",
            expected={CONTROL_MODEL: 3, FUTURE_MODEL: 3},
        )

        _write_epoch(
            epoch_config_path,
            epoch="stop",
            request_models=(CONTROL_MODEL,),
            worker_models=(CONTROL_MODEL,),
            submission_batch=1,
            submit_probability=0,
            generation_delay_ms=0,
            stop=True,
        )
        time.sleep(0.5)
        if locust_process.poll() is None:
            locust_process.terminate()
        output, _ = locust_process.communicate(timeout=10)
        request_total, failure_total = _locust_totals(tmp_path / "reference-churn")
        assert failure_total == 0, output
        assert request_total >= 500
        completed = [record for record in _read_evidence(evidence_path) if record.get("event") == "request_completed"]
        assert len(completed) >= 45
        assert {record["epoch"] for record in completed} == {
            "control-low",
            "baseline-only-low",
            "existing-model-medium",
            "interleaved-model-burst",
            "remote-outage-heavy",
            "policy-recovery-medium",
            "model-retired-cooldown",
        }
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
