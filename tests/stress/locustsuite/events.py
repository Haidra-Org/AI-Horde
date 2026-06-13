# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust event listeners, CLI arguments, and target preflight."""
# ruff: noqa: I001

import logging
import uuid

from locust import events
import requests

from .config import _config
from .helpers import _parse_csv

logger = logging.getLogger(__name__)


@events.init_command_line_parser.add_listener
def add_ai_horde_arguments(parser):
    group = parser.add_argument_group("AI Horde Stress Test")

    # API keys
    group.add_argument(
        "--requestor-api-keys",
        type=str,
        env_var="HORDE_REQUESTOR_API_KEYS",
        default="",
        help="Comma-separated API keys for requestor users",
    )
    group.add_argument(
        "--worker-api-keys",
        type=str,
        env_var="HORDE_WORKER_API_KEYS",
        default="",
        help="Comma-separated API keys for worker users",
    )
    group.add_argument(
        "--anonymous-api-key",
        type=str,
        env_var="HORDE_ANONYMOUS_API_KEY",
        default="0000000000",
        help="API key used for anonymous/unauthenticated requests",
    )

    # Models & identity
    group.add_argument(
        "--horde-models",
        type=str,
        env_var="HORDE_MODELS",
        default="Fustercluck,AlbedoBase XL (SDXL),stable_diffusion,waifu_diffusion",
        help="Comma-separated list of model names to request/serve",
    )
    group.add_argument(
        "--client-agent",
        type=str,
        env_var="HORDE_CLIENT_AGENT",
        default="aihorde_stress_test:1.0.0:(discord)stress_tester",
        help="Client-Agent header value sent with all requests",
    )

    # Generation parameters (standard requests)
    group.add_argument(
        "--gen-width",
        type=int,
        env_var="HORDE_GEN_WIDTH",
        default=512,
        help="Default image width for standard generation requests",
    )
    group.add_argument(
        "--gen-height",
        type=int,
        env_var="HORDE_GEN_HEIGHT",
        default=512,
        help="Default image height for standard generation requests",
    )
    group.add_argument(
        "--gen-steps",
        type=int,
        env_var="HORDE_GEN_STEPS",
        default=20,
        help="Default sampling steps for standard generation requests",
    )
    group.add_argument(
        "--gen-cfg-scale",
        type=float,
        env_var="HORDE_GEN_CFG_SCALE",
        default=7.0,
        help="Default CFG scale for generation requests",
    )

    # Generation parameters (large/high-res requests)
    group.add_argument(
        "--large-gen-width",
        type=int,
        env_var="HORDE_LARGE_GEN_WIDTH",
        default=1024,
        help="Image width for large/high-res generation requests",
    )
    group.add_argument(
        "--large-gen-height",
        type=int,
        env_var="HORDE_LARGE_GEN_HEIGHT",
        default=1024,
        help="Image height for large/high-res generation requests",
    )
    group.add_argument(
        "--large-gen-steps",
        type=int,
        env_var="HORDE_LARGE_GEN_STEPS",
        default=30,
        help="Sampling steps for large/high-res generation requests",
    )

    # Worker simulation
    group.add_argument(
        "--worker-max-pixels",
        type=int,
        env_var="HORDE_WORKER_MAX_PIXELS",
        default=4194304,
        help="Max pixels advertised by simulated workers",
    )
    group.add_argument(
        "--worker-bridge-agent",
        type=str,
        env_var="HORDE_WORKER_BRIDGE_AGENT",
        default="AI Horde Worker reGen:9.0.1-stress:https://github.com/Haidra-Org/horde-worker-reGen",
        help="Bridge agent string for simulated workers",
    )
    group.add_argument(
        "--sim-gen-time-min",
        type=float,
        env_var="HORDE_SIM_GEN_TIME_MIN",
        default=4.0,
        help="Minimum simulated generation time in seconds",
    )
    group.add_argument(
        "--sim-gen-time-max",
        type=float,
        env_var="HORDE_SIM_GEN_TIME_MAX",
        default=120.0,
        help="Maximum simulated generation time in seconds",
    )

    # User behavior
    group.add_argument(
        "--anon-chance-poller",
        type=float,
        env_var="HORDE_ANON_CHANCE_POLLER",
        default=0.75,
        help="Probability (0.0-1.0) that a StatusPoller acts as anonymous",
    )
    group.add_argument(
        "--anon-chance-requester",
        type=float,
        env_var="HORDE_ANON_CHANCE_REQUESTER",
        default=0.65,
        help="Probability (0.0-1.0) that a RequestGenerator acts as anonymous",
    )

    # Auto-bootstrap of API keys by POSTing to /register. Works only against local
    # instances where RECAPTCHA_SECRET_KEY is not set. Fills in any requestor/worker
    # key slots that were NOT supplied explicitly via --requestor-api-keys /
    # --worker-api-keys so CI/local runs don't need a separate gen_api_keys.py step.
    group.add_argument(
        "--bootstrap-requestors",
        type=int,
        env_var="HORDE_BOOTSTRAP_REQUESTORS",
        default=4,
        help="Auto-register N requestor users at test start (0 to disable). Ignored if --requestor-api-keys is set.",
    )
    group.add_argument(
        "--bootstrap-workers",
        type=int,
        env_var="HORDE_BOOTSTRAP_WORKERS",
        default=30,
        help=(
            "Auto-register N worker-owner users at test start (0 to disable). "
            "Ignored if --worker-api-keys is set. Should be >= ceil(worker_users / 3) "
            "because untrusted users are capped at 3 distinct workers each."
        ),
    )
    group.add_argument(
        "--bootstrap-fail-hard",
        action="store_true",
        env_var="HORDE_BOOTSTRAP_FAIL_HARD",
        default=False,
        help="Abort the test run if auto-registration fails (e.g. reCAPTCHA enabled). By default we fall back to anon.",
    )

    # Per-class fixed user counts. When > 0 the corresponding User class is
    # spawned with exactly that many concurrent users, *bypassing* the global
    # `weight` distribution (Locust's `fixed_count` mechanism). This lets you
    # decouple requestor:worker ratios per gentype so you can deliberately
    # build up a queue (e.g. 200 image requestors against 2 image workers with
    # --sim-gen-time-min/max bumped to several minutes).
    #
    # Any class left at 0 falls back to the weight-based distribution against
    # whatever spawn budget remains after the fixed counts are subtracted from
    # `-u`. If the sum of fixed counts exceeds `-u`, Locust will simply spawn
    # the fixed counts and ignore the global cap, so set `-u` accordingly.
    fixed_count_group = parser.add_argument_group("AI Horde Stress Test - per-class fixed counts")
    for _flag, _envvar, _help in (
        ("--image-requestors", "HORDE_IMAGE_REQUESTORS", "Concurrent RequestGenerator users (image POST /generate/async)"),
        ("--image-workers", "HORDE_IMAGE_WORKERS", "Concurrent WorkerSimulator users (image POST /generate/pop+submit)"),
        ("--text-requestors", "HORDE_TEXT_REQUESTORS", "Concurrent TextRequester users"),
        ("--text-workers", "HORDE_TEXT_WORKERS", "Concurrent TextWorkerSimulator users"),
        ("--interrogate-requestors", "HORDE_INTERROGATE_REQUESTORS", "Concurrent InterrogationRequester users"),
        ("--interrogate-workers", "HORDE_INTERROGATE_WORKERS", "Concurrent InterrogationWorkerSimulator users"),
        ("--status-pollers", "HORDE_STATUS_POLLERS", "Concurrent StatusPoller users (image /check + /status)"),
        (
            "--hot-path-requestors",
            "HORDE_HOT_PATH_REQUESTORS",
            "Concurrent HotPathRequester users (identical-payload image POST /async)",
        ),
        ("--meta-browsers", "HORDE_META_BROWSERS", "Concurrent MetaBrowser users (read-only meta endpoints)"),
        ("--misuse-users", "HORDE_MISUSE_USERS", "Concurrent MisuseUser users (4xx validation paths)"),
    ):
        fixed_count_group.add_argument(_flag, type=int, env_var=_envvar, default=0, help=_help + " (0 = use weight-based distribution)")

    # Polling behavior. We want to mirror real clients: submit, then aggressively
    # poll /check until done, only escalating to /status (which is rate-limited at
    # 10/min/path) on completion. Defaults match a typical SDK loop (~1-3 Hz checks
    # while ≤ 4 outstanding requests in flight per client).
    poll_group = parser.add_argument_group("AI Horde Stress Test - requestor poll loop")
    poll_group.add_argument(
        "--requestor-max-pending",
        type=int,
        env_var="HORDE_REQUESTOR_MAX_PENDING",
        default=4,
        help="Max in-flight /async requests per RequestGenerator before they pause submitting and only poll",
    )
    poll_group.add_argument(
        "--requestor-wait-min",
        type=float,
        env_var="HORDE_REQUESTOR_WAIT_MIN",
        default=0.2,
        help="Min wait between RequestGenerator/StatusPoller task ticks (seconds)",
    )
    poll_group.add_argument(
        "--requestor-wait-max",
        type=float,
        env_var="HORDE_REQUESTOR_WAIT_MAX",
        default=1.0,
        help="Max wait between RequestGenerator/StatusPoller task ticks (seconds)",
    )
    poll_group.add_argument(
        "--status-fetch-on-done",
        action="store_true",
        env_var="HORDE_STATUS_FETCH_ON_DONE",
        default=True,
        help="When a /check returns done=true, also fetch /status (mirrors real client behavior)",
    )

    # Target/external dependency preflight. Locust remains a black-box workload
    # runner: the Horde app and its Postgres/Redis/R2 dependencies are stood up
    # outside this process, while these flags make the target readiness contract
    # explicit at test start.
    preflight_group = parser.add_argument_group("AI Horde Stress Test - target preflight")
    preflight_group.add_argument(
        "--skip-preflight",
        action="store_true",
        env_var="HORDE_SKIP_PREFLIGHT",
        default=False,
        help="Skip the startup GET /api/v2/status/heartbeat readiness check.",
    )
    preflight_group.add_argument(
        "--preflight-fail-hard",
        action="store_true",
        env_var="HORDE_PREFLIGHT_FAIL_HARD",
        default=False,
        help="Abort the run if the target heartbeat check fails.",
    )
    preflight_group.add_argument(
        "--preflight-timeout",
        type=float,
        env_var="HORDE_PREFLIGHT_TIMEOUT",
        default=10.0,
        help="Seconds to wait for the startup heartbeat check.",
    )

    # Optional staged load profile. This is consumed only by locustfile_shaped.py,
    # keeping the default locustfile compatible with classic -u/-r operation.
    shape_group = parser.add_argument_group("AI Horde Stress Test - staged load shape")
    shape_group.add_argument(
        "--stress-shape-profile",
        choices=("smoke", "baseline", "spike"),
        env_var="HORDE_STRESS_SHAPE_PROFILE",
        default="baseline",
        help="Staged load profile used by locustfile_shaped.py.",
    )
    shape_group.add_argument(
        "--stress-shape-scale",
        type=float,
        env_var="HORDE_STRESS_SHAPE_SCALE",
        default=1.0,
        help="Multiplier applied to the selected staged load profile's user counts and spawn rates.",
    )


