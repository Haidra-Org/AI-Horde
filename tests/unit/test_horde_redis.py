# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fan-out writes must reach the primary redis server before any secondary, and local copies stay short-lived."""

from __future__ import annotations

from datetime import timedelta

import redis

from horde.horde_redis import LOCAL_CACHE_MAX_LIFETIME, LOCAL_CACHE_SET_LIFETIME, HordeRedis


def _client(host: str, calls: list[str]) -> redis.Redis:
    client = redis.Redis(host=host, port=6379, socket_connect_timeout=0.01)
    client.setex = lambda key, expiry, value: calls.append(host)  # type: ignore[method-assign]
    client.set = lambda key, value: calls.append(host)  # type: ignore[method-assign]
    client.delete = lambda key: calls.append(host)  # type: ignore[method-assign]
    return client


def test_writes_reach_the_primary_first_regardless_of_configured_order():
    calls: list[str] = []
    horde_redis = HordeRedis()
    horde_redis.horde_r = _client("primary", [])
    horde_redis.all_horde_redis = [_client("passive", calls), _client("primary", calls)]

    horde_redis.horde_r_setex("key", timedelta(seconds=1), "value")
    horde_redis.horde_r_set("key", "value")
    horde_redis.horde_r_delete("key")

    assert calls == ["primary", "passive"] * 3


def test_without_a_primary_the_configured_order_is_kept():
    calls: list[str] = []
    horde_redis = HordeRedis()
    horde_redis.all_horde_redis = [_client("b", calls), _client("a", calls)]

    horde_redis.horde_r_setex("key", timedelta(seconds=1), "value")

    assert calls == ["b", "a"]


def _expiry_recorder(host: str, expiries: list[tuple[str, timedelta]]) -> redis.Redis:
    client = redis.Redis(host=host, port=6379, socket_connect_timeout=0.01)
    client.setex = lambda key, expiry, value: expiries.append((host, expiry))  # type: ignore[method-assign]
    client.set = lambda key, value: expiries.append((host, timedelta(0)))  # type: ignore[method-assign]
    return client


def test_a_long_expiry_is_kept_shared_and_capped_locally():
    expiries: list[tuple[str, timedelta]] = []
    horde_redis = HordeRedis()
    horde_redis.horde_r = _expiry_recorder("primary", [])
    horde_redis.all_horde_redis = [_expiry_recorder("primary", expiries)]
    horde_redis.horde_local_r = _expiry_recorder("local", expiries)

    horde_redis.horde_r_setex("key", timedelta(seconds=600), "value")

    assert expiries == [("primary", timedelta(seconds=600)), ("local", LOCAL_CACHE_MAX_LIFETIME)]
    assert LOCAL_CACHE_MAX_LIFETIME == timedelta(seconds=5)


def test_a_short_expiry_is_kept_locally():
    expiries: list[tuple[str, timedelta]] = []
    horde_redis = HordeRedis()
    horde_redis.horde_local_r = _expiry_recorder("local", expiries)

    horde_redis.horde_r_setex("key", timedelta(seconds=2), "value")

    assert expiries == [("local", timedelta(seconds=2))]


def test_a_value_set_without_expiry_expires_locally():
    expiries: list[tuple[str, timedelta]] = []
    horde_redis = HordeRedis()
    horde_redis.horde_local_r = _expiry_recorder("local", expiries)

    horde_redis.horde_r_set("key", "value")

    assert expiries == [("local", LOCAL_CACHE_SET_LIFETIME)]
    assert LOCAL_CACHE_SET_LIFETIME == timedelta(seconds=10)
