# SPDX-FileCopyrightText: 2022 AI Horde developers
#
# SPDX-License-Identifier: AGPL-3.0-only

import ipaddress
import os
from datetime import timedelta

import requests

from horde.argparser import args
from horde.consts import WHITELISTED_SERVICE_IPS, WHITELISTED_VPN_IPS
from horde.logger import logger
from horde.redis_ctrl import (
    get_ipaddr_db,
    get_ipaddr_suspicion_db,
    get_ipaddr_timeout_db,
    is_redis_up,
)

ip_r = None
logger.init("IP Address Cache", status="Connecting")
if is_redis_up():
    ip_r = get_ipaddr_db()
    logger.init_ok("IP Address Cache", status="Connected")
else:
    logger.init_err("IP Address Cache", status="Failed")
ip_s_r = None
logger.init("IP Suspicion Cache", status="Connecting")
if is_redis_up():
    ip_s_r = get_ipaddr_suspicion_db()
    logger.init_ok("IP Suspicion Cache", status="Connected")
else:
    logger.init_err("IP Suspicion Cache", status="Failed")
ip_t_r = None
logger.init("IP Timeout Cache", status="Connecting")
if is_redis_up():
    ip_t_r = get_ipaddr_timeout_db()
    logger.init_ok("IP Timeout Cache", status="Connected")
else:
    logger.init_err("IP Timeout Cache", status="Failed")

test_timeout = 0


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
        return is_safe

    @staticmethod
    def report_suspicion(ipaddr):
        """Increases the suspicion of an IP in redis temporarily"""
        if not ip_s_r:
            global test_timeout
            test_timeout = test_timeout + test_timeout + 1
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
        """Checks if an IP address is still in timeout"""
        if not ip_t_r:
            return test_timeout * 3 * 60
        has_timeout = ip_t_r.get(ipaddr)
        if not bool(has_timeout):
            if ignore_blocks is True:
                return 0
            return CounterMeasures.retrieve_block_timeout(ipaddr)
        ttl = ip_t_r.ttl(ipaddr)
        return int(ttl)

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
    def extract_ipv6_subnet(ipaddr, subnet_prefix_length=64):
        try:
            ip = ipaddress.IPv6Address(ipaddr)
            network = ipaddress.IPv6Network(f"{ip.exploded}/{subnet_prefix_length}", strict=False)
            return str(network)
        except ipaddress.AddressValueError:
            return None
