# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
from io import BytesIO

import pytest
import requests
from horde_sdk.ai_horde_api.apimodels import AlchemyJobPopResponse
from PIL import Image

from tests.integration._object_storage import (
    assert_presigned_image_download,
    make_test_webp,
    upload_to_presigned_url,
)

# The annotation form is image-output: its pop mints an R2 upload URL, so the object store must be
# provisioned for this module rather than depending on a co-running marked module to bring it up.
pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
]


def test_simple_alchemy(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "caption"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["caption", "strip_background", "interrogation"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
    }
    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    job_id = pop_results["forms"][0]["id"]
    assert job_id is not None, pop_results

    submit_dict = {
        "id": job_id,
        "result": {"caption": "Test"},
        "state": "ok",
    }
    submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 1
    gen = retrieve_results["forms"][0]
    assert "result" in gen
    assert isinstance(gen["result"], dict)
    assert "caption" in gen["result"]
    assert gen["form"] == "caption"
    assert gen["result"]["caption"] == "Test"
    assert gen["state"] == "done"
    assert retrieve_results["state"] == "done"


def test_alchemy_vectorize(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "vectorize"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["vectorize"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
    }
    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    job_id = pop_results["forms"][0]["id"]
    assert job_id is not None, pop_results

    submit_dict = {
        "id": job_id,
        "result": {"vectorize": "Test"},
        "state": "ok",
    }
    submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 1
    gen = retrieve_results["forms"][0]
    assert "result" in gen
    assert isinstance(gen["result"], dict)
    assert "vectorize" in gen["result"]
    assert gen["form"] == "vectorize"
    assert gen["result"]["vectorize"] == "Test"
    assert gen["state"] == "done"
    assert retrieve_results["state"] == "done"


def test_alchemy_annotation(client, request_headers: dict[str, str]) -> None:
    source_image = make_test_webp(size=(12, 8), color=(17, 91, 203))
    annotation_image = make_test_webp(size=(12, 8), color=(240, 240, 240))
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "canny"}},
        ],
        "source_image": base64.b64encode(source_image).decode("ascii"),
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["annotation"],
        "annotation_types": ["canny"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
    }
    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()
    sdk_response = AlchemyJobPopResponse.model_validate(pop_results)

    popped_form = pop_results["forms"][0]
    job_id = popped_form["id"]
    assert job_id is not None, pop_results
    assert popped_form["form"] == "annotation"
    # annotation is an image-output form, so the pop must carry an R2 upload destination.
    assert popped_form["r2_upload"], pop_results
    # The parameterized control type must survive round-trip to the worker via the form payload.
    assert popped_form["payload"]["control_type"] == "canny", pop_results
    assert sdk_response.forms is not None
    assert sdk_response.forms[0].payload is not None
    assert sdk_response.forms[0].payload.control_type == "canny"

    # A base64 request source is stored by the API and handed to the worker as a presigned URL.
    # Dereference it as a worker would, rather than merely checking that a URL was minted.
    source_response = requests.get(popped_form["source_image"], timeout=10)
    assert source_response.status_code == 200, source_response.text
    with Image.open(BytesIO(source_response.content)) as popped_source:
        assert popped_source.format == "WEBP"
        assert popped_source.size == (12, 8)

    # Exercise the worker-facing presigned PUT before claiming that the result lives in R2.
    upload_to_presigned_url(popped_form["r2_upload"], annotation_image)

    submit_dict = {
        "id": job_id,
        "result": {"annotation": "R2"},
        "state": "ok",
    }
    submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 1
    gen = retrieve_results["forms"][0]
    assert gen["form"] == "annotation"
    assert gen["state"] == "done"
    assert gen["result"]["annotation"], gen
    # The requester-facing URL must dereference to exactly what the worker uploaded.
    assert_presigned_image_download(gen["result"]["annotation"], annotation_image)
    assert retrieve_results["state"] == "done"


def test_alchemy_annotation_forms_are_distinct_per_detector(client, request_headers: dict[str, str]) -> None:
    """One request may carry an annotation form per detector; status echoes each form's payload.

    Identical name+payload pairs collapse to one form, so a repeated detector is not queued twice.
    """
    source_image = make_test_webp(size=(12, 8), color=(17, 91, 203))
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "canny"}},
            {"name": "annotation", "payload": {"control_type": "depth"}},
            {"name": "annotation", "payload": {"control_type": "canny"}},
            {"name": "caption"},
        ],
        "source_image": base64.b64encode(source_image).decode("ascii"),
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]
    try:
        status = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers).get_json()
        forms = status["forms"]
        assert len(forms) == 3, forms
        annotations = [form for form in forms if form["form"] == "annotation"]
        assert sorted(form["payload"]["control_type"] for form in annotations) == ["canny", "depth"], forms
        caption = next(form for form in forms if form["form"] == "caption")
        assert "payload" not in caption, caption
    finally:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)


