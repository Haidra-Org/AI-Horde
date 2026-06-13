# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Semantic coverage for the worker-operations endpoints.

Covers the highest-traffic worker paths - pop, submit, and worker details/modify
- asserting *behaviour* (auth gating, the owner/moderator permission matrix,
state transitions, kudos accounting) rather than restating field plumbing. The
text product line is used because it needs no object storage, so a full
pop→submit cycle runs in-process.
"""

from __future__ import annotations

import pytest

TEXT_MODEL = "elinas/chronos-70b-v2"
AGENT = "aihorde_ci_client:1.0:(test)ci"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    """Disable Flask-Limiter for these tests so the per-endpoint limits
    (e.g. 30/min on worker PUT) don't turn into spurious 429s under a fast suite."""
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _headers(api_key: str) -> dict[str, str]:
    return {"apikey": api_key, "Client-Agent": AGENT}


def _expire_caches() -> None:
    """Flush the (fake) redis caches to simulate the 30s worker/user cache TTL
    elapsing, so a read-after-write reflects committed DB state rather than a
    cached snapshot taken before the mutation."""
    from horde import horde_redis as horde_redis_module

    redis_conn = horde_redis_module.horde_redis
    seen: dict[int, object] = {}
    for client in [redis_conn.horde_r, redis_conn.horde_local_r, *redis_conn.all_horde_redis]:
        if client is not None:
            seen[id(client)] = client
    for client in seen.values():
        try:
            client.flushdb()
        except Exception:
            continue


def _pop_text(client, api_key: str, worker_name: str, models=(TEXT_MODEL,)):
    """Pop a text job, which registers + checks in the worker as a side effect."""
    payload = {
        "name": worker_name,
        "models": list(models),
        "bridge_agent": AGENT,
        "amount": 10,
        "max_context_length": 4096,
        "max_length": 512,
    }
    return client.post("/api/v2/generate/text/pop", json=payload, headers=_headers(api_key))


def _worker_id_by_name(client, api_key: str, worker_name: str) -> str:
    resp = client.get(f"/api/v2/workers/name/{worker_name}", headers=_headers(api_key))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["id"]


# --------------------------------------------------------------------------- #
# Pop auth + empty-horde semantics                                            #
# --------------------------------------------------------------------------- #


