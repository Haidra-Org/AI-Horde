# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared helpers used by the AI Horde Locust workloads."""

import random
import time

from locust.exception import RescheduleTask

from .config import _config


def _is_expected_rc(body_json: dict | None, rc_set: set[str]) -> bool:
    return bool(body_json and body_json.get("rc") in rc_set)


def _is_too_many_workers(body_json: dict | None) -> bool:
    """Match the 'untrusted users can only have up to 3 distinct workers' 403.

    Older AI-Horde deployments raise this with the generic rc='Forbidden', so
    we fall back to substring matching on the message body.
    """
    if not body_json:
        return False
    if body_json.get("rc") in ("TooManyWorkers", "TooManyWorkersTrusted"):
        return True
    msg = body_json.get("message") or ""
    return "distinct workers" in msg or "onboard more than 20 workers" in msg


def _safe_json(resp) -> dict | None:
    try:
        return resp.json()
    except Exception:
        return None


def _record_expected(environment, request_type: str, name: str, response_time_ms: float, response_length: int) -> None:
    """Record an \"expected failure\" sample under a *_expected name.

    The original request is already counted as success via ``resp.success()``,
    we just additionally surface the expected-failure rate so it shows up in
    the Locust statistics tab.
    """
    environment.events.request.fire(
        request_type=request_type,
        name=f"{name} [expected]",
        response_time=response_time_ms,
        response_length=response_length,
        exception=None,
        context={},
    )


def _handle_async_generate(resp, environment) -> str | None:
    """Common handling for POST /generate/async responses.

    Returns the request id on success, ``None`` otherwise. 429 (rate limit)
    and selected 403 rcs are treated as expected and trigger a back-off via
    ``RescheduleTask`` so Locust skips ahead to the next task instead of
    hammering the same endpoint.
    """
    name = resp.request_meta.get("name", "/api/v2/generate/async")
    if resp.ok:
        body = _safe_json(resp) or {}
        resp.success()
        return body.get("id")

    body = _safe_json(resp)
    if resp.status_code == 429:
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        # Honour the Retry-After header if present, else jittered back-off.
        retry_after = float(resp.headers.get("Retry-After") or random.uniform(2.0, 6.0))
        time.sleep(min(retry_after, 10.0))
        raise RescheduleTask()
    if resp.status_code == 403 and _is_expected_rc(body, {"WorkerInviteOnly"}):
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        raise RescheduleTask()

    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
    return None


def _parse_csv(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _headers(api_key: str) -> dict[str, str]:
    return {
        "apikey": api_key,
        "Client-Agent": _config.get("client_agent", "aihorde_stress_test:1.0.0:(discord)stress_tester"),
    }


def _random_prompt() -> str:
    words = [
        "landscape",
        "portrait",
        "cyberpunk",
        "medieval",
        "futuristic",
        "underwater",
        "forest",
        "city",
        "desert",
        "mountain",
        "space",
        "robot",
        "dragon",
        "castle",
        "sunset",
        "neon",
        "abstract",
    ]
    return " ".join(random.sample(words, k=random.randint(3, 8)))


def _pick_requestor_key() -> str:
    keys = _config.get("requestor_api_keys", [])
    if not keys:
        return _config.get("anonymous_api_key", "0000000000")
    return random.choice(keys)


def _pick_worker_key() -> str:
    keys = _config.get("worker_api_keys", [])
    if not keys:
        return _config.get("anonymous_api_key", "0000000000")
    return random.choice(keys)
