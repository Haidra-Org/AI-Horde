# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

TEST_MODELS = ["elinas/chronos-70b-v2"]


class _MetricRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, str]]] = []

    def record(self, value: float, attributes: dict[str, str]) -> None:
        self.calls.append((value, attributes))

    def add(self, value: float, attributes: dict[str, str]) -> None:
        self.calls.append((value, attributes))


def _run_text_gen_round_trip(
    client,
    request_headers: dict[str, str],
    *,
    submit_state: str = "ok",
    gen_metadata: list[dict[str, str]] | None = None,
    create_shadow_forecast: bool = False,
) -> dict[str, object]:
    """Helper: async → pop → submit → retrieve a single text generation.

    Returns the retrieved generation dict so callers can assert on specific fields.
    """
    async_dict = {
        "prompt": "a horde of cute stable robots in a sprawling server room repairing a massive mainframe",
        "trusted_workers": True,
        "validated_backends": False,
        "max_length": 512,
        "max_context_length": 2048,
        "temperature": 1,
        "models": TEST_MODELS,
    }
    async_req = client.post("/api/v2/generate/text/async", json=async_dict, headers=request_headers)
    assert async_req.status_code < 400, async_req.get_data(as_text=True)
    async_results = async_req.get_json()
    req_id = async_results["id"]
    if create_shadow_forecast:
        from horde.database import functions
        from horde.database.threads import store_prioritized_wp_queue

        store_prioritized_wp_queue()
        functions._wp_queue_positions_cache["text"] = {}
        functions._wp_queue_positions_time["text"] = 0
        forecast_response = client.get(f"/api/v2/generate/text/status/{req_id}", headers=request_headers)
        assert forecast_response.status_code == 200

    pop_dict = {
        "name": "CICD Fake Scribe",
        "models": ["elinas/chronos-70b-v2"],
        "bridge_agent": request_headers["Client-Agent"],
        "amount": 10,
        "max_context_length": 4096,
        "max_length": 512,
    }
    pop_req = client.post("/api/v2/generate/text/pop", json=pop_dict, headers=request_headers)
    assert pop_req.status_code < 400, pop_req.get_data(as_text=True)
    pop_results = pop_req.get_json()

    job_id = pop_results["id"]
    try:
        assert job_id is not None, pop_results
    except AssertionError as err:
        client.delete(f"/api/v2/generate/text/status/{req_id}", headers=request_headers)
        print("Request cancelled")
        raise err

    submit_dict: dict[str, object] = {
        "id": job_id,
        "generation": "test ",
        "state": submit_state,
        "seed": 0,
    }
    if gen_metadata is not None:
        submit_dict["gen_metadata"] = gen_metadata

    submit_req = client.post("/api/v2/generate/text/submit", json=submit_dict, headers=request_headers)
    assert submit_req.status_code < 400, submit_req.get_data(as_text=True)
    submit_results = submit_req.get_json()
    assert submit_results["reward"] > 0

    retrieve_req = client.get(f"/api/v2/generate/text/status/{req_id}", headers=request_headers)
    assert retrieve_req.status_code < 400, retrieve_req.get_data(as_text=True)
    retrieve_results = retrieve_req.get_json()

    assert len(retrieve_results["generations"]) == 1
    gen = retrieve_results["generations"][0]
    assert gen["worker_name"] == "CICD Fake Scribe"
    assert gen["model"] in TEST_MODELS
    assert retrieve_results["kudos"] > 1
    assert retrieve_results["done"] is True
    return gen


def test_simple_text_gen(client, request_headers: dict[str, str]) -> None:
    gen = _run_text_gen_round_trip(client, request_headers, submit_state="ok")
    assert len(gen["gen_metadata"]) == 0
    assert gen["state"] == "ok"


def test_text_round_trip_records_request_lifecycle_metrics(client, request_headers: dict[str, str], monkeypatch) -> None:
    from horde import metrics
    from horde.classes.base import waiting_prompt as waiting_prompt_module

    start_recorder = _MetricRecorder()
    completion_recorder = _MetricRecorder()
    outcome_recorder = _MetricRecorder()
    estimate_validations = []
    stall_validations = []
    monkeypatch.setattr(waiting_prompt_module, "request_time_to_first_start", start_recorder)
    monkeypatch.setattr(metrics, "request_time_to_completion", completion_recorder)
    monkeypatch.setattr(metrics, "request_outcomes", outcome_recorder)
    monkeypatch.setattr(
        metrics,
        "record_request_estimate_validation",
        lambda **kwargs: estimate_validations.append(kwargs),
    )
    monkeypatch.setattr(
        metrics,
        "record_request_stall_validation",
        lambda **kwargs: stall_validations.append(kwargs),
    )

    _run_text_gen_round_trip(
        client,
        request_headers,
        submit_state="ok",
        create_shadow_forecast=True,
    )
    from horde.request_scheduling import wait_for_scheduling_forecast_events

    assert wait_for_scheduling_forecast_events()

    assert len(start_recorder.calls) == 1
    assert start_recorder.calls[0][1] == {"horde.gentype": "text"}
    assert len(completion_recorder.calls) == 1
    assert completion_recorder.calls[0][1] == {"horde.gentype": "text"}
    assert outcome_recorder.calls == [(1, {"horde.gentype": "text", "horde.outcome": "completed"})]
    assert [validation["phase"] for validation in estimate_validations] == ["start", "completion"]
    assert all(validation["estimator"] == "compatible-queue-v1" for validation in estimate_validations)
    assert len(stall_validations) == 1
    assert stall_validations[0]["expired_without_start"] is False


def test_censored_text_gen_round_trip(client, request_headers: dict[str, str]) -> None:
    """Validates that a worker submitting with state='censored' is reflected in the client status."""
    gen = _run_text_gen_round_trip(client, request_headers, submit_state="censored")
    assert gen["state"] == "censored", f"Expected state='censored', got {gen.get('state')}"


def test_csam_text_gen_round_trip_via_state(client, request_headers: dict[str, str]) -> None:
    """Validates that a worker submitting with state='csam' is reflected in the client status."""
    gen = _run_text_gen_round_trip(client, request_headers, submit_state="csam")
    assert gen["state"] == "csam", f"Expected state='csam', got {gen.get('state')}"


def test_csam_text_gen_round_trip_via_metadata(client, request_headers: dict[str, str]) -> None:
    """Validates that csam reported via gen_metadata is reflected as state='csam'."""
    gen = _run_text_gen_round_trip(
        client,
        request_headers,
        submit_state="ok",
        gen_metadata=[{"type": "censorship", "value": "csam"}],
    )
    assert gen["state"] == "csam", f"Expected state='csam', got {gen.get('state')}"