def _register_test_user(base_url: str, role: str) -> str | None:
    """POST the /register form to mint a fresh API key.

    Returns the raw API key on success, ``None`` on failure (e.g. reCAPTCHA is
    enabled on this deployment, or the form changed shape). The template ships
    the key inside ``<code class=\"ah-api-key\">KEY</code>``.
    """
    username = f"stress_{role}_{uuid.uuid4().hex[:8]}"
    try:
        resp = requests.post(
            f"{base_url}/register",
            data={"username": username},
            allow_redirects=False,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Auto-register POST /register failed: %s", exc)
        return None
    if resp.status_code != 200:
        logger.warning(
            "Auto-register for '%s' returned status %d (body: %.120r)",
            username,
            resp.status_code,
            resp.text[:120],
        )
        return None
    marker = 'class="ah-api-key">'
    idx = resp.text.find(marker)
    if idx == -1:
        logger.warning(
            "Auto-register response did not contain an api-key marker; reCAPTCHA likely enabled.",
        )
        return None
    start = idx + len(marker)
    end = resp.text.find("<", start)
    if end == -1:
        return None
    key = resp.text[start:end].strip()
    return key or None


def _autoregister_keys(host: str, count: int, role: str) -> list[str]:
    keys: list[str] = []
    for _ in range(count):
        key = _register_test_user(host, role)
        if key is None:
            break
        keys.append(key)
    return keys


def _preflight_target(host: str, timeout: float, fail_hard: bool, environment) -> None:
    heartbeat_url = f"{host}/api/v2/status/heartbeat"
    try:
        resp = requests.get(heartbeat_url, timeout=timeout)
    except requests.RequestException as exc:
        message = f"Target preflight failed for {heartbeat_url}: {exc}"
        if fail_hard:
            environment.runner.quit()
            raise RuntimeError(message) from exc
        logger.warning(message)
        return

    if resp.status_code >= 400:
        message = f"Target preflight returned HTTP {resp.status_code} for {heartbeat_url}: {resp.text[:120]!r}"
        if fail_hard:
            environment.runner.quit()
            raise RuntimeError(message)
        logger.warning(message)
        return

    logger.info("Target preflight OK: %s", heartbeat_url)


@events.test_start.add_listener
def on_test_start(environment, **kw):
    opts = environment.parsed_options
    _config["models"] = _parse_csv(opts.horde_models)
    _config["requestor_api_keys"] = _parse_csv(opts.requestor_api_keys)
    _config["worker_api_keys"] = _parse_csv(opts.worker_api_keys)
    _config["anonymous_api_key"] = opts.anonymous_api_key
    _config["client_agent"] = opts.client_agent

    # Auto-bootstrap keys if not supplied
    host = (environment.host or "").rstrip("/") or "http://localhost:7001"
    if not opts.skip_preflight:
        _preflight_target(host, opts.preflight_timeout, opts.preflight_fail_hard, environment)
    if not _config["requestor_api_keys"] and opts.bootstrap_requestors > 0:
        logger.info("Auto-registering %d requestor users at %s", opts.bootstrap_requestors, host)
        _config["requestor_api_keys"] = _autoregister_keys(host, opts.bootstrap_requestors, "req")
        if not _config["requestor_api_keys"] and opts.bootstrap_fail_hard:
            environment.runner.quit()
            raise RuntimeError("Auto-registration of requestor users failed and --bootstrap-fail-hard is set.")
    if not _config["worker_api_keys"] and opts.bootstrap_workers > 0:
        logger.info("Auto-registering %d worker-owner users at %s", opts.bootstrap_workers, host)
        _config["worker_api_keys"] = _autoregister_keys(host, opts.bootstrap_workers, "wrk")
        if not _config["worker_api_keys"] and opts.bootstrap_fail_hard:
            environment.runner.quit()
            raise RuntimeError("Auto-registration of worker users failed and --bootstrap-fail-hard is set.")

    if not _config["requestor_api_keys"]:
        logger.warning(
            "No requestor API keys available (neither supplied nor auto-registered). "
            "StatusPoller and RequestGenerator will use the anonymous API key.",
        )
    if not _config["worker_api_keys"]:
        logger.warning(
            "No worker API keys available. WorkerSimulator users will be unable to function.",
        )
    if not _config["models"]:
        logger.warning("No --horde-models provided. Requests will be sent with an empty model list.")

    logger.info(
        "Stress test config: %d models, %d requestor keys, %d worker keys",
        len(_config["models"]),
        len(_config["requestor_api_keys"]),
        len(_config["worker_api_keys"]),
    )

    # Apply per-class fixed_count overrides. Locust reads `fixed_count` from
    # each User class on test start; once we mutate the attribute the runner's
    # spawner respects it. We do this here rather than at import time so the
    # CLI arguments are guaranteed to be parsed.
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

    _fixed_count_overrides = (
        (StatusPoller, "status_pollers"),
        (RequestGenerator, "image_requestors"),
        (WorkerSimulator, "image_workers"),
        (TextRequester, "text_requestors"),
        (TextWorkerSimulator, "text_workers"),
        (InterrogationRequester, "interrogate_requestors"),
        (InterrogationWorkerSimulator, "interrogate_workers"),
        (HotPathRequester, "hot_path_requestors"),
        (MetaBrowser, "meta_browsers"),
        (MisuseUser, "misuse_users"),
    )
    fixed_total = 0
    for cls, attr in _fixed_count_overrides:
        n = int(getattr(opts, attr, 0) or 0)
        if n > 0:
            cls.fixed_count = n
            fixed_total += n
            logger.info("Pinning %s.fixed_count = %d", cls.__name__, n)
    if fixed_total:
        logger.info(
            "Per-class fixed counts total %d users; remaining -u budget will be distributed by weight across the unpinned User classes.",
            fixed_total,
        )
