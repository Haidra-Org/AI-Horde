# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Full-round-trip Locust workload driven through image-reference epochs."""

from __future__ import annotations

import json
import os
import random
import string
import time
from pathlib import Path
from typing import Any

from locust import HttpUser, between, task
from locust.exception import StopUser

_CONFIG_PATH = Path(os.environ["LOCUST_REFERENCE_EPOCH_CONFIG"])
_EVIDENCE_PATH = Path(os.environ["LOCUST_REFERENCE_EVIDENCE"])
_API_KEY = os.environ["LOCUST_REFERENCE_API_KEY"]
_REQUESTER_COUNT = int(os.environ.get("LOCUST_REFERENCE_REQUESTERS", "4"))
_WORKER_COUNT = int(os.environ.get("LOCUST_REFERENCE_WORKERS", "3"))
_WORKER_PREFIX = "ReferenceEpochWorker-"


def _headers() -> dict[str, str]:
    return {"apikey": _API_KEY, "Client-Agent": "aihorde_reference_epochs:1:test"}


def _epoch_config() -> dict[str, Any]:
    for _attempt in range(5):
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
    raise RuntimeError(f"Unable to read epoch configuration from {_CONFIG_PATH}")


def _record_evidence(event: str, **fields: Any) -> None:
    record = {"event": event, "time": time.time(), **fields}
    with _EVIDENCE_PATH.open("a", encoding="utf-8") as evidence:
        evidence.write(json.dumps(record, sort_keys=True) + "\n")


def _stop_if_requested(user: HttpUser, config: dict[str, Any]) -> None:
    if not config.get("stop"):
        return
    if user.environment.runner is not None:
        user.environment.runner.quit()
    raise StopUser()


def _request_payload(epoch: str, model: str) -> dict[str, Any]:
    return {
        "prompt": f"reference epoch={epoch} model={model} nonce={random.randint(0, 10**12)}",
        "models": [model],
        "params": {
            "width": 512,
            "height": 512,
            "steps": 8,
            "cfg_scale": 7.5,
            "sampler_name": "k_euler_a",
        },
        "nsfw": False,
        "r2": True,
        "trusted_workers": False,
    }


class ReferenceEpochRequester(HttpUser):
    """Submit epoch-selected models and verify the worker result reaches requester status."""

    fixed_count = _REQUESTER_COUNT
    wait_time = between(0.02, 0.08)

    def on_start(self) -> None:
        self.pending: dict[str, tuple[str, str]] = {}
        self.observed_epoch = ""
        self.model_cursor = 0

    def _cancel(self, request_id: str) -> None:
        with self.client.delete(
            f"/api/v2/generate/status/{request_id}",
            headers=_headers(),
            catch_response=True,
            name="/api/v2/generate/status/[id] [reference-epoch-cancel]",
        ) as response:
            if response.status_code in {200, 404, 410}:
                response.success()
            else:
                response.failure(f"status={response.status_code} body={response.text[:200]}")

    def _sync_epoch(self, config: dict[str, Any]) -> None:
        epoch = str(config["epoch"])
        if epoch == self.observed_epoch:
            return
        active_models = set(config["request_models"])
        # Keep compatible work alive across the boundary. The resulting overlap is
        # deliberate: reference publication must remain coherent while requesters poll
        # old work, workers drain it, and new-epoch traffic starts using the same models.
        for request_id, (_submitted_epoch, model) in list(self.pending.items()):
            if model not in active_models:
                self._cancel(request_id)
                self.pending.pop(request_id, None)
        self.observed_epoch = epoch

    def on_stop(self) -> None:
        for request_id in list(self.pending):
            self._cancel(request_id)
            self.pending.pop(request_id, None)

    @task(5)
    def submit_epoch_requests(self) -> None:
        config = _epoch_config()
        _stop_if_requested(self, config)
        self._sync_epoch(config)
        max_pending = int(config.get("max_pending", 12))
        if len(self.pending) >= max_pending or random.random() > float(config.get("submit_probability", 1.0)):
            return

        epoch = str(config["epoch"])
        models = list(config["request_models"])
        for _ in range(int(config.get("submission_batch", 1))):
            if len(self.pending) >= max_pending:
                break
            # Per-model completion is a correctness oracle, not a statistical one. Cycle
            # through the epoch's models so every requester supplies balanced work while
            # task selection, polling, generation latency, and HTTP scheduling stay random.
            model = models[self.model_cursor % len(models)]
            with self.client.post(
                "/api/v2/generate/async",
                json=_request_payload(epoch, model),
                headers=_headers(),
                catch_response=True,
                name=f"/api/v2/generate/async [reference-epoch/{epoch}]",
            ) as response:
                body = response.json() if response.content else {}
                if response.status_code == 429:
                    response.success()
                    _record_evidence("backpressure", epoch=epoch, operation="request_submit")
                    time.sleep(0.1)
                    continue
                if response.status_code != 202 or not body.get("id"):
                    response.failure(f"epoch={epoch} model={model}: status={response.status_code} body={body}")
                    continue
                response.success()
                request_id = str(body["id"])
                self.pending[request_id] = (epoch, model)
                self.model_cursor += 1
                _record_evidence("request_submitted", epoch=epoch, model=model, request_id=request_id)

    @task(8)
    def poll_epoch_requests(self) -> None:
        config = _epoch_config()
        _stop_if_requested(self, config)
        self._sync_epoch(config)
        if not self.pending:
            return

        request_id = random.choice(list(self.pending))
        epoch, model = self.pending[request_id]
        with self.client.get(
            f"/api/v2/generate/check/{request_id}",
            headers=_headers(),
            catch_response=True,
            name="/api/v2/generate/check/[id] [reference-epoch]",
        ) as response:
            body = response.json() if response.content else {}
            if response.status_code == 429:
                response.success()
                _record_evidence("backpressure", epoch=epoch, operation="request_poll")
                time.sleep(0.1)
                return
            if response.status_code in {404, 410}:
                self.pending.pop(request_id, None)
                response.failure(f"epoch={epoch} model={model}: request disappeared before completion")
                return
            if response.status_code != 200:
                response.failure(f"epoch={epoch} model={model}: status={response.status_code} body={body}")
                return
            response.success()
            if not body.get("done"):
                return

        with self.client.get(
            f"/api/v2/generate/status/{request_id}",
            headers=_headers(),
            catch_response=True,
            name="/api/v2/generate/status/[id] [reference-epoch-complete]",
        ) as response:
            body = response.json() if response.content else {}
            generations = body.get("generations", [])
            valid = (
                response.status_code == 200
                and body.get("done") is True
                and len(generations) == 1
                and generations[0].get("model") == model
                and generations[0].get("state") == "ok"
                and str(generations[0].get("worker_name", "")).startswith(_WORKER_PREFIX)
            )
            if not valid:
                response.failure(f"epoch={epoch} model={model}: incomplete round trip body={body}")
                return
            response.success()
            self.pending.pop(request_id, None)
            _record_evidence(
                "request_completed",
                epoch=epoch,
                model=model,
                request_id=request_id,
                worker_name=generations[0]["worker_name"],
            )


