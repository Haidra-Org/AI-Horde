# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared dependency orchestration for pytest suites."""

from __future__ import annotations

import importlib
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import pytest
import sqlalchemy

ALLOWED_LOCAL_TEST_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1", "postgres", "db"}
NONLOCAL_DB_OVERRIDE_ENV = "AI_HORDE_ALLOW_NONLOCAL_TEST_DB"
DEPS_MODE_ENV = "AI_HORDE_TEST_DEPS_MODE"
REQUIRE_TEST_DEPS_ENV = "AI_HORDE_REQUIRE_TEST_DEPS"
AUTO_DEPS_MODE = "auto"
EXTERNAL_DEPS_MODE = "external"

TEST_POSTGRES_IMAGE = "ghcr.io/haidra-org/ai-horde-postgres:latest"
TEST_POSTGRES_USER = "postgres"
TEST_POSTGRES_PASSWORD = "postgres"
TEST_POSTGRES_DB = "postgres"
TEST_POSTGRES_PORT = 5432

TEST_GARAGE_IMAGE = "dxflrs/garage:v2.1.0"
TEST_GARAGE_CONFIG_PATH = Path(__file__).resolve().parent / "garage" / "garage.toml"
TEST_GARAGE_S3_PORT = 3900
TEST_GARAGE_RPC_PORT = 3901
TEST_GARAGE_ADMIN_PORT = 3903
TEST_GARAGE_ACCESS_KEY_NAME = "ai-horde-tests"
TEST_GARAGE_CAPACITY = "1G"
TEST_GARAGE_TRANSIENT_BUCKET = "stable-horde"
TEST_GARAGE_PERMANENT_BUCKET = "stable-horde"
TEST_GARAGE_SOURCE_IMAGE_BUCKET = "stable-horde-source-images"
TEST_GARAGE_PROMPTS_BUCKET = "prompts"
REQUIRED_OBJECT_STORE_ENV = (
    "R2_TRANSIENT_ACCOUNT",
    "R2_PERMANENT_ACCOUNT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "SHARED_AWS_ACCESS_ID",
    "SHARED_AWS_ACCESS_KEY",
)
OBJECT_STORE_ENDPOINT_ENV = ("R2_TRANSIENT_ACCOUNT", "R2_PERMANENT_ACCOUNT")
DOCKER_CONFIG_BASENAME = "config.json"


@dataclass
class HordeTestRuntime:
    """Runtime resources shared by unit and integration pytest namespaces."""

    deps_mode: str
    needs_object_store: bool
    postgres_dsn: str | None = None
    postgres_is_managed: bool = False
    postgres_skip_reason: str | None = None
    object_store_available: bool = False
    object_store_skip_reason: str | None = None
    _stack: ExitStack = field(default_factory=ExitStack, repr=False)
    _saved_env: dict[str, str | None] = field(default_factory=dict, repr=False)

    def set_env(self, key: str, value: str) -> None:
        if key not in self._saved_env:
            self._saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    def restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._saved_env.clear()

    def close(self) -> None:
        self.restore_env()
        try:
            self._stack.close()
        except BaseExceptionGroup as exc_group:
            remaining = exc_group.subgroup(lambda raised_exc: not should_ignore_cleanup_error(raised_exc))
            if remaining is not None:
                raise remaining
        except Exception as raised_exc:
            if not should_ignore_cleanup_error(raised_exc):
                raise


def normalize_deps_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {AUTO_DEPS_MODE, EXTERNAL_DEPS_MODE}:
        return normalized
    return AUTO_DEPS_MODE


def selected_object_store_tests(request: pytest.FixtureRequest) -> bool:
    return any(item.get_closest_marker("object_storage") is not None for item in request.session.items)


def build_test_runtime(deps_mode: str, needs_object_store: bool) -> HordeTestRuntime:
    runtime = HordeTestRuntime(deps_mode=deps_mode, needs_object_store=needs_object_store)
    runtime.set_env("LOGFIRE_IGNORE_NO_CONFIG", "1")

    if runtime.deps_mode == AUTO_DEPS_MODE:
        prepare_managed_docker_env(runtime)

    configure_postgres(runtime)
    configure_redis(runtime)
    configure_object_store(runtime)
    if runtime.object_store_available:
        refresh_r2_module_from_env()

    return runtime


def resolve_postgres_dsn() -> str:
    """Return SQLAlchemy DSN following this repository's env-var convention."""
    user = os.environ.get("PGUSER", TEST_POSTGRES_USER)
    password = os.environ.get("PGPASSWORD", TEST_POSTGRES_PASSWORD)
    host_port_db = os.environ.get("POSTGRES_URL", "localhost:5432/postgres")
    return f"postgresql+psycopg2://{user}:{password}@{host_port_db}"


