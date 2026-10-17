# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""User classes exported for Locust discovery."""

from .chat_mix import ChatMixFrontClient, ChatMixNativeClient, ChatMixWorker
from .image import (
    BaselineFeatureRequester,
    ExtendedWorkerSimulator,
    HotPathRequester,
    LegacyWorkerSimulator,
    RequestGenerator,
    SamplerFeatureRequester,
    StatusPoller,
)
from .interrogation import InterrogationRequester, InterrogationWorkerSimulator
from .meta import MetaBrowser
from .misuse import MisuseUser
from .text import TextRequester, TextWorkerSimulator

__all__ = [
    "BaselineFeatureRequester",
    "ChatMixFrontClient",
    "ChatMixNativeClient",
    "ChatMixWorker",
    "ExtendedWorkerSimulator",
    "HotPathRequester",
    "InterrogationRequester",
    "InterrogationWorkerSimulator",
    "LegacyWorkerSimulator",
    "MetaBrowser",
    "MisuseUser",
    "RequestGenerator",
    "SamplerFeatureRequester",
    "StatusPoller",
    "TextRequester",
    "TextWorkerSimulator",
]
