# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the free-tier daily quota.

Verifies: policy-exempt users bypass the cap, free users are metered per day, the
limit raises 429 with a Retry-After, and a Redis outage fails OPEN (never
blocks inference).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from grid_api.services import quota


class FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        self.expires[key] = ttl

    async def get(self, key):
        return self.store.get(key)


class BrokenRedis:
    async def incr(self, key):
        raise ConnectionError("redis down")

    async def get(self, key):
        raise ConnectionError("redis down")


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(quota, "get_redis", lambda: r)
    return r


def _free_user(uid=1):
    return {"id": uid, "quota_exempt": False}


# ── paid bypass ──


@pytest.mark.asyncio
async def test_paid_user_is_not_metered(fake_redis, monkeypatch):
    user = {"id": 1, "quota_exempt": True}
    # Way over any limit, but paid → never raises, never touches redis.
    for _ in range(10_000):
        await quota.check_and_consume(user)
    assert fake_redis.store == {}


def test_is_paid_uses_explicit_policy():
    assert quota.is_paid({"quota_exempt": True}) is True
    assert quota.is_paid({"quota_exempt": False}) is False
    assert quota.is_paid({"kudos": 1_000_000}) is False
    assert quota.is_paid({}) is False


# ── free metering ──


@pytest.mark.asyncio
async def test_free_user_allowed_up_to_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(quota, "FREE_DAILY_LIMIT", 5)
    user = _free_user()
    for _ in range(5):
        await quota.check_and_consume(user)  # 1..5 ok


@pytest.mark.asyncio
async def test_free_user_blocked_over_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(quota, "FREE_DAILY_LIMIT", 3)
    user = _free_user()
    for _ in range(3):
        await quota.check_and_consume(user)
    with pytest.raises(HTTPException) as exc:
        await quota.check_and_consume(user)
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


@pytest.mark.asyncio
async def test_first_request_sets_expiry(fake_redis, monkeypatch):
    monkeypatch.setattr(quota, "FREE_DAILY_LIMIT", 100)
    await quota.check_and_consume(_free_user())
    # Exactly one key, with a TTL set on first hit.
    assert len(fake_redis.expires) == 1
    ttl = next(iter(fake_redis.expires.values()))
    assert 0 < ttl <= 86400


@pytest.mark.asyncio
async def test_separate_users_have_separate_buckets(fake_redis, monkeypatch):
    monkeypatch.setattr(quota, "FREE_DAILY_LIMIT", 1)
    await quota.check_and_consume(_free_user(uid=1))
    # user 2 still has their own allowance
    await quota.check_and_consume(_free_user(uid=2))
    # user 1 is now over
    with pytest.raises(HTTPException):
        await quota.check_and_consume(_free_user(uid=1))


# ── fail open ──


@pytest.mark.asyncio
async def test_redis_outage_fails_open(monkeypatch):
    monkeypatch.setattr(quota, "get_redis", lambda: BrokenRedis())
    monkeypatch.setattr(quota, "FREE_DAILY_LIMIT", 1)
    # Even far past the limit, a broken quota store must not block requests.
    for _ in range(50):
        await quota.check_and_consume(_free_user())


@pytest.mark.asyncio
async def test_missing_user_id_not_blocked(fake_redis, monkeypatch):
    monkeypatch.setattr(quota, "FREE_DAILY_LIMIT", 1)
    await quota.check_and_consume({"quota_exempt": False})  # no id → pass through
    await quota.check_and_consume({"quota_exempt": False})
