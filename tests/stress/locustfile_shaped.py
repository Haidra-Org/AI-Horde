# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in staged-load entrypoint for the AI Horde Locust stress suite.

This imports the same users/events as ``locustfile.py`` and additionally exposes
``HordeStagesShape``. Use ``--stress-shape-profile`` and
``--stress-shape-scale`` to choose and tune the staged profile.
"""

from locustsuite import (
    ChatMixFrontClient,
    ChatMixNativeClient,
    ChatMixWorker,
    ExtendedWorkerSimulator,
    HotPathRequester,
    InterrogationRequester,
    InterrogationWorkerSimulator,
    LegacyWorkerSimulator,
    MetaBrowser,
    MisuseUser,
    RequestGenerator,
    SamplerFeatureRequester,
    StatusPoller,
    TextRequester,
    TextWorkerSimulator,
)
from locustsuite.shapes import HordeStagesShape

__all__ = [
    "ChatMixFrontClient",
    "ChatMixNativeClient",
    "ChatMixWorker",
    "ExtendedWorkerSimulator",
    "HordeStagesShape",
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
