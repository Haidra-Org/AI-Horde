# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Chat collector billing behavior after the durable-settlement refactor.

Design: the HTTP response collectors (`_stream_openai` / `_collect_response`) are
NO LONGER the settler. LIVE money is settled durably + authoritatively in the
worker-WS handler (see test_reservation_lifecycle.py). The collectors only:

  * feed dry-run OBSERVABILITY (log would-charge on grid counts), and
  * compute the client-facing usage on grid counts (never zeroed by a silent
    worker), as faithful display.

These tests pin exactly that: dry-run observes on grid counts; live mode does
NOT settle in the collector; display usage falls back to grid counts.
"""

import uuid

import pytest

from grid_api.routers import openai as o
from grid_api.services import credits, den, token_stream

MODEL = "gpt-oss-120b"


def _fake_subscribe(events):
    async def gen(job_id, *a, **kw):
        for e in events:
            yield e
    return gen


@pytest.fixture
def spy(monkeypatch):
    """Record charge_request (dry-run observe) and reconcile (live settle) calls."""
    charge, reconcile = [], []

    async def fake_charge(user, model, p, c, job_id):
        charge.append({"p": p, "c": c})
        return {"status": "dry_run", "charged": 0}

    async def fake_reconcile(*a, **k):
        reconcile.append(a)

    monkeypatch.setattr(credits, "charge_request", fake_charge)
    monkeypatch.setattr(credits, "reconcile", fake_reconcile)
    return {"charge": charge, "reconcile": reconcile}


@pytest.mark.asyncio
async def test_collect_observes_grid_count_in_dry_run(monkeypatch, spy):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    answer = "Paris, the capital of France, founded over two thousand years ago."
    events = [
        {"delta": {"content": answer}},
        # Worker LIES with zero usage; the grid must observe its OWN count.
        {"text": token_stream.DONE_SENTINEL, "full_text": answer,
         "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "finish_reason": "stop"},
    ]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    resp = await o._collect_response("j1", MODEL, {"account_id": uuid.uuid4()}, seed=None, prompt_toks=11)

    assert len(spy["charge"]) == 1 and not spy["reconcile"]   # observed, did not settle
    assert spy["charge"][0]["p"] == 11
    assert spy["charge"][0]["c"] == den.count_tokens(answer) > 0
    # Display usage falls back to grid count when the worker zeroes it.
    assert resp["usage"]["completion_tokens"] == den.count_tokens(answer)


@pytest.mark.asyncio
async def test_collect_does_not_settle_in_live(monkeypatch, spy):
    """LIVE mode: settlement is worker_ws's job, not the collector's."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    events = [{"delta": {"content": "hi there"}},
              {"text": token_stream.DONE_SENTINEL, "full_text": "hi there", "finish_reason": "stop"}]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    await o._collect_response("j2", MODEL, {"account_id": uuid.uuid4()}, seed=None, prompt_toks=3)
    assert not spy["charge"] and not spy["reconcile"]  # collector is silent in live


@pytest.mark.asyncio
async def test_stream_observes_grid_count_in_dry_run(monkeypatch, spy):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    parts = ["The last revolution ", "was metered in kWh, ", "this one in tokens."]
    events = [{"delta": {"content": p}} for p in parts] + [
        {"text": token_stream.DONE_SENTINEL,
         "usage": {"prompt_tokens": 5, "completion_tokens": 0}, "finish_reason": "stop"},
    ]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    async for _ in o._stream_openai("j3", MODEL, "cid", {"account_id": uuid.uuid4()}, seed=None, prompt_toks=7):
        pass
    assert len(spy["charge"]) == 1 and not spy["reconcile"]
    assert spy["charge"][0]["c"] == den.count_tokens("".join(parts)) > 0


@pytest.mark.asyncio
async def test_stream_does_not_settle_in_live(monkeypatch, spy):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    events = [{"delta": {"content": "abc"}}, {"text": token_stream.DONE_SENTINEL, "finish_reason": "stop"}]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))
    async for _ in o._stream_openai("j4", MODEL, "cid", {"account_id": uuid.uuid4()}, seed=None, prompt_toks=2):
        pass
    assert not spy["charge"] and not spy["reconcile"]


@pytest.mark.asyncio
async def test_stream_observes_once_on_disconnect(monkeypatch, spy):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    events = [{"delta": {"content": "first part "}}, {"delta": {"content": "second part"}}]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))
    agen = o._stream_openai("j5", MODEL, "cid", {"account_id": uuid.uuid4()}, seed=None, prompt_toks=1)
    await agen.__anext__()  # role
    await agen.__anext__()  # first delta
    await agen.aclose()     # disconnect
    assert len(spy["charge"]) == 1  # observed once in finally
    assert spy["charge"][0]["c"] == den.count_tokens("first part ")
