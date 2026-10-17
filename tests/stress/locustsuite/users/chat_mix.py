# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The chat contract mix's user classes, exported for Locust discovery.

The populations are opt-in: ``events.on_test_start`` pins each to its ``--chat-mix-*`` count and leaves out a class
whose count is zero, because the clients need a running front and the workers need worker names their key owns. With
every count at zero (the default) the mix runs as before, which keeps the CI smoke job, which has no front, unchanged.

- ``ChatMixFrontClient`` ([`chat_front_client`][tests.stress.locustsuite.chat_front_client]) sends the front scenarios.
- ``ChatMixNativeClient`` ([`chat_native_client`][tests.stress.locustsuite.chat_native_client]) drives the native routes.
- ``ChatMixWorker`` ([`chat_worker_user`][tests.stress.locustsuite.chat_worker_user]) serves chat jobs as a persona.

The settings, arguments and run hooks they share are in [`chat_mix_settings`][tests.stress.locustsuite.chat_mix_settings].
"""

from ..chat_front_client import ChatMixFrontClient
from ..chat_native_client import ChatMixNativeClient
from ..chat_worker_user import ChatMixWorker

__all__ = ["ChatMixFrontClient", "ChatMixNativeClient", "ChatMixWorker"]
