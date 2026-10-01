# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dispatch gating for image requests that ask for post-processing.

A worker that disallows post-processing must not be offered such jobs, and its
pop response must report them under the ``post-processing`` skip reason. The
request stores the field as ``params["post_processing"]``; the hyphenated
spelling is the bridge capability and skip-reason name only.

Clients may send an empty ``post_processing`` list. Such a request needs no
post-processor, so it stays eligible for every worker.
"""

import pytest

from horde.bridge_reference import CAPABILITY_EXPANDED_REGEN_VERSION

TEST_MODELS = ["stable_diffusion"]

WORKER_NAME = "CICD Fake Post Processing Dreamer"

# A reGen agent whose bridge maps every known post-processor, so only the worker opt-out decides.
BRIDGE_AGENT = f"AI Horde Worker reGen:{CAPABILITY_EXPANDED_REGEN_VERSION}:https://github.com/Haidra-Org/horde-worker-reGen"

MAX_CANDIDATE_PAGES = 50
"""Upper bound on candidate pages read when listing a worker's queue, so a runaway queue fails the test instead of hanging it."""

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def _async_dict(post_processing: list[str]) -> dict:
    return {
        "prompt": "a horde of robots restoring old photographs",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 512,
            "height": 512,
            "steps": 20,
            "cfg_scale": 7.5,
            "sampler_name": "k_euler_a",
            "post_processing": post_processing,
        },
        "models": TEST_MODELS,
    }


def _pop_dict(allow_post_processing: bool) -> dict:
    return {
        "name": WORKER_NAME,
        "models": TEST_MODELS,
        "bridge_agent": BRIDGE_AGENT,
        "nsfw": True,
        "amount": 10,
        "max_pixels": 4194304,
        "allow_img2img": True,
        "allow_painting": True,
        "allow_unsafe_ipaddr": True,
        "allow_post_processing": allow_post_processing,
        "allow_lora": True,
    }


def _queue_request(client, request_headers: dict[str, str], post_processing: list[str]) -> str:
    async_req = client.post("/api/v2/generate/async", json=_async_dict(post_processing), headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    return async_req.get_json()["id"]


def _candidate_ids_for_worker(app) -> set[str]:
    from horde.classes.stable.worker import ImageWorker
    from horde.database import functions as database
    from horde.flask import db

    candidate_ids: set[str] = set()
    with app.app_context():
        worker = db.session.query(ImageWorker).filter_by(name=WORKER_NAME).one()
        for page in range(MAX_CANDIDATE_PAGES):
            wp_list = database.get_sorted_wp_filtered_to_worker(worker, TEST_MODELS, [], page=page)
            if not wp_list:
                break
            candidate_ids.update(str(wp.id) for wp in wp_list)
        else:
            pytest.fail(f"worker candidate list exceeded {MAX_CANDIDATE_PAGES} pages")
        # The candidate query locks the rows it returns.
        db.session.rollback()
    return candidate_ids


def test_post_processing_job_is_reported_skipped_for_worker_that_disallows_it(
    client,
    request_headers: dict[str, str],
) -> None:
    req_id = _queue_request(client, request_headers, ["GFPGAN"])
    try:
        pop = client.post(
            "/api/v2/generate/pop",
            json=_pop_dict(allow_post_processing=False),
            headers=request_headers,
        )
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is None, results
        assert results["skipped"].get("post-processing", 0) >= 1, results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_post_processing_job_is_not_a_candidate_for_worker_that_disallows_it(
    app,
    client,
    request_headers: dict[str, str],
) -> None:
    # The candidate query must exclude the job itself. Leaving the rejection to the per-candidate
    # check still withholds the job, but it fills candidate pages and locks rows the worker cannot use.
    check_in = client.post(
        "/api/v2/generate/pop",
        json=_pop_dict(allow_post_processing=False),
        headers=request_headers,
    )
    assert check_in.status_code < 400, check_in.get_data(as_text=True)

    req_id = _queue_request(client, request_headers, ["GFPGAN"])
    try:
        assert req_id not in _candidate_ids_for_worker(app)
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_post_processing_job_matches_worker_that_allows_it(client, request_headers: dict[str, str]) -> None:
    req_id = _queue_request(client, request_headers, ["GFPGAN"])
    try:
        pop = client.post(
            "/api/v2/generate/pop",
            json=_pop_dict(allow_post_processing=True),
            headers=request_headers,
        )
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
        assert results["payload"]["post_processing"] == ["GFPGAN"], results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)


def test_empty_post_processing_list_matches_worker_that_disallows_it(
    client,
    request_headers: dict[str, str],
) -> None:
    req_id = _queue_request(client, request_headers, [])
    try:
        pop = client.post(
            "/api/v2/generate/pop",
            json=_pop_dict(allow_post_processing=False),
            headers=request_headers,
        )
        assert pop.status_code < 400, pop.get_data(as_text=True)
        results = pop.get_json()
        assert results["id"] is not None, results
    finally:
        client.delete(f"/api/v2/generate/status/{req_id}", headers=request_headers)
