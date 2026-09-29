# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Normalize request addresses into the IP subject they are judged as, and key subjects with the deployment secret.

``CounterMeasures.ip_subject`` is the one normalization for evidence capture, the evidence address filter, worker
report records and worker IP blocks. ``CounterMeasures.ip_subject_key`` is the address pseudonym stored beside it.
"""

from __future__ import annotations

import re

import pytest

from horde import countermeasures
from horde.countermeasures import CounterMeasures, load_ip_subject_key_secret

pytestmark = pytest.mark.unit

TEST_SECRET: bytes = b"unit-test deployment secret"
"""A private secret the key tests install in place of the environment's."""


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key pseudonyms with ``TEST_SECRET``."""
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_SECRET)


@pytest.mark.parametrize(
    ("ipaddr", "subject"),
    [
        ("192.0.2.7", "192.0.2.7"),
        (" 192.0.2.7 ", "192.0.2.7"),
        ("192.0.2.7/32", "192.0.2.7"),
        ("::ffff:192.0.2.7", "192.0.2.7"),
        ("2001:db8:abcd:1234:5678::1", "2001:db8:abcd:1234::/64"),
        ("2001:DB8:ABCD:1234:0:0:0:1", "2001:db8:abcd:1234::/64"),
        ("2001:db8:abcd:1234::/64", "2001:db8:abcd:1234::/64"),
        ("2001:db8:abcd:1234:8000::/80", "2001:db8:abcd:1234::/64"),
        ("2001:db8:abcd:1234::1/128", "2001:db8:abcd:1234::/64"),
    ],
)
def test_addresses_and_narrow_ipv6_networks_normalize_to_their_subject(ipaddr: str, subject: str) -> None:
    assert CounterMeasures.parse_ip_subject(ipaddr) == subject
    assert CounterMeasures.ip_subject(ipaddr) == subject


def test_normalizing_a_subject_again_returns_it_unchanged() -> None:
    for ipaddr in ("192.0.2.7", "::ffff:192.0.2.7", "2001:db8:1:2:3:4:5:6"):
        subject = CounterMeasures.ip_subject(ipaddr)
        assert CounterMeasures.ip_subject(subject) == subject


def test_a_mapped_ipv4_address_is_the_same_subject_as_the_ipv4_address() -> None:
    assert CounterMeasures.ip_subject("::ffff:198.51.100.1") == CounterMeasures.ip_subject("198.51.100.1")


@pytest.mark.parametrize(
    "ipaddr",
    [
        "not an address",
        "198.51.100.0/24",
        "2001:db8::/48",
        "::ffff:198.51.100.0/120",
        "300.1.2.3",
    ],
)
def test_values_that_are_not_one_subject_are_refused_by_the_parser_and_have_no_subject(ipaddr: str) -> None:
    with pytest.raises(ValueError):
        CounterMeasures.parse_ip_subject(ipaddr)
    assert CounterMeasures.ip_subject(f"  {ipaddr} ") is None


@pytest.mark.parametrize("ipaddr", [None, "", "   "])
def test_blank_or_missing_addresses_have_no_subject(ipaddr: str | None) -> None:
    assert CounterMeasures.ip_subject(ipaddr) is None


@pytest.mark.usefixtures("keyed")
def test_the_key_is_a_stable_hexadecimal_digest_per_subject() -> None:
    key = CounterMeasures.ip_subject_key("192.0.2.7")

    assert key is not None
    assert re.fullmatch("[0-9a-f]{64}", key)
    assert CounterMeasures.ip_subject_key("192.0.2.7") == key
    assert CounterMeasures.ip_subject_key(CounterMeasures.ip_subject("::ffff:192.0.2.7")) == key
    assert CounterMeasures.ip_subject_key("192.0.2.8") != key


@pytest.mark.usefixtures("keyed")
def test_addresses_in_one_ipv6_64_share_a_key() -> None:
    first = CounterMeasures.ip_subject_key(CounterMeasures.ip_subject("2001:db8:5:6::1"))
    second = CounterMeasures.ip_subject_key(CounterMeasures.ip_subject("2001:db8:5:6::ffff"))
    other_network = CounterMeasures.ip_subject_key(CounterMeasures.ip_subject("2001:db8:5:7::1"))

    assert first == second
    assert first != other_network


def test_the_key_depends_on_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_SECRET)
    first = CounterMeasures.ip_subject_key("192.0.2.7")
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", TEST_SECRET + b" rotated")

    assert CounterMeasures.ip_subject_key("192.0.2.7") != first


@pytest.mark.usefixtures("keyed")
def test_no_subject_has_no_key() -> None:
    assert CounterMeasures.ip_subject_key(None) is None


def test_no_key_is_produced_without_a_usable_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(countermeasures, "IP_SUBJECT_KEY_SECRET", None)

    assert CounterMeasures.ip_subject_key("192.0.2.7") is None


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"secret_key": ""},
        {"secret_key": "changeme"},
        {"secret_key": "s0m3s3cr3t"},
    ],
)
def test_an_unset_or_published_secret_yields_no_key_secret_and_one_warning(
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(countermeasures.logger, "warning", warnings.append)

    assert load_ip_subject_key_secret(environ) is None
    assert len(warnings) == 1


def test_a_private_secret_keys_pseudonyms_without_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(countermeasures.logger, "warning", warnings.append)

    assert load_ip_subject_key_secret({"secret_key": "a private deployment secret"}) == b"a private deployment secret"
    assert warnings == []
