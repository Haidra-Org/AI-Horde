# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compatibility entrypoint for the AI Horde Locust stress suite.

The workload lives in the ``locustsuite`` package so requestors, workers,
read-only browsing, misuse probes, shared helpers, and Locust events stay in
coherent modules. This file intentionally re-exports only the concrete
``HttpUser`` classes Locust should discover.

Usage:
    cd tests/stress && locust
    uv run locust -f tests/stress/locustfile.py --host http://localhost:7001

For staged load profiles, use ``locustfile_shaped.py`` instead.
"""

from locustsuite import (
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
