# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every redis client the app builds must carry bounded socket timeouts.

An unbounded redis wait blocks the request thread that issued it, and with it the database connection,
transaction and advisory lock that thread holds; the timeouts turn a stalled redis into a handled exception.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from horde import redis_ctrl


@pytest.mark.parametrize(
    "factory",
    [
        redis_ctrl.get_horde_db,
        redis_ctrl.get_local_horde_db,
        redis_ctrl.get_ipaddr_db,
        redis_ctrl.get_ipaddr_suspicion_db,
        redis_ctrl.get_ipaddr_timeout_db,
        lambda: redis_ctrl.get_redis_db_server("127.0.0.1"),
    ],
)
def test_clients_carry_socket_timeouts(factory):
    connection_kwargs = factory().connection_pool.connection_kwargs
    assert connection_kwargs["socket_timeout"] == redis_ctrl.redis_socket_timeout_seconds
    assert connection_kwargs["socket_connect_timeout"] == redis_ctrl.redis_socket_connect_timeout_seconds


@pytest.mark.parametrize("url_factory", [redis_ctrl.ger_limiter_url, redis_ctrl.ger_cache_url])
def test_urls_carry_socket_timeouts(url_factory):
    url = urlsplit(url_factory())
    query = parse_qs(url.query)
    assert url.path.lstrip("/").isdigit()
    assert float(query["socket_timeout"][0]) == redis_ctrl.redis_socket_timeout_seconds
    assert float(query["socket_connect_timeout"][0]) == redis_ctrl.redis_socket_connect_timeout_seconds


def test_fan_out_clients_are_created_once_per_server(monkeypatch):
    monkeypatch.setenv("REDIS_SERVERS", '["10.0.0.1", "10.0.0.2"]')
    monkeypatch.setattr(redis_ctrl, "is_redis_up", lambda hostname, port=6379: True)
    monkeypatch.setattr(redis_ctrl, "_redis_db_server_clients", {})

    first = redis_ctrl.get_all_redis_db_servers()
    second = redis_ctrl.get_all_redis_db_servers()

    assert [c.connection_pool.connection_kwargs["host"] for c in first] == ["10.0.0.1", "10.0.0.2"]
    assert all(a is b for a, b in zip(first, second, strict=True))
