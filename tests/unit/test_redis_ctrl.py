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