def _annotation_reward_for_control_type(client, request_headers: dict[str, str], control_type: str) -> float:
    """Run a full annotation pop->submit cycle for ``control_type`` and return the awarded kudos."""
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": control_type}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop_dict = {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "annotation_types": [control_type],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        }
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
        job_id = pop_req.get_json()["forms"][0]["id"]

        submit_dict = {"id": job_id, "result": {"annotation": "R2"}, "state": "ok"}
        submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
        assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
        return submit_req.get_json()["reward"]
    finally:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)


def test_alchemy_annotation_bucket_prices_by_detector_class(client, request_headers: dict[str, str]) -> None:
    """Pricing uses the detector cost bucket: a heavy hub detector (oneformer) pays more per tile
    than a weightless one (canny) for the same source image."""
    canny_reward = _annotation_reward_for_control_type(client, request_headers, "canny")
    oneformer_reward = _annotation_reward_for_control_type(client, request_headers, "oneformer_ade20k")
    assert canny_reward > 0
    assert oneformer_reward > canny_reward, (canny_reward, oneformer_reward)


def _pop_annotation(client, request_headers: dict[str, str], pop_dict: dict) -> dict:
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "canny"}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
        assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
        return pop_req.get_json()
    finally:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)


def test_alchemy_annotation_non_matching_type_does_not_pop(client, request_headers: dict[str, str]) -> None:
    """A worker advertising annotation types that exclude the job's control_type receives no job."""
    pop_results = _pop_annotation(
        client,
        request_headers,
        {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "annotation_types": ["depth"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
    )
    assert not pop_results.get("forms"), pop_results


def test_alchemy_annotation_absent_types_does_not_pop(client, request_headers: dict[str, str]) -> None:
    """The annotation form is fail-closed: a worker that advertises no annotation types matches no jobs."""
    pop_results = _pop_annotation(
        client,
        request_headers,
        {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
    )
    assert not pop_results.get("forms"), pop_results


def test_annotation_matching_happens_before_the_candidate_limit(app, client, request_headers: dict[str, str]) -> None:
    """One compatible job remains discoverable behind 100 older incompatible annotation jobs."""
    from horde.classes.stable.interrogation import Interrogation, InterrogationForms
    from horde.database import functions as database
    from horde.flask import db

    worker_name = "AnnotationStarvationWorker"
    initial_pop = client.post(
        "/api/v2/interrogate/pop",
        json={
            "name": worker_name,
            "forms": ["annotation"],
            "annotation_types": ["canny"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
        headers=request_headers,
    )
    assert initial_pop.status_code == 200, initial_pop.get_data(as_text=True)

    queued_ids = []
    with app.app_context():
        user = database.find_user_by_api_key(request_headers["apikey"])
        assert user is not None
        for index, control_type in enumerate(["depth"] * 100 + ["canny"]):
            interrogation = Interrogation(
                user=user,
                source_image=f"https://example.invalid/starvation-{index}.webp",
                safe_ip=True,
                image_tiles=1,
            )
            db.session.add(
                InterrogationForms(
                    interrogation=interrogation,
                    name="annotation",
                    payload={"control_type": control_type},
                ),
            )
            queued_ids.append(interrogation.id)
        db.session.commit()

    try:
        pop_req = client.post(
            "/api/v2/interrogate/pop",
            json={
                "name": worker_name,
                "forms": ["annotation"],
                "annotation_types": ["canny"],
                "bridge_agent": request_headers["Client-Agent"],
                "max_tiles": 96,
                "amount": 1,
            },
            headers=request_headers,
        )
        assert pop_req.status_code == 200, pop_req.get_data(as_text=True)
        forms = pop_req.get_json().get("forms", [])
        assert len(forms) == 1, pop_req.get_json()
        assert forms[0]["payload"]["control_type"] == "canny"
    finally:
        with app.app_context():
            db.session.query(Interrogation).filter(Interrogation.id.in_(queued_ids)).delete(synchronize_session=False)
            db.session.commit()


def test_form_query_applies_priority_and_exclusion_filters(app, client, request_headers: dict[str, str], make_api_user) -> None:
    """The priority pass is user-scoped and the general pass cannot repeat its rows."""
    from horde.classes.stable.interrogation import Interrogation, InterrogationForms
    from horde.classes.stable.interrogation_worker import InterrogationWorker
    from horde.database import functions as database
    from horde.flask import db

    worker_name = "AlchemyQueryFilterWorker"
    check_in = client.post(
        "/api/v2/interrogate/pop",
        json={
            "name": worker_name,
            "forms": ["caption"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
        headers=request_headers,
    )
    assert check_in.status_code == 200, check_in.get_data(as_text=True)
    other_user = make_api_user(trusted=True, kudos=1000)

    queued_ids = []
    with app.app_context():
        owner = database.find_user_by_api_key(request_headers["apikey"])
        worker = db.session.query(InterrogationWorker).filter_by(name=worker_name).one()
        assert owner is not None
        for index, user_id in enumerate((owner.id, other_user.id)):
            interrogation = Interrogation(
                user_id=user_id,
                source_image=f"https://example.invalid/query-filter-{index}.webp",
                safe_ip=True,
                image_tiles=1,
            )
            form = InterrogationForms(interrogation=interrogation, name="caption")
            db.session.add(form)
            db.session.flush()
            queued_ids.append(interrogation.id)
        db.session.commit()

        priority_forms = database.get_sorted_forms_filtered_to_worker(
            worker=worker,
            forms_list=["caption"],
            priority_user_ids=[owner.id],
        )
        assert priority_forms
        assert {form.interrogation.user_id for form in priority_forms} == {owner.id}

        general_forms = database.get_sorted_forms_filtered_to_worker(
            worker=worker,
            forms_list=["caption"],
            excluded_forms=priority_forms,
        )
        assert not ({form.id for form in priority_forms} & {form.id for form in general_forms})
        assert any(form.interrogation.user_id == other_user.id for form in general_forms)

        db.session.query(Interrogation).filter(Interrogation.id.in_(queued_ids)).delete(synchronize_session=False)
        db.session.commit()


def test_annotation_filter_preserves_legacy_forms(client, request_headers: dict[str, str]) -> None:
    """Type filtering excludes only incompatible annotations, not legacy alchemy forms."""
    async_req = client.post(
        "/api/v2/interrogate/async",
        json={
            "forms": [
                {"name": "caption"},
                {"name": "annotation", "payload": {"control_type": "depth"}},
            ],
            "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
        },
        headers=request_headers,
    )
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    req_id = async_req.get_json()["id"]

    try:
        pop_req = client.post(
            "/api/v2/interrogate/pop",
            json={
                "name": "MixedAlchemyWorker",
                "forms": ["caption", "annotation"],
                "annotation_types": ["canny"],
                "bridge_agent": request_headers["Client-Agent"],
                "max_tiles": 96,
                "amount": 10,
            },
            headers=request_headers,
        )
        assert pop_req.status_code == 200, pop_req.get_data(as_text=True)
        forms = pop_req.get_json().get("forms", [])
        assert [form["form"] for form in forms] == ["caption"], pop_req.get_json()
    finally:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)


def test_alchemy_annotation_rejects_unknown_control_type(client, request_headers: dict[str, str]) -> None:
    """The server validates control_type against its closed set and rejects anything outside it."""
    async_dict = {
        "forms": [
            {"name": "annotation", "payload": {"control_type": "not_a_real_detector"}},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code == 400, async_req.get_data(as_text=True)


def test_alchemy_annotation_requires_control_type(client, request_headers: dict[str, str]) -> None:
    """An annotation form with no control_type payload is rejected rather than silently defaulted."""
    async_dict = {
        "forms": [
            {"name": "annotation"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code == 400, async_req.get_data(as_text=True)


def test_alchemist_palette_and_describe(client, request_headers: dict[str, str]) -> None:
    async_dict = {
        "forms": [
            {"name": "palette"},
            {"name": "describe"},
        ],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    async_req = client.post("/api/v2/interrogate/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]

    pop_dict = {
        "name": "CICD Fake Alchemist",
        "forms": ["palette", "describe"],
        "bridge_agent": request_headers["Client-Agent"],
        "max_tiles": 96,
        "amount": 2,
    }

    try:
        pop_req = client.post("/api/v2/interrogate/pop", json=pop_dict, headers=request_headers)
    except Exception:
        client.delete(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
        raise

    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    assert len(pop_results["forms"]) == 2, pop_results

    expected_results = {"palette": "Test palette", "describe": "Test Describe"}

    # Submit each form individually with its own job ID
    for form in pop_results["forms"]:
        job_id = form["id"]
        form_name = form["form"]
        assert job_id is not None, pop_results
        submit_dict = {
            "id": job_id,
            "result": {form_name: expected_results[form_name]},
            "state": "ok",
        }
        submit_req = client.post("/api/v2/interrogate/submit", json=submit_dict, headers=request_headers)
        assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
        submit_results = submit_req.get_json()
        assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/interrogate/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["forms"]) == 2
    assert retrieve_results["state"] == "done"

    # Build a dict keyed by form name for order-independent assertions
    forms_by_name = {f["form"]: f for f in retrieve_results["forms"]}
    for form_name, expected_result in expected_results.items():
        form = forms_by_name[form_name]
        assert "result" in form
        assert isinstance(form["result"], dict)
        assert form_name in form["result"]
        assert form["result"][form_name] == expected_result
        assert form["state"] == "done"