def postgres_reachable(dsn: str) -> bool:
    try:
        engine = sqlalchemy.create_engine(dsn, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


def assert_safe_test_target(dsn: str, suite_name: str = "tests") -> None:
    """Abort when tests point at a non-local DB unless explicitly allowed."""
    host = (sqlalchemy.engine.make_url(dsn).host or "").lower()
    if host in ALLOWED_LOCAL_TEST_DB_HOSTS:
        return

    if os.environ.get(NONLOCAL_DB_OVERRIDE_ENV) == "1":
        return

    raise RuntimeError(
        f"Refusing to run DB-backed {suite_name} against non-local host "
        f"{host!r}. Set {NONLOCAL_DB_OVERRIDE_ENV}=1 to override intentionally.",
    )


def new_test_schema_name(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def create_schema(dsn: str, schema_name: str) -> None:
    engine = sqlalchemy.create_engine(dsn, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text(f'CREATE SCHEMA "{schema_name}"'))
    finally:
        engine.dispose()


def drop_schema(dsn: str, schema_name: str) -> None:
    engine = sqlalchemy.create_engine(dsn, pool_pre_ping=True)
    try:
        try:
            with engine.begin() as conn:
                conn.execute(sqlalchemy.text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        except sqlalchemy.exc.OperationalError:
            if postgres_reachable(dsn):
                raise
    finally:
        engine.dispose()


def redis_reachable(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def wait_until(description: str, predicate: Callable[[], bool], timeout: float = 60.0, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as raised_exc:  # pragma: no cover - exercised only on startup failures
            last_error = raised_exc
        time.sleep(interval)

    if last_error is not None:
        raise RuntimeError(f"{description} did not become ready in time: {last_error}") from last_error
    raise RuntimeError(f"{description} did not become ready in time.")


def should_ignore_cleanup_error(raised_exc: BaseException) -> bool:
    exc_type = type(raised_exc)
    if exc_type.__module__.startswith("docker.errors") and exc_type.__name__ == "NotFound":
        return True

    if isinstance(raised_exc, sqlalchemy.exc.OperationalError):
        return True

    return False


def load_testcontainers():
    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.redis import RedisContainer
    except ModuleNotFoundError as raised_exc:  # pragma: no cover - depends on local env
        raise RuntimeError(
            "Automatic test dependency provisioning requires testcontainers. Install dev dependencies or rerun with --test-deps=external.",
        ) from raised_exc

    return DockerContainer, RedisContainer


def prepare_managed_docker_env(runtime: HordeTestRuntime) -> None:
    """Use an isolated Docker config so public-image pulls ignore broken host helpers."""
    temp_dir = runtime._stack.enter_context(tempfile.TemporaryDirectory(prefix="aihorde-testcontainers-"))
    temp_config_path = Path(temp_dir) / DOCKER_CONFIG_BASENAME

    docker_config: dict[str, object] = {}
    source_config_path = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker")) / DOCKER_CONFIG_BASENAME
    if source_config_path.exists():
        try:
            source_config = json.loads(source_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            source_config = {}

        if isinstance(source_config, dict):
            for key in ("auths", "HttpHeaders", "proxies"):
                value = source_config.get(key)
                if isinstance(value, dict):
                    docker_config[key] = value

    temp_config_path.write_text(json.dumps(docker_config), encoding="utf-8")
    runtime.set_env("DOCKER_CONFIG", temp_dir)


def start_managed_postgres(runtime: HordeTestRuntime) -> None:
    DockerContainer, _ = load_testcontainers()
    postgres = (
        DockerContainer(TEST_POSTGRES_IMAGE)
        .with_env("POSTGRES_USER", TEST_POSTGRES_USER)
        .with_env("POSTGRES_PASSWORD", TEST_POSTGRES_PASSWORD)
        .with_env("POSTGRES_DB", TEST_POSTGRES_DB)
        .with_env("POSTGRES_HOST_AUTH_METHOD", "trust")
        .with_command(f"postgres -c cron.database_name={TEST_POSTGRES_DB}")
        .with_exposed_ports(TEST_POSTGRES_PORT)
    )
    postgres = runtime._stack.enter_context(postgres)
    host = postgres.get_container_host_ip()
    port = str(postgres.get_exposed_port(TEST_POSTGRES_PORT))
    dsn = f"postgresql+psycopg2://{TEST_POSTGRES_USER}:{TEST_POSTGRES_PASSWORD}@{host}:{port}/{TEST_POSTGRES_DB}"
    wait_until("managed Postgres", lambda: postgres_reachable(dsn))

    runtime.postgres_dsn = dsn
    runtime.postgres_is_managed = True


def configure_postgres(runtime: HordeTestRuntime) -> None:
    external_dsn = resolve_postgres_dsn()
    if postgres_reachable(external_dsn):
        runtime.postgres_dsn = external_dsn
        return

    if runtime.deps_mode == EXTERNAL_DEPS_MODE:
        runtime.postgres_skip_reason = (
            f"Postgres unreachable at {external_dsn!r}. Provide an external test database or rerun with {DEPS_MODE_ENV}={AUTO_DEPS_MODE}."
        )
        return

    try:
        start_managed_postgres(runtime)
    except Exception as raised_exc:  # pragma: no cover - depends on local Docker availability
        runtime.postgres_skip_reason = f"Postgres unreachable at {external_dsn!r}, and automatic provisioning failed: {raised_exc}"


def configure_redis(runtime: HordeTestRuntime) -> None:
    try:
        import fakeredis  # noqa: F401
    except ModuleNotFoundError:
        pass
    else:
        return

    redis_host = os.environ.get("REDIS_IP", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    if redis_reachable(redis_host, redis_port):
        return

    if runtime.deps_mode == EXTERNAL_DEPS_MODE:
        return

    try:
        _, RedisContainer = load_testcontainers()
        redis_container = runtime._stack.enter_context(RedisContainer())
        host = redis_container.get_container_host_ip()
        port = str(redis_container.get_exposed_port(redis_container.port))
        wait_until("managed Redis", lambda: redis_reachable(host, int(port)))

        runtime.set_env("REDIS_IP", host)
        runtime.set_env("REDIS_PORT", port)
        runtime.set_env("REDIS_SERVERS", f'["{host}"]')
    except Exception:  # pragma: no cover - depends on local Docker availability
        return


def missing_object_store_env() -> list[str]:
    return [env_var for env_var in REQUIRED_OBJECT_STORE_ENV if not os.getenv(env_var)]


def object_store_endpoint_reachable(endpoint_url: str) -> bool:
    parsed_url = urlparse(endpoint_url)
    if parsed_url.hostname is None or parsed_url.scheme not in {"http", "https"}:
        return False
    try:
        port = parsed_url.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parsed_url.scheme == "https" else 80
    try:
        with socket.create_connection((parsed_url.hostname, port), timeout=2):
            return True
    except OSError:
        return False


def unreachable_object_store_env() -> list[str]:
    return [env_var for env_var in OBJECT_STORE_ENDPOINT_ENV if not object_store_endpoint_reachable(os.environ[env_var])]


def container_exec(container, command: list[str]) -> str:
    result = container.exec(command)
    exit_code = getattr(result, "exit_code", None)
    output = getattr(result, "output", b"")

    if exit_code is None and isinstance(result, tuple):
        exit_code, output = result

    text = output.decode() if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        raise RuntimeError(f"Container command {' '.join(command)!r} failed: {text.strip()}")
    return text


def parse_garage_node_id(output: str) -> str | None:
    node_id_match = re.search(r"^([0-9a-f]{16,64})@", output, flags=re.MULTILINE)
    if node_id_match:
        return node_id_match.group(1)

    status_match = re.search(r"^([0-9a-f]{16,64})\s", output, flags=re.MULTILINE)
    if status_match:
        return status_match.group(1)

    return None


def garage_health_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as response:
            return response.status == 200
    except URLError:
        return False


def start_managed_object_store(runtime: HordeTestRuntime) -> None:
    DockerContainer, _ = load_testcontainers()

    # The managed Garage container is ephemeral (fresh volume per run), so mint
    # throwaway secrets in-process rather than committing fixed values. Garage
    # reads these from the environment, overriding the secret-free garage.toml.
    rpc_secret = secrets.token_hex(32)
    admin_token = secrets.token_hex(32)
    metrics_token = secrets.token_hex(32)
    access_key_id = f"GK{secrets.token_hex(12)}"
    secret_key = secrets.token_hex(32)

    garage = (
        DockerContainer(TEST_GARAGE_IMAGE)
        .with_volume_mapping(str(TEST_GARAGE_CONFIG_PATH), "/etc/garage.toml", "ro")
        .with_env("GARAGE_RPC_SECRET", rpc_secret)
        .with_env("GARAGE_ADMIN_TOKEN", admin_token)
        .with_env("GARAGE_METRICS_TOKEN", metrics_token)
        .with_exposed_ports(TEST_GARAGE_S3_PORT, TEST_GARAGE_RPC_PORT, TEST_GARAGE_ADMIN_PORT)
    )
    garage = runtime._stack.enter_context(garage)

    host = garage.get_container_host_ip()
    s3_port = str(garage.get_exposed_port(TEST_GARAGE_S3_PORT))
    admin_port = str(garage.get_exposed_port(TEST_GARAGE_ADMIN_PORT))

    wait_until(
        "managed Garage CLI",
        lambda: getattr(garage.exec(["/garage", "-c", "/etc/garage.toml", "status"]), "exit_code", 1) == 0,
    )

    node_id_output = container_exec(garage, ["/garage", "-c", "/etc/garage.toml", "node", "id"])
    node_id = parse_garage_node_id(node_id_output)
    if node_id is None:
        node_id = parse_garage_node_id(container_exec(garage, ["/garage", "-c", "/etc/garage.toml", "status"]))
    if node_id is None:
        raise RuntimeError("Could not parse Garage node ID for layout assignment.")

    container_exec(
        garage,
        [
            "/garage",
            "-c",
            "/etc/garage.toml",
            "layout",
            "assign",
            node_id,
            "--zone",
            "dc1",
            "--capacity",
            TEST_GARAGE_CAPACITY,
        ],
    )
    container_exec(garage, ["/garage", "-c", "/etc/garage.toml", "layout", "apply", "--version", "1"])
    container_exec(
        garage,
        [
            "/garage",
            "-c",
            "/etc/garage.toml",
            "key",
            "import",
            "--yes",
            "-n",
            TEST_GARAGE_ACCESS_KEY_NAME,
            access_key_id,
            secret_key,
        ],
    )

    for bucket in sorted(
        {
            TEST_GARAGE_TRANSIENT_BUCKET,
            TEST_GARAGE_PERMANENT_BUCKET,
            TEST_GARAGE_SOURCE_IMAGE_BUCKET,
            TEST_GARAGE_PROMPTS_BUCKET,
        },
    ):
        container_exec(garage, ["/garage", "-c", "/etc/garage.toml", "bucket", "create", bucket])
        container_exec(
            garage,
            [
                "/garage",
                "-c",
                "/etc/garage.toml",
                "bucket",
                "allow",
                "--read",
                "--write",
                "--owner",
                bucket,
                "--key",
                TEST_GARAGE_ACCESS_KEY_NAME,
            ],
        )

    wait_until(
        "managed Garage admin health endpoint",
        lambda: garage_health_ready(f"http://{host}:{admin_port}/health"),
        timeout=30.0,
    )

    runtime.set_env("AWS_ACCESS_KEY_ID", access_key_id)
    runtime.set_env("AWS_SECRET_ACCESS_KEY", secret_key)
    runtime.set_env("SHARED_AWS_ACCESS_ID", access_key_id)
    runtime.set_env("SHARED_AWS_ACCESS_KEY", secret_key)
    runtime.set_env("R2_TRANSIENT_ACCOUNT", f"http://{host}:{s3_port}")
    runtime.set_env("R2_PERMANENT_ACCOUNT", f"http://{host}:{s3_port}")
    runtime.set_env("R2_TRANSIENT_BUCKET", TEST_GARAGE_TRANSIENT_BUCKET)
    runtime.set_env("R2_PERMANENT_BUCKET", TEST_GARAGE_PERMANENT_BUCKET)
    runtime.set_env("R2_SOURCE_IMAGE_BUCKET", TEST_GARAGE_SOURCE_IMAGE_BUCKET)
    runtime.object_store_available = True


def configure_object_store(runtime: HordeTestRuntime) -> None:
    if not runtime.needs_object_store:
        return

    missing_env = missing_object_store_env()
    if not missing_env:
        unreachable_env = unreachable_object_store_env()
        if unreachable_env and runtime.deps_mode == EXTERNAL_DEPS_MODE:
            runtime.object_store_skip_reason = (
                "Object-storage integration tests require reachable S3-compatible endpoints. "
                f"Unreachable env vars: {', '.join(unreachable_env)}"
            )
            return
        if not unreachable_env:
            runtime.object_store_available = True
            return

    elif runtime.deps_mode == EXTERNAL_DEPS_MODE:
        runtime.object_store_skip_reason = (
            "Object-storage integration tests require caller-provided S3-compatible endpoints and credentials. "
            f"Missing env vars: {', '.join(missing_env)}"
        )
        return

    try:
        start_managed_object_store(runtime)
    except Exception as raised_exc:  # pragma: no cover - depends on local Docker availability
        runtime.object_store_skip_reason = (
            f"Object-storage integration tests need S3-compatible storage, and automatic Garage provisioning failed: {raised_exc}"
        )


def refresh_r2_module_from_env() -> None:
    r2_module = sys.modules.get("horde.r2")
    if r2_module is not None:
        importlib.reload(r2_module)
