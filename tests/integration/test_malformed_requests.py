# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adversarial request sweep across high-traffic endpoints.

The contract under test: malformed, out-of-range, or hostile input must produce
a *client* error (4xx), never an unhandled 500. A 500 here is a real bug (an
exception escaping to the WSGI layer), so these tests are written to surface
them rather than to assert tidy happy-path behaviour.

Each case is labelled and parametrized so a failure points at the exact input.
"""

from __future__ import annotations

import pytest

AGENT = "aihorde_ci_client:1.0:(test)ci"
SERVER_ERROR = 500


@pytest.fixture(autouse=True)
def _no_rate_limit():
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
def auth(api_key):
    return {"apikey": api_key, "Client-Agent": AGENT}


# Each case: (id, method, path, body_kind, body, expect)
#   body_kind: "json" | "raw" | "none"
#   expect: "4xx" (must be a client error) | "not500" (just must not crash)
def _cases():
    return [
        # ---- image async: schema + custom field handling -----------------
        ("img_empty_body", "POST", "/api/v2/generate/async", "json", {}, "4xx"),
        (
            "img_empty_prompt",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512}},
            "4xx",
        ),
        (
            "img_width_string",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "x", "models": ["stable_diffusion"], "params": {"width": "wide", "height": 512}},
            "4xx",
        ),
        (
            "img_width_negative",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "x", "models": ["stable_diffusion"], "params": {"width": -512, "height": 512}},
            "4xx",
        ),
        (
            "img_steps_huge",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "x", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512, "steps": 100000}},
            "4xx",
        ),
        (
            "img_n_huge",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "x", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512, "n": 100000}},
            "4xx",
        ),
        (
            "img_models_not_list",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "x", "models": "stable_diffusion", "params": {"width": 512, "height": 512}},
            "4xx",
        ),
        (
            "img_bad_base64_source",
            "POST",
            "/api/v2/generate/async",
            "json",
            {
                "prompt": "x",
                "models": ["stable_diffusion"],
                "source_image": "!!!not base64!!!",
                "source_processing": "img2img",
                "params": {"width": 512, "height": 512},
            },
            "not500",
        ),
        (
            "img_loras_wrong_type",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "x", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512, "loras": "notalist"}},
            "not500",
        ),
        (
            "img_huge_prompt",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "a " * 50000, "models": ["stable_diffusion"], "params": {"width": 512, "height": 512}},
            "not500",
        ),
        (
            "img_nullbyte_prompt",
            "POST",
            "/api/v2/generate/async",
            "json",
            {"prompt": "a\x00b", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512}},
            "4xx",
        ),
        ("img_malformed_json", "POST", "/api/v2/generate/async", "raw", "{not valid json", "4xx"),
        # ---- text async --------------------------------------------------
        ("txt_empty_body", "POST", "/api/v2/generate/text/async", "json", {}, "4xx"),
        (
            "txt_maxlen_string",
            "POST",
            "/api/v2/generate/text/async",
            "json",
            {"prompt": "x", "models": ["elinas/chronos-70b-v2"], "params": {"max_length": "lots"}},
            "4xx",
        ),
        (
            "txt_maxlen_negative",
            "POST",
            "/api/v2/generate/text/async",
            "json",
            {"prompt": "x", "models": ["elinas/chronos-70b-v2"], "params": {"max_length": -5}},
            "4xx",
        ),
        # ---- pop ---------------------------------------------------------
        ("pop_empty_body", "POST", "/api/v2/generate/pop", "json", {}, "4xx"),
        (
            "pop_models_not_list",
            "POST",
            "/api/v2/generate/pop",
            "json",
            {"name": "w", "models": "stable_diffusion", "bridge_agent": AGENT},
            "4xx",
        ),
        (
            # Worker name flows through sanitize_string (bleach), which strips
            # NUL bytes before they reach the DB, so this is silently cleaned
            # ("w\x00x" -> "wx") rather than rejected. The contract under test is
            # "never a 500"; the reactive NUL->400 path is exercised by raw-stored
            # fields like prompt (img_nullbyte_prompt) instead.
            "pop_nullbyte_name",
            "POST",
            "/api/v2/generate/pop",
            "json",
            {"name": "w\x00x", "models": ["stable_diffusion"], "bridge_agent": AGENT},
            "not500",
        ),
        # ---- submit ------------------------------------------------------
        ("submit_empty_body", "POST", "/api/v2/generate/submit", "json", {}, "4xx"),
        ("submit_id_number", "POST", "/api/v2/generate/submit", "json", {"id": 12345, "generation": "x"}, "not500"),
        ("submit_bad_uuid", "POST", "/api/v2/generate/submit", "json", {"id": "not-a-uuid", "generation": "x", "state": "ok"}, "not500"),
        # ---- kudos transfer ----------------------------------------------
        ("transfer_missing_username", "POST", "/api/v2/kudos/transfer", "json", {"amount": 10}, "4xx"),
        ("transfer_amount_string", "POST", "/api/v2/kudos/transfer", "json", {"username": "nobody#1", "amount": "lots"}, "4xx"),
        ("transfer_unknown_target", "POST", "/api/v2/kudos/transfer", "json", {"username": "ghost#999999", "amount": 10}, "4xx"),
        # ---- path params: overflow / garbage -----------------------------
        ("user_negative_id", "GET", "/api/v2/users/-1", "none", None, "not500"),
        ("user_overflow_id", "GET", "/api/v2/users/99999999999999999999999999", "none", None, "4xx"),
        ("worker_garbage_id", "GET", "/api/v2/workers/!!!garbage!!!", "none", None, "not500"),
        ("status_garbage_id", "GET", "/api/v2/generate/status/!!!garbage!!!", "none", None, "not500"),
        ("check_garbage_id", "GET", "/api/v2/generate/check/%00", "none", None, "not500"),
        ("stats_bad_model_state", "GET", "/api/v2/stats/img/models?model_state=__nope__", "none", None, "4xx"),
    ]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c[0])
def test_malformed_request_never_500s(client, auth, case):
    _id, method, path, kind, body, expect = case
    headers = dict(auth)
    if kind == "json":
        resp = client.open(path, method=method, json=body, headers=headers)
    elif kind == "raw":
        headers["Content-Type"] = "application/json"
        resp = client.open(path, method=method, data=body, headers=headers)
    else:
        resp = client.open(path, method=method, headers=headers)

    assert resp.status_code != SERVER_ERROR, (
        f"[{_id}] {method} {path} returned 500 on malformed input:\n{resp.get_data(as_text=True)[:600]}"
    )
    if expect == "4xx":
        assert 400 <= resp.status_code < 500, (
            f"[{_id}] expected a 4xx client error, got {resp.status_code}:\n{resp.get_data(as_text=True)[:400]}"
        )


# ---- unauthenticated access must be 401, not 500 ----------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/v2/generate/async", {"prompt": "x", "models": ["stable_diffusion"]}),
        ("POST", "/api/v2/generate/pop", {"name": "w", "models": ["stable_diffusion"]}),
        ("POST", "/api/v2/kudos/transfer", {"username": "x#1", "amount": 10}),
        ("PUT", "/api/v2/users/1", {"trusted": True}),
    ],
    ids=["async", "pop", "transfer", "user_put"],
)
def test_missing_api_key_is_unauthorized_not_500(client, method, path, body):
    resp = client.open(path, method=method, json=body, headers={"Client-Agent": AGENT})
    assert resp.status_code != SERVER_ERROR, resp.get_data(as_text=True)[:400]
    # Any 4xx is acceptable (RESTX returns 400 for a missing required header,
    # auth layers return 401/403, and existence checks may return 404); the
    # contract under test is "a client error, never an unhandled 500".
    assert 400 <= resp.status_code < 500, resp.get_data(as_text=True)[:400]


def test_out_of_range_user_id_is_not_found_not_500(client):
    """A user id beyond Postgres int4 range must resolve to 404, not a
    NumericValueOutOfRange 500 (regression guard for _coerce_user_id)."""
    resp = client.get("/api/v2/users/99999999999999999999999999")
    assert resp.status_code == 404
    assert resp.get_json()["rc"] == "UserNotFound"


def test_nul_byte_payload_is_rejected_cleanly(client, auth):
    """A NUL byte in a raw-stored JSON text field (prompt) is a clean 400, not a
    DB-flush 500. Regression guard for the reactive ValueError handler that
    catches psycopg2's NUL rejection and rolls the session back (see
    horde.exceptions.is_nul_byte_value_error)."""
    resp = client.post(
        "/api/v2/generate/async",
        json={"prompt": "hello\x00world", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512}},
        headers=auth,
    )
    assert resp.status_code == 400
    assert resp.get_json()["rc"] == "NulByteInPayload"
