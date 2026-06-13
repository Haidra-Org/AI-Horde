# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.dependency_runtime import (
    AUTO_DEPS_MODE,
    DEPS_MODE_ENV,
    EXTERNAL_DEPS_MODE,
    REQUIRE_TEST_DEPS_ENV,
    HordeTestRuntime,
    assert_safe_test_target,
    build_test_runtime,
    normalize_deps_mode,
    selected_object_store_tests,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--test-deps",
        dest="test_deps",
        action="store",
        choices=(AUTO_DEPS_MODE, EXTERNAL_DEPS_MODE),
        default=normalize_deps_mode(os.environ.get(DEPS_MODE_ENV, AUTO_DEPS_MODE)),
        help=(
            "How tests obtain Postgres/object-storage dependencies: "
            "'auto' prefers reachable external services and otherwise provisions testcontainers; "
            "'external' never provisions containers and only uses caller-provided services. "
        ),
    )


@pytest.fixture(scope="session")
def CIVERSION() -> str:
    return "0.1.1"


@pytest.fixture(scope="session")
def horde_test_runtime(request: pytest.FixtureRequest) -> Iterator[HordeTestRuntime]:
    runtime = build_test_runtime(
        deps_mode=request.config.getoption("test_deps"),
        needs_object_store=selected_object_store_tests(request),
    )

    try:
        yield runtime
    finally:
        runtime.close()


def _skip_or_fail_missing_dependency(message: str) -> None:
    if os.environ.get(REQUIRE_TEST_DEPS_ENV) == "1":
        pytest.fail(message)

    pytest.skip(message, allow_module_level=False)


@pytest.fixture(scope="session")
def pg_dsn(horde_test_runtime: HordeTestRuntime) -> str:
    dsn = horde_test_runtime.postgres_dsn
    if dsn is None:
        _skip_or_fail_missing_dependency(
            horde_test_runtime.postgres_skip_reason or "Tests require a reachable Postgres backend.",
        )

    if not horde_test_runtime.postgres_is_managed:
        assert_safe_test_target(dsn)

    return dsn


@pytest.fixture(scope="session")
def object_store_ready(horde_test_runtime: HordeTestRuntime) -> None:
    if horde_test_runtime.object_store_available:
        return

    _skip_or_fail_missing_dependency(
        horde_test_runtime.object_store_skip_reason
        or "Object-storage integration tests require a managed or external S3-compatible backend.",
    )
