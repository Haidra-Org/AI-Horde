# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import hmac
import ipaddress
import os
import time as _time
from collections.abc import Mapping
from datetime import timedelta

import logfire
import requests

from horde.argparser import args
from horde.consts import WHITELISTED_SERVICE_IPS, WHITELISTED_VPN_IPS
from horde.logger import logger
from horde.metrics import ip_check_duration
from horde.redis_ctrl import (
    get_ipaddr_db,
    get_ipaddr_suspicion_db,
    get_ipaddr_timeout_db,
    is_redis_up,
)

ip_r = None
ip_s_r = None
ip_t_r = None


def init_countermeasures():
    global ip_r, ip_s_r, ip_t_r
    logger.init("IP Caches", status="Connecting")
    if is_redis_up():
        ip_r = get_ipaddr_db()
        ip_s_r = get_ipaddr_suspicion_db()
        ip_t_r = get_ipaddr_timeout_db()
        logger.init_ok("IP Caches", status="Connected")
    else:
        logger.init_err("IP Caches", status="Failed")


test_timeout = 0
# Upper bound for the fallback timeout accumulator used only when the suspicion
# Redis is unavailable; caps the resulting IP timeout at MAX_TEST_TIMEOUT * 3
# seconds so a Redis outage cannot escalate into effectively permanent bans.
MAX_TEST_TIMEOUT = 300

IPV6_SUBJECT_PREFIX_LENGTH: int = 64
"""The IPv6 prefix length an address is judged, blocked and keyed by.

IPv6 clients commonly receive a /64 and rotate addresses within it, so a single IPv6 address does not follow a client.
"""

IP_SUBJECT_SECRET_ENV: str = "secret_key"
"""The environment variable holding the deployment secret that keys address pseudonyms.

``hash_api_key`` in ``horde.utils`` salts stored API keys with the same secret, so every deployment sets it and it never
rotates: rotating it would invalidate every stored API key.
"""
PLACEHOLDER_SECRETS: frozenset[str] = frozenset(
    {
        "changeme",
        "s0m3s3cr3t",
    },
)
"""Published values of the deployment secret: the ``.env_template`` placeholder and ``hash_api_key``'s fallback.

The IPv4 space is small enough to enumerate, so a pseudonym keyed with a published secret reverses to its address.
"""
IP_SUBJECT_KEY_LABEL: bytes = b"AI-Horde moderation IP subject key"
"""The domain-separation label hashed before every IP subject, so no other use of the secret yields the same value."""


def load_ip_subject_key_secret(environ: Mapping[str, str] = os.environ) -> bytes | None:
    """Return the secret that keys address pseudonyms, or None when the deployment has no private secret.

    Logs one warning when the secret is unset or one of ``PLACEHOLDER_SECRETS``.

    Args:
        environ: Variables to read; the process environment by default.

    Returns:
        The encoded secret, or None when pseudonyms must not be produced.
    """
    secret = environ.get(IP_SUBJECT_SECRET_ENV)
    if not secret or secret in PLACEHOLDER_SECRETS:
        logger.warning(
            f"{IP_SUBJECT_SECRET_ENV} is unset or a published placeholder, so moderation records store no IP subject key",
        )
        return None
    return secret.encode()


IP_SUBJECT_KEY_SECRET: bytes | None = load_ip_subject_key_secret()
"""The secret keying ``CounterMeasures.ip_subject_key``, read from the environment at import, or None when unusable."""


