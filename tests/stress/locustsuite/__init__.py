# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AI Horde Locust workload package.

Importing this package registers the suite's Locust event listeners and exports
the concrete ``HttpUser`` classes that Locust should discover.
"""

from . import events as _events  # noqa: F401 - imported for Locust event registration side effects
from .users import (
    HotPathRequester,
    InterrogationRequester,
    InterrogationWorkerSimulator,
    MetaBrowser,
    MisuseUser,
    RequestGenerator,
    StatusPoller,
    TextRequester,
    TextWorkerSimulator,
    WorkerSimulator,
)

__all__ = [
    "HotPathRequester",
    "InterrogationRequester",
    "InterrogationWorkerSimulator",
    "MetaBrowser",
    "MisuseUser",
    "RequestGenerator",
    "StatusPoller",
    "TextRequester",
    "TextWorkerSimulator",
    "WorkerSimulator",
]
