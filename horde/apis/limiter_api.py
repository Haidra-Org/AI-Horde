# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime, timedelta

from flask import request
from loguru import logger

from horde.consts import WHITELISTED_SERVICE_IPS
from horde.database import cached_passkeys
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


def get_remoteaddr():
    """Returns the remote address of the request, accounting for proxies"""
    remoteaddr = request.remote_addr
    passkey = request.headers.get("Proxy-Authorization", None)
    if passkey:
        if cached_passkeys.is_passkey_known(passkey):
            remoteaddr = request.headers.get("Proxied-For", remoteaddr)
        else:
            logger.info(f"Unknown passkey {passkey} from {remoteaddr}. Ignoring Proxied-For header.")
    logger.debug(f"Remote address: {remoteaddr}")
    return remoteaddr


# Used to for the flask limiter, to limit requests per url paths
def get_request_path():
    # logger.info(dir(request))
    return f"{get_remoteaddr()}@{request.method}@{request.path}"


def get_request_90min_limit_per_ip():
    if get_remoteaddr() in WHITELISTED_SERVICE_IPS or dynamic_ip_whitelist.is_ip_whitelisted(get_remoteaddr()):
        return "300/minute"
    return "90/minute"


def get_request_90hour_limit_per_ip():
    if get_remoteaddr() in WHITELISTED_SERVICE_IPS or dynamic_ip_whitelist.is_ip_whitelisted(get_remoteaddr()):
        return "600/hour"
    return "90/hour"


def get_request_2sec_limit_per_ip():
    if get_remoteaddr() in WHITELISTED_SERVICE_IPS or dynamic_ip_whitelist.is_ip_whitelisted(get_remoteaddr()):
        return "10/second"
    return "2/second"


def get_request_api_key():
    apikey = hash_api_key(request.headers.get("apikey", "0000000000"))
    return f"{apikey}@{request.method}@{request.path}"


def get_request_limit_per_apikey():
    apikey = request.headers.get("apikey", "0000000000")
    if apikey == "0000000000" or dynamic_ip_whitelist.is_ip_whitelisted(get_remoteaddr()):
        return "60/second"
    return "2/second"
