# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from tests import dependency_runtime
from tests.dependency_runtime import AUTO_DEPS_MODE, EXTERNAL_DEPS_MODE, HordeTestRuntime, configure_object_store


def _set_complete_object_store_env(monkeypatch) -> None:
    for env_var in dependency_runtime.REQUIRED_OBJECT_STORE_ENV:
        monkeypatch.setenv(env_var, "test-value")
    monkeypatch.setenv("R2_TRANSIENT_ACCOUNT", "http://127.0.0.1:1")
    monkeypatch.setenv("R2_PERMANENT_ACCOUNT", "http://127.0.0.1:1")


def test_auto_object_store_falls_back_when_complete_env_is_unreachable(monkeypatch) -> None:
    _set_complete_object_store_env(monkeypatch)
    monkeypatch.setattr(dependency_runtime, "unreachable_object_store_env", lambda: ["R2_TRANSIENT_ACCOUNT"])

    started = False

    def fake_start_managed_object_store(runtime: HordeTestRuntime) -> None:
        nonlocal started
        started = True
        runtime.object_store_available = True
        runtime.set_env("R2_TRANSIENT_ACCOUNT", "http://127.0.0.1:3900")

    monkeypatch.setattr(dependency_runtime, "start_managed_object_store", fake_start_managed_object_store)

    runtime = HordeTestRuntime(deps_mode=AUTO_DEPS_MODE, needs_object_store=True)
    configure_object_store(runtime)

    assert started is True
    assert runtime.object_store_available is True
    assert runtime.object_store_skip_reason is None


def test_external_object_store_reports_unreachable_complete_env(monkeypatch) -> None:
    _set_complete_object_store_env(monkeypatch)
    monkeypatch.setattr(dependency_runtime, "unreachable_object_store_env", lambda: ["R2_TRANSIENT_ACCOUNT"])

    runtime = HordeTestRuntime(deps_mode=EXTERNAL_DEPS_MODE, needs_object_store=True)
    configure_object_store(runtime)

    assert runtime.object_store_available is False
    assert runtime.object_store_skip_reason is not None
    assert "Unreachable env vars: R2_TRANSIENT_ACCOUNT" in runtime.object_store_skip_reason
