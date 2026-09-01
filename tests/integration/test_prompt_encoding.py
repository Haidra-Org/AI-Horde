# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A prompt carrying unpaired UTF-16 surrogates is rejected up front instead of failing at the database."""

from __future__ import annotations

import json

AGENT = "aihorde_ci_client:1.0:(test)ci"


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT, "Content-Type": "application/json"}


def test_text_prompt_with_lone_surrogate_is_a_bad_request(client, api_key):
    body = {"prompt": "hello \udde2 world", "models": ["elinas/chronos-70b-v2"], "max_length": 80, "max_context_length": 1024}
    resp = client.post("/api/v2/generate/text/async", data=json.dumps(body), headers=_headers(api_key))
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json().get("rc") == "InvalidPromptEncoding"


def test_image_prompt_with_lone_surrogate_is_a_bad_request(client, api_key):
    body = {"prompt": "hello \udde2 world", "models": ["stable_diffusion"], "params": {"width": 512, "height": 512, "steps": 10}}
    resp = client.post("/api/v2/generate/async", data=json.dumps(body), headers=_headers(api_key))
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json().get("rc") == "InvalidPromptEncoding"
