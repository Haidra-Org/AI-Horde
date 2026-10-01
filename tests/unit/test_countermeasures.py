# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for ``horde.countermeasures``.

Two surfaces are exercised:

* The pure IP-classification helpers (no Redis, no network).
* The Redis-backed suspicion/timeout state machine, with fakeredis injected
  into the module globals that ``init_countermeasures`` would otherwise wire to
  the live IP caches. These lock the Fibonacci back-off and CIDR block-matching
  logic that gate abuse mitigation.
"""

from __future__ import annotations

import pytest
import redis

from horde import countermeasures as cm
from horde.countermeasures import CounterMeasures


class _UnreachableRedis:
    """Fail every command the rejection countermeasures send, as a Redis that dropped its connection does."""

    def incr(self, key: str) -> int:
        raise redis.ConnectionError("connection refused")

    def set(self, *args: object, **kwargs: object) -> bool:
        raise redis.ConnectionError("connection refused")


class TestIPClassification:
    def test_is_ipv4(self):
        assert CounterMeasures.is_ipv4("192.168.0.1") is True
        assert CounterMeasures.is_ipv4("10.0.0.0/8") is True  # networks accepted too
        assert CounterMeasures.is_ipv4("2001:db8::1") is False
        assert CounterMeasures.is_ipv4("not-an-ip") is False

    def test_is_ipv6(self):
        assert CounterMeasures.is_ipv6("2001:db8::1") is True
        assert CounterMeasures.is_ipv6("2001:db8::/32") is True
        assert CounterMeasures.is_ipv6("192.168.0.1") is False

    def test_is_valid_ip(self):
        assert CounterMeasures.is_valid_ip("8.8.8.8") is True
        assert CounterMeasures.is_valid_ip("2001:db8::1") is True
        assert CounterMeasures.is_valid_ip("garbage") is False

    def test_is_whitelisted_vpn(self):
        # 8.8.8.0/24 is in WHITELISTED_VPN_IPS.
        assert CounterMeasures.is_whitelisted_vpn("8.8.8.8") is True
        # A plain public IP that is not in any whitelisted range.
        assert CounterMeasures.is_whitelisted_vpn("1.1.1.1") is False


@pytest.fixture
def redis_caches(monkeypatch):
    """Inject independent fakeredis instances for the three IP caches."""
    fakeredis = pytest.importorskip("fakeredis")
    ip_r = fakeredis.FakeStrictRedis()
    ip_s_r = fakeredis.FakeStrictRedis()
    ip_t_r = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(cm, "ip_r", ip_r)
    monkeypatch.setattr(cm, "ip_s_r", ip_s_r)
    monkeypatch.setattr(cm, "ip_t_r", ip_t_r)
    return ip_r, ip_s_r, ip_t_r


class TestSuspicion:
    def test_report_suspicion_fibonacci_backoff(self, redis_caches):
        ip = "203.0.113.7"
        # timeout = (2*current + 1) * 3, current starting at 0 and incrementing.
        assert CounterMeasures.report_suspicion(ip) == 3
        assert CounterMeasures.report_suspicion(ip) == 9
        assert CounterMeasures.report_suspicion(ip) == 15
        # Stored suspicion count tracks the number of reports.
        assert CounterMeasures.retrieve_suspicion(ip) == 3

    def test_retrieve_suspicion_unknown_ip_is_zero(self, redis_caches):
        assert CounterMeasures.retrieve_suspicion("198.51.100.1") == 0

    def test_whitelisted_service_ip_timeout_capped(self, redis_caches):
        # 212.227.227.178 is a WHITELISTED_SERVICE_IP; its timeout is capped at 5.
        ip = "212.227.227.178"
        for _ in range(5):
            assert CounterMeasures.report_suspicion(ip) <= 5


class TestTimeouts:
    def test_set_and_retrieve_timeout(self, redis_caches):
        ip = "203.0.113.9"
        CounterMeasures.set_timeout(ip, minutes=3)
        ttl = CounterMeasures.retrieve_timeout(ip)
        assert 0 < ttl <= 3 * 60

    def test_retrieve_timeout_unknown_ip_no_block(self, redis_caches):
        assert CounterMeasures.retrieve_timeout("198.51.100.2", ignore_blocks=True) == 0

    def test_delete_timeout(self, redis_caches):
        ip = "203.0.113.10"
        CounterMeasures.set_timeout(ip, minutes=3)
        CounterMeasures.delete_timeout(ip)
        assert CounterMeasures.retrieve_timeout(ip, ignore_blocks=True) == 0


class TestBlockTimeouts:
    def test_block_timeout_matches_cidr(self, redis_caches):
        CounterMeasures.set_block_timeout("10.0.0.0/24", minutes=5)
        # An address inside the block is in timeout...
        assert CounterMeasures.retrieve_block_timeout("10.0.0.42") > 0
        # ...one outside it is not.
        assert CounterMeasures.retrieve_block_timeout("10.0.1.42") == 0

    def test_set_block_timeout_rejects_non_block(self, redis_caches):
        # A bare address (no "/prefix") is not a block and must be ignored.
        CounterMeasures.set_block_timeout("10.0.0.5", minutes=5)
        assert CounterMeasures.get_block_timeouts() == []

    def test_get_block_timeouts_matching_ip(self, redis_caches):
        CounterMeasures.set_block_timeout("172.16.0.0/16", minutes=5)
        CounterMeasures.set_block_timeout("192.168.0.0/24", minutes=5)
        matches = CounterMeasures.get_block_timeouts_matching_ip("172.16.5.5")
        assert len(matches) == 1
        assert matches[0]["ipaddr"] == "172.16.0.0/16"


class TestRejectionCounting:
    def test_rejections_count_per_ip_subject(self, redis_caches):
        """Model rejections from addresses in one IPv6 /64 count as one subject, and another subject counts from one."""
        assert [CounterMeasures.count_model_rejection("2001:db8:1:2::1") for _ in range(3)] == [1, 2, 3]
        assert CounterMeasures.count_model_rejection("2001:db8:1:2::ff") == 4
        assert CounterMeasures.count_model_rejection("203.0.113.20") == 1

    def test_counter_is_kept_apart_from_the_suspicion_level(self, redis_caches):
        """Counting a rejection leaves the address's suspicion level, which shares the database, unchanged."""
        ip = "203.0.113.21"
        CounterMeasures.count_model_rejection(ip)
        CounterMeasures.count_model_rejection(ip)
        assert CounterMeasures.retrieve_suspicion(ip) == 0

    def test_window_starts_at_the_first_rejection(self, redis_caches):
        """The counter expires one window after the first rejection; later rejections do not extend it."""
        _, ip_s_r, _ = redis_caches
        ip = "203.0.113.22"
        CounterMeasures.count_model_rejection(ip)
        key = f"{cm.MODEL_REJECTION_COUNT_KEY_PREFIX}{ip}"
        assert 0 < ip_s_r.ttl(key) <= cm.REJECTION_WINDOW.total_seconds()
        ip_s_r.expire(key, 100)
        CounterMeasures.count_model_rejection(ip)
        assert 0 < ip_s_r.ttl(key) <= 100

    def test_nothing_is_counted_without_the_suspicion_redis(self, monkeypatch):
        """Without the suspicion Redis every rejection counts as 0."""
        monkeypatch.setattr(cm, "ip_s_r", None)
        assert CounterMeasures.count_model_rejection("203.0.113.23") == 0

    def test_a_failing_suspicion_redis_counts_nothing(self, monkeypatch):
        """A Redis error while counting yields 0, so the rejection is answered without a countermeasure."""
        monkeypatch.setattr(cm, "ip_s_r", _UnreachableRedis())
        assert CounterMeasures.count_model_rejection("203.0.113.24") == 0

    @pytest.mark.parametrize("origin", [None, "", "proxy-supplied text", "10.0.0.0/8"])
    def test_an_origin_without_a_subject_is_not_counted(self, redis_caches, origin):
        """An origin with no IP subject counts as 0 and writes no key."""
        _, ip_s_r, _ = redis_caches
        assert CounterMeasures.count_model_rejection(origin) == 0
        assert ip_s_r.keys() == []