class ReferenceEpochWorker(HttpUser):
    """Pop epoch-selected models and submit a fake generation for requester verification."""

    fixed_count = _WORKER_COUNT
    wait_time = between(0.01, 0.05)

    def on_start(self) -> None:
        suffix = "".join(random.choices(string.ascii_lowercase, k=8))
        self.worker_name = f"{_WORKER_PREFIX}{suffix}"

    @task
    def pop_and_submit(self) -> None:
        config = _epoch_config()
        _stop_if_requested(self, config)
        epoch = str(config["epoch"])
        worker_models = list(config["worker_models"])
        with self.client.post(
            "/api/v2/generate/pop",
            json={
                "name": self.worker_name,
                "models": worker_models,
                "bridge_agent": "AI Horde Worker reGen:17.0.0-reference-epochs:https://github.com/Haidra-Org/horde-worker-reGen",
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
            headers=_headers(),
            catch_response=True,
            name=f"/api/v2/generate/pop [reference-epoch/{epoch}]",
        ) as response:
            body = response.json() if response.content else {}
            if response.status_code == 429:
                response.success()
                _record_evidence("backpressure", epoch=epoch, operation="worker_pop")
                time.sleep(0.1)
                return
            job_id = body.get("id")
            if response.status_code != 200:
                response.failure(f"epoch={epoch}: status={response.status_code} body={body}")
                return
            if not job_id:
                response.success()
                return
            if body.get("model") not in worker_models:
                response.failure(f"epoch={epoch}: popped unadvertised model body={body}")
                return
            if type(body.get("ttl")) is not int or body["ttl"] <= 0:
                response.failure(f"epoch={epoch}: popped invalid ttl body={body}")
                return
            response.success()

        delay_ms = float(config.get("generation_delay_ms", 0))
        if delay_ms:
            time.sleep(random.uniform(delay_ms * 0.5, delay_ms * 1.5) / 1000)
        with self.client.post(
            "/api/v2/generate/submit",
            json={"id": job_id, "generation": "R2", "state": "ok", "seed": random.randint(0, 10**9)},
            headers=_headers(),
            catch_response=True,
            name="/api/v2/generate/submit [reference-epoch]",
        ) as response:
            body = response.json() if response.content else {}
            if response.status_code == 200 and body.get("reward", 0) > 0:
                response.success()
                _record_evidence(
                    "worker_submitted",
                    epoch=epoch,
                    job_id=job_id,
                    worker_name=self.worker_name,
                )
            elif response.status_code == 404 or body.get("rc") in {"InvalidJobID", "InvalidProcGen"}:
                # An epoch transition may cancel a request after it was popped.
                response.success()
            else:
                response.failure(f"epoch={epoch}: status={response.status_code} body={body}")