class TestPopAuth:
    def test_anonymous_worker_forbidden(self, client):
        resp = _pop_text(client, "0000000000", "anon worker")
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "AnonForbiddenWorker"

    def test_invalid_api_key_rejected(self, client):
        resp = _pop_text(client, "definitely-not-a-real-key", "ghost worker")
        assert resp.status_code == 401

    def test_empty_horde_returns_no_job_but_registers_worker(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        resp = _pop_text(client, owner.api_key, "Lonely Scribe")
        assert resp.status_code == 200
        body = resp.get_json()
        # No waiting prompts queued -> explicit "no job" shape, not an error.
        assert body["id"] is None
        assert body["ids"] == []
        # The worker was nonetheless created and is now discoverable by name.
        assert _worker_id_by_name(client, owner.api_key, "Lonely Scribe")


# --------------------------------------------------------------------------- #
# Worker details + modify permission matrix                                   #
# --------------------------------------------------------------------------- #


class TestWorkerDetails:
    def test_unknown_worker_id_404(self, client, api_key):
        resp = client.get("/api/v2/workers/00000000-0000-0000-0000-000000000000", headers=_headers(api_key))
        assert resp.status_code == 404
        assert resp.get_json()["rc"] == "WorkerNotFound"


class TestWorkerModify:
    def test_owner_can_toggle_maintenance(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        _pop_text(client, owner.api_key, "Maint Worker")
        wid = _worker_id_by_name(client, owner.api_key, "Maint Worker")

        resp = client.put(f"/api/v2/workers/{wid}", json={"maintenance": True}, headers=_headers(owner.api_key))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["maintenance"] is True

        # Semantic: the state actually persisted, observable on a fresh (cache-expired) read.
        _expire_caches()
        details = client.get(f"/api/v2/workers/{wid}", headers=_headers(owner.api_key)).get_json()
        assert details["maintenance_mode"] is True

    def test_non_owner_non_mod_cannot_set_maintenance(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        _pop_text(client, owner.api_key, "Owned Worker")
        wid = _worker_id_by_name(client, owner.api_key, "Owned Worker")

        stranger = make_api_user(trusted=True, kudos=100)
        resp = client.put(f"/api/v2/workers/{wid}", json={"maintenance": True}, headers=_headers(stranger.api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotOwner"

    def test_pause_requires_moderator_not_just_owner(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)  # owner but NOT a moderator
        _pop_text(client, owner.api_key, "Pause Worker")
        wid = _worker_id_by_name(client, owner.api_key, "Pause Worker")

        resp = client.put(f"/api/v2/workers/{wid}", json={"paused": True}, headers=_headers(owner.api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotModerator"

    def test_moderator_can_pause_any_worker(self, client, make_api_user, api_key):
        owner = make_api_user(trusted=True, kudos=100)
        _pop_text(client, owner.api_key, "Mod Pause Worker")
        wid = _worker_id_by_name(client, owner.api_key, "Mod Pause Worker")

        # api_key fixture user is a moderator; pausing someone else's worker is allowed.
        resp = client.put(f"/api/v2/workers/{wid}", json={"paused": True}, headers=_headers(api_key))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["paused"] is True

    def test_empty_modify_is_rejected(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        _pop_text(client, owner.api_key, "NoOp Worker")
        wid = _worker_id_by_name(client, owner.api_key, "NoOp Worker")

        resp = client.put(f"/api/v2/workers/{wid}", json={}, headers=_headers(owner.api_key))
        assert resp.status_code == 400
        assert resp.get_json()["rc"] == "NoWorkerModSelected"


class TestWorkerDelete:
    def test_stranger_cannot_delete(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        _pop_text(client, owner.api_key, "Victim Worker")
        wid = _worker_id_by_name(client, owner.api_key, "Victim Worker")

        stranger = make_api_user(trusted=True, kudos=100)
        resp = client.delete(f"/api/v2/workers/{wid}", headers=_headers(stranger.api_key))
        assert resp.status_code == 403
        assert resp.get_json()["rc"] == "NotModerator"

    def test_owner_can_delete_and_worker_disappears(self, client, make_api_user):
        owner = make_api_user(trusted=True, kudos=100)
        _pop_text(client, owner.api_key, "Doomed Worker")
        wid = _worker_id_by_name(client, owner.api_key, "Doomed Worker")

        resp = client.delete(f"/api/v2/workers/{wid}", headers=_headers(owner.api_key))
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()["deleted_name"] == "Doomed Worker"

        # Semantic: it is gone, not merely flagged (read past the details cache).
        _expire_caches()
        gone = client.get(f"/api/v2/workers/{wid}", headers=_headers(owner.api_key))
        assert gone.status_code == 404


# --------------------------------------------------------------------------- #
# Submit semantics (full pop -> submit cycle + negative paths)                #
# --------------------------------------------------------------------------- #


class TestSubmit:
    def test_invalid_job_id_rejected(self, client, make_api_user):
        worker_user = make_api_user(trusted=True, kudos=100)
        resp = client.post(
            "/api/v2/generate/text/submit",
            json={"id": "00000000-0000-0000-0000-000000000000", "generation": "hi", "state": "ok"},
            headers=_headers(worker_user.api_key),
        )
        assert resp.status_code == 404
        assert resp.get_json()["rc"] == "InvalidJobID"

    def test_full_cycle_awards_kudos_and_rejects_duplicate(self, client, api_key, make_api_user):
        # Requester (moderator fixture user, has kudos) queues a text request.
        async_dict = {
            "prompt": "a quiet test prompt",
            "trusted_workers": True,
            "validated_backends": False,
            "max_length": 80,
            "max_context_length": 1024,
            "models": [TEXT_MODEL],
        }
        async_resp = client.post("/api/v2/generate/text/async", json=async_dict, headers=_headers(api_key))
        assert async_resp.status_code < 400, async_resp.get_data(as_text=True)
        req_id = async_resp.get_json()["id"]

        # Worker pops the job.
        worker_user = make_api_user(trusted=True, kudos=100)
        pop_resp = _pop_text(client, worker_user.api_key, "Cycle Scribe")
        assert pop_resp.status_code == 200, pop_resp.get_data(as_text=True)
        job_id = pop_resp.get_json()["id"]
        assert job_id is not None, "worker should have received the queued job"

        # A *different* user cannot submit a job they did not pop.
        stranger = make_api_user(trusted=True, kudos=100)
        wrong = client.post(
            "/api/v2/generate/text/submit",
            json={"id": job_id, "generation": "stolen", "state": "ok"},
            headers=_headers(stranger.api_key),
        )
        assert wrong.status_code == 403
        assert wrong.get_json()["rc"] == "WrongCredentials"

        # The owning worker submits -> positive kudos reward.
        submit = client.post(
            "/api/v2/generate/text/submit",
            json={"id": job_id, "generation": "a generated answer", "state": "ok"},
            headers=_headers(worker_user.api_key),
        )
        assert submit.status_code == 200, submit.get_data(as_text=True)
        assert submit.get_json()["reward"] > 0

        # Re-submitting the same finished job is a duplicate, not a second reward.
        dup = client.post(
            "/api/v2/generate/text/submit",
            json={"id": job_id, "generation": "again", "state": "ok"},
            headers=_headers(worker_user.api_key),
        )
        assert dup.status_code == 400
        assert dup.get_json()["rc"] == "DuplicateGen"

        # Clean up the queued request.
        client.delete(f"/api/v2/generate/text/status/{req_id}", headers=_headers(api_key))