class TestRejectionNotice:
    def test_first_claim_in_the_window_is_due(self, redis_caches):
        """Only the first claim per subject in the window is due, and the mute expires with the window."""
        _, ip_s_r, _ = redis_caches
        ip = "203.0.113.30"
        assert CounterMeasures.claim_model_rejection_notice(ip) is True
        assert CounterMeasures.claim_model_rejection_notice(ip) is False
        assert CounterMeasures.claim_model_rejection_notice("::ffff:203.0.113.30") is False
        assert CounterMeasures.claim_model_rejection_notice("203.0.113.31") is True
        key = f"{cm.MODEL_REJECTION_NOTICE_KEY_PREFIX}{ip}"
        assert 0 < ip_s_r.ttl(key) <= cm.REJECTION_WINDOW.total_seconds()

    def test_no_notice_is_due_without_the_suspicion_redis(self, monkeypatch):
        """Without the suspicion Redis no notice is due, since no model rejection is counted either."""
        monkeypatch.setattr(cm, "ip_s_r", None)
        assert CounterMeasures.claim_model_rejection_notice("203.0.113.32") is False

    def test_no_notice_is_due_while_the_suspicion_redis_fails(self, monkeypatch):
        """A Redis error while claiming yields no notice and no exception."""
        monkeypatch.setattr(cm, "ip_s_r", _UnreachableRedis())
        assert CounterMeasures.claim_model_rejection_notice("203.0.113.33") is False

    def test_no_notice_is_due_for_an_origin_without_a_subject(self, redis_caches):
        """An origin with no IP subject is never due a notice."""
        assert CounterMeasures.claim_model_rejection_notice("proxy-supplied text") is False