class CounterMeasures:
    @staticmethod
    def set_safe(ipaddr, is_safe):
        """Stores the safety of the IP in redis temporarily"""
        ip_r.setex(ipaddr, timedelta(hours=6), int(is_safe))
        return is_safe

    @staticmethod
    def get_safe(ipaddr):
        is_safe = ip_r.get(ipaddr)
        if is_safe is None:
            return is_safe
        return bool(int(is_safe))

    @staticmethod
    def is_ip_safe(ipaddr):
        """Returns False if the IP is not false
        Else return true
        This function is a bit obscured with env vars to prevent defeat
        """
        with logfire.span("horde.countermeasures.is_ip_safe", cached=False) as ip_span:
            return CounterMeasures._is_ip_safe(ipaddr, ip_span)

    @staticmethod
    def _is_ip_safe(ipaddr, ip_span):
        """Returns False if the IP is not false
        Else return true
        This function is a bit obscured with env vars to prevent defeat
        """
        t0 = _time.monotonic()
        # return True # FIXME: Until I figure this out
        if args.allow_all_ips or os.getenv("IP_CHECKER", "") == "":
            return True
        # If we don't have the cache up, it's always OK
        if not ip_r:
            return True
        safety_threshold = 0.93
        timeout = 2.00
        if CounterMeasures.is_whitelisted_vpn(ipaddr):
            return True
        is_safe = CounterMeasures.get_safe(ipaddr)
        if is_safe is None:
            try:
                result = requests.get(os.getenv("IP_CHECKER").format(ipaddr=ipaddr), timeout=timeout)
            except Exception as err:
                logger.error(f"Exception when requesting info from checker: {err}")
                return None
            if not result.ok:
                if result.status_code == 429:
                    # If we exceeded the amount of requests we can do to the IP checker, we ask the client to try again later.
                    return None
                else:
                    probability = float(result.content)
                if probability == int(os.getenv("IP_CHECKER_LC")):
                    is_safe = CounterMeasures.set_safe(ipaddr, True)
                else:
                    is_safe = CounterMeasures.set_safe(ipaddr, True)  # True until I can improve my load
                    logger.error(f"An error occurred while validating IP. Return Code: {result.text}")
            else:
                probability = float(result.content)
                is_safe = CounterMeasures.set_safe(ipaddr, probability < safety_threshold)
            logger.debug(f"IP {ipaddr} has a probability of {probability}. Safe = {is_safe}")
        else:
            ip_span.set_attribute("cached", True)
        ip_check_duration.record(_time.monotonic() - t0, {"horde.cached": is_safe is not None})
        return is_safe

    @staticmethod
    def report_suspicion(ipaddr):
        """Increases the suspicion of an IP in redis temporarily"""
        if not ip_s_r:
            global test_timeout
            # Bounded growth: without a cap this doubles every call and never
            # resets while the suspicion Redis is down, quickly producing
            # absurd (multi-day) timeouts that never recover.
            test_timeout = min(test_timeout + test_timeout + 1, MAX_TEST_TIMEOUT)
            timeout = test_timeout * 3
            logger.debug(f"Redis not available, so setting test_timeout to {test_timeout}")
            CounterMeasures.set_timeout(ipaddr, timeout)
            return test_timeout
        current_suspicion = ip_s_r.get(ipaddr)
        if current_suspicion is None:
            current_suspicion = 0
        current_suspicion = int(current_suspicion)
        suspicion_timeout = 24
        if ipaddr in WHITELISTED_SERVICE_IPS:
            suspicion_timeout = 1
        ip_s_r.setex(ipaddr, timedelta(hours=suspicion_timeout), current_suspicion + 1)
        # Fibonacci in seconds FTW!
        timeout = (current_suspicion + current_suspicion + 1) * 3
        if ipaddr in WHITELISTED_SERVICE_IPS and timeout > 5:
            timeout = 5
        CounterMeasures.set_timeout(ipaddr, timeout)
        return timeout

    @staticmethod
    def report_proxy_suspicion(ipaddr):
        """Increases the suspicion of proxy service's IP in redis temporarily"""
        if not ip_s_r:
            global test_timeout
            test_timeout = min(test_timeout + test_timeout + 1, MAX_TEST_TIMEOUT)
            timeout = test_timeout * 3
            logger.debug(f"Redis not available, so setting test_timeout to {test_timeout}")
            CounterMeasures.set_timeout(ipaddr, timeout)
            return test_timeout
        current_suspicion = ip_s_r.get(ipaddr)
        if current_suspicion is None:
            current_suspicion = 0
        current_suspicion = int(current_suspicion)
        suspicion_timeout = 1
        ip_s_r.setex(ipaddr, timedelta(hours=suspicion_timeout), current_suspicion + 1)
        return current_suspicion

    @staticmethod
    def retrieve_suspicion(ipaddr):
        """Checks the current suspicion of an IP address"""
        if not ip_s_r:
            return 0
        current_suspicion = ip_s_r.get(ipaddr)
        if current_suspicion is None:
            current_suspicion = 0
        return int(current_suspicion)

    @staticmethod
    def set_timeout(ipaddr, minutes):
        """Puts the ip address into timeout for these amount of seconds"""
        if not ip_t_r:
            return
        ip_t_r.setex(ipaddr, timedelta(minutes=minutes), int(True))

    @staticmethod
    def retrieve_timeout(ipaddr, ignore_blocks=False):
        """Checks if an IP address is still in timeout.

        A timeout set on the address's IP subject applies as well, so an IPv4 client that reaches the horde as an
        IPv4-mapped IPv6 address is held by a timeout on its IPv4 address.
        """
        if not ip_t_r:
            return 0
            # return test_timeout * 3 * 60
        subject = CounterMeasures.ip_subject(ipaddr)
        timeout_keys = [ipaddr]
        if subject is not None and subject != ipaddr:
            timeout_keys.append(subject)
        for timeout_key in timeout_keys:
            if bool(ip_t_r.get(timeout_key)):
                return int(ip_t_r.ttl(timeout_key))
        if ignore_blocks is True:
            return 0
        # An IPv6 subject is a network, so a block range is matched against the address itself.
        block_address = subject if subject is not None and "/" not in subject else ipaddr
        return CounterMeasures.retrieve_block_timeout(block_address)

    @staticmethod
    def delete_timeout(ipaddr):
        """Deletes an IP address in timeout"""
        if not ip_t_r:
            return
        ip_t_r.delete(ipaddr)
        ip_s_r.delete(ipaddr)

    @staticmethod
    def is_whitelisted_vpn(ipaddr):
        return any(ipaddress.ip_address(ipaddr) in ipaddress.ip_network(iprange) for iprange in WHITELISTED_VPN_IPS)

    @staticmethod
    def set_block_timeout(ip_block, minutes):
        """Puts the ip address block into timeout for these amount of seconds"""
        if not ip_t_r:
            return
        if len(ip_block.split("/")) != 2:
            logger.warning(f"Attempted to inset non-block {ip_block} IP as a block timeout")
            return
        ip_t_r.setex(f"ipblock_{ip_block}", timedelta(minutes=minutes), int(True))

    @staticmethod
    def retrieve_block_timeout(ipaddr):
        """Checks if the IP is in a block timeout"""
        if not ip_t_r:
            return None
        for ip_block_key in ip_t_r.scan_iter("ipblock_*"):
            ip_range = ip_block_key.decode().split("_", 1)[1]
            if ipaddress.ip_address(ipaddr) in ipaddress.ip_network(ip_range):
                ttl = ip_t_r.ttl(ip_block_key)
                return int(ttl)
        return 0

    @staticmethod
    def delete_block_timeout(ip_block):
        """Deletes an IP address block from being in timeout"""
        if not ip_t_r:
            return
        if len(ip_block.split("/")) != 2:
            logger.warning(f"Attempted to inset non-block {ip_block} IP as a block timeout")
            return
        ip_t_r.delete(f"ipblock_{ip_block}")

    @staticmethod
    def get_block_timeouts():
        """Returns all known IP block timeouts"""
        ip_blocks = []
        for ip_block_key in ip_t_r.scan_iter("ipblock_*"):
            ip_range = ip_block_key.decode().split("_", 1)[1]
            ip_blocks.append(
                {
                    "ipaddr": ip_range,
                    "seconds": ip_t_r.ttl(ip_block_key),
                },
            )
        return ip_blocks

    @staticmethod
    def get_block_timeouts_matching_ip(ipaddr):
        """Returns all known IP block timeouts which match a specific IP address"""
        ip_blocks = CounterMeasures.get_block_timeouts()
        timeouts = []
        for block in ip_blocks:
            if ipaddress.ip_address(ipaddr) in ipaddress.ip_network(block["ipaddr"]):
                timeouts.append(block)
        return timeouts

    @staticmethod
    def is_ipv6(ipaddr):
        try:
            ipaddress.IPv6Address(ipaddr)
            return True
        except ipaddress.AddressValueError:
            try:
                ipaddress.IPv6Network(ipaddr)
                return True
            except ipaddress.AddressValueError:
                return False

    @staticmethod
    def is_ipv4(ipaddr):
        try:
            ipaddress.IPv4Address(ipaddr)
            return True
        except ipaddress.AddressValueError:
            try:
                ipaddress.IPv4Network(ipaddr)
                return True
            except ipaddress.AddressValueError:
                return False

    @staticmethod
    def is_valid_ip(ipaddr):
        if CounterMeasures.is_ipv4(ipaddr):
            return True
        if CounterMeasures.is_ipv6(ipaddr):
            return True
        return False

    @staticmethod
    def parse_ip_subject(ipaddr: str) -> str:
        """Parse an address, or an IPv6 network of at most one /64, into the subject it is judged as.

        - An IPv4 address is its own subject.
        - An IPv4-mapped IPv6 address, as dual-stack sockets report IPv4 clients, is its IPv4 address.
        - An IPv6 address, or an IPv6 network of /64 or narrower, is its /64 network. An already normalized subject
          parses to itself.

        Args:
            ipaddr: Address or network text; surrounding whitespace is ignored.

        Returns:
            The IPv4 address or the IPv6 /64 network, as text.

        Raises:
            ValueError: The value is not an address, or is a network wider than one subject (any IPv4 network other
                than a /32, an IPv6 network wider than /64, or an IPv4-mapped network other than a /128).
        """
        network = ipaddress.ip_network(ipaddr.strip(), strict=False)
        is_single_address = network.prefixlen == network.max_prefixlen
        if isinstance(network, ipaddress.IPv4Network):
            if not is_single_address:
                raise ValueError(f"{ipaddr!r} is an IPv4 network, not an address")
            return str(network.network_address)
        mapped_address = network.network_address.ipv4_mapped
        if mapped_address is not None:
            if not is_single_address:
                raise ValueError(f"{ipaddr!r} is an IPv4-mapped network, not an address")
            return str(mapped_address)
        if network.prefixlen < IPV6_SUBJECT_PREFIX_LENGTH:
            raise ValueError(f"{ipaddr!r} is wider than one IPv6 /{IPV6_SUBJECT_PREFIX_LENGTH}")
        return str(network.supernet(new_prefix=IPV6_SUBJECT_PREFIX_LENGTH))

    @staticmethod
    def ip_subject(ipaddr: str | None) -> str | None:
        """Return the subject an address is judged, blocked and keyed as, or None when it is not one.

        A value ``parse_ip_subject`` accepts becomes its subject. Any other value, such as free text in a trusted
        proxy's ``Proxied-For`` header or a network wider than one subject, has no subject: it is not keyed or blocked;
        capture keeps the text as origin_text.

        Args:
            ipaddr: Request origin as the request reported it, or None when unknown.

        Returns:
            The IPv4 address or the IPv6 /64 network, or None when the value is blank or does not parse.
        """
        if ipaddr is None or not ipaddr.strip():
            return None
        try:
            return CounterMeasures.parse_ip_subject(ipaddr)
        except ValueError:
            return None

    @staticmethod
    def ip_subject_key(subject: str | None) -> str | None:
        """Return the address pseudonym of an IP subject: equal for equal subjects, and irreversible without the secret.

        The pseudonym is HMAC-SHA256 under ``IP_SUBJECT_KEY_SECRET`` over ``IP_SUBJECT_KEY_LABEL``, a NUL byte and the
        subject.

        Args:
            subject: A value ``ip_subject`` returned.

        Returns:
            The pseudonym as 64 hexadecimal characters, or None when the subject is None or the deployment has no
            usable secret.
        """
        if subject is None or IP_SUBJECT_KEY_SECRET is None:
            return None
        message = IP_SUBJECT_KEY_LABEL + b"\x00" + subject.encode()
        return hmac.new(IP_SUBJECT_KEY_SECRET, message, hashlib.sha256).hexdigest()
