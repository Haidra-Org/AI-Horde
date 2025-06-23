# SPDX-FileCopyrightText: 2025 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from flask import request
from loguru import logger

from horde.database import cached_passkeys


def get_remoteaddr():
    """Returns the remote address of the request, accounting for proxies"""
    remoteaddr = request.remote_addr
    passkey = request.headers.get("Proxy-Authorization", None)
    if passkey:
        if cached_passkeys.is_passkey_known(passkey):
            remoteaddr = request.headers.get("Proxied-For", remoteaddr)
        else:
            logger.info(f"Unknown passkey {passkey} from {remoteaddr}. Ignoring Proxied-For header.")
    return remoteaddr
