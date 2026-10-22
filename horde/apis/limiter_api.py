# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timedelta

from flask import request

from horde.apis.request_utils import get_remoteaddr
from horde.config import Config
from horde.consts import WHITELISTED_SERVICE_IPS
from horde.utils import hash_api_key


class DynamicIPWhitelist:
    # Marks IPs to dynamically whitelist for 1 day
    # Those IPs will have lower limits during API calls
    whitelisted_ips = {}

    def whitelist_ip(self, ipaddr):
        self.whitelisted_ips[ipaddr] = datetime.now() + timedelta(days=1)

    def is_ip_whitelisted(self, ipaddr):
        if ipaddr not in self.whitelisted_ips:
            return False
        return self.whitelisted_ips[ipaddr] > datetime.now()


dynamic_ip_whitelist = DynamicIPWhitelist()


# Used to for the flask limiter, to limit requests per url paths
def get_request_path():
    # logger.info(dir(request))
    return f"{get_remoteaddr()}@{request.method}@{request.path}"


REQUEST_90MIN_LIMIT_PER_IP = "90/minute"
"""What an ordinary address is held to where this rate applies. A whitelisted service address gets more."""

REQUEST_2SEC_LIMIT_PER_IP = "2/second"
"""What an ordinary address is held to where this rate applies. A whitelisted service address gets more."""


def is_whitelisted_address():
    """Return whether the request comes from a service address or one whitelisted for the day.

    Returns:
        True when the address is held to the higher limits.
    """
    return get_remoteaddr() in WHITELISTED_SERVICE_IPS or dynamic_ip_whitelist.is_ip_whitelisted(get_remoteaddr())


def get_request_90min_limit_per_ip():
    if is_whitelisted_address():
        return "300/minute"
    return REQUEST_90MIN_LIMIT_PER_IP


def get_request_90hour_limit_per_ip():
    if is_whitelisted_address():
        return "600/hour"
    return "90/hour"


def get_request_2sec_limit_per_ip():
    if is_whitelisted_address():
        return "10/second"
    return REQUEST_2SEC_LIMIT_PER_IP


def get_chat_request_90min_limit_per_ip():
    """Return the per-minute limit on chat submits from the request's address.

    Clients behind one egress address, such as the users of one chat front, share this budget, so a deployment
    sizes it apart from the text limit. A whitelisted address gets the text route's whitelisted value.

    Returns:
        ``HORDE_CHAT_REQUEST_90MIN_LIMIT_PER_IP`` when it is set, otherwise the text limit.
    """
    if is_whitelisted_address() or Config.chat_request_90min_limit_per_ip is None:
        return get_request_90min_limit_per_ip()
    return Config.chat_request_90min_limit_per_ip


def get_chat_request_2sec_limit_per_ip():
    """Return the per-second limit on chat submits from the request's address.

    Clients behind one egress address, such as the users of one chat front, share this budget, so a deployment
    sizes it apart from the text limit. A whitelisted address gets the text route's whitelisted value.

    Returns:
        ``HORDE_CHAT_REQUEST_2SEC_LIMIT_PER_IP`` when it is set, otherwise the text limit.
    """
    if is_whitelisted_address() or Config.chat_request_2sec_limit_per_ip is None:
        return get_request_2sec_limit_per_ip()
    return Config.chat_request_2sec_limit_per_ip


def get_request_api_key():
    apikey = hash_api_key(request.headers.get("apikey", "0000000000"))
    return f"{apikey}@{request.method}@{request.path}"


def get_request_api_key_per_method():
    """Key a limit by the hashed API key and method alone.

    A decorated limit is already scoped to its route's endpoint, so leaving the concrete path out makes a route with a
    path parameter, such as one event's notes, share one budget across every value of the parameter.
    """
    apikey = hash_api_key(request.headers.get("apikey", "0000000000"))
    return f"{apikey}@{request.method}"


def get_request_limit_per_apikey():
    apikey = request.headers.get("apikey", "0000000000")
    if apikey == "0000000000" or dynamic_ip_whitelist.is_ip_whitelisted(get_remoteaddr()):
        return "60/second"
    return "2/second"
