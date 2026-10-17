# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run the chat front with uvicorn: ``python -m horde.oai``.

Settings come from the ``HORDE_OAI_*`` environment variables (see ``horde.oai.settings``).
``HORDE_OAI_FORWARDED_ALLOW_IPS`` is passed to uvicorn, which then takes the client address from the trusted
listener's ``X-Forwarded-For``; that address is the per-source admission key and the ``Proxied-For`` the Horde sees.
``HORDE_OAI_SHUTDOWN_DRAIN_SECONDS`` bounds how long a clean stop waits for open requests before cancelling them.
"""

from __future__ import annotations

import math

import uvicorn

from horde.oai.app import create_app
from horde.oai.settings import load_settings


def main() -> None:
    """Serve the chat front until interrupted."""
    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.listen,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=math.ceil(settings.shutdown_drain_seconds),
    )


if __name__ == "__main__":
    main()
