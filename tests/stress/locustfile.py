# SPDX-FileCopyrightText: 2026 Tazlin
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Locust stress test targeting the hot paths identified by instrumentation.

Exercises the most expensive code paths across the full AI-Horde API surface:

  Image generation (stable):
    POST /generate/async   -> prompt detection, WP creation, source image upload
    POST /generate/pop     -> get_sorted_wp, candidate evaluation, start_generation
    POST /generate/submit  -> set_generation (R2), record_contribution, webhook
    GET  /generate/check   -> wp_has_valid_workers, get_wp_queue_stats
    GET  /generate/status  -> check + full procgen details + R2 presigned URLs

  Text generation (kobold):
    POST /generate/text/async|pop|submit, GET /generate/text/status

  Interrogation (alchemy):
    POST /interrogate/async|pop|submit, GET /interrogate/status

  Meta / browse (read-only, cache-heavy in prod):
    GET /status/{heartbeat,models,performance,modes,news}
    GET /workers, /workers/[id], /workers/name/[name]
    GET /users, /users/[id], /find_user
    GET /teams, /stats/img/*, /stats/text/*

  Misuse (validates 4xx code paths — 5xx from any of these is a real bug):
    invalid API keys, empty prompts, oversized params, unknown models,
    bogus request IDs, missing-field payloads, self-kudos-transfer.

Both **hot** (repeated identical payload, cache-friendly) and **cold**
(randomized per-call) paths are exercised; tasks are tagged ``[hot]`` /
``[cold]`` in the Locust stats table.

Known-expected error responses (429 rate limit, 403 TooManyWorkers,
404 InvalidJobID, ProfaneWorkerName, WorkerMaintenance, ...) are reported
under ``… [expected]`` rows rather than as test failures.

Usage:
    pip install locust   # or:  uv pip install locust requests

    # Copy the example config and edit for your environment:
    cp tests/stress/locust.conf.example tests/stress/locust.conf

    # Run from the stress test directory (auto-discovers locust.conf):
    cd tests/stress && locust

    # Or from the repo root:
    locust -f tests/stress/locustfile.py --config tests/stress/locust.conf

    # Local dev (no reCAPTCHA): API keys are auto-registered at test start.
    uv run locust -f tests/stress/locustfile.py --host http://localhost:7001

    # Supply existing keys explicitly (disables auto-bootstrap for that role):
    locust -f tests/stress/locustfile.py \\
        --requestor-api-keys "key1,key2" \\
        --worker-api-keys "wkey1,wkey2,wkey3" \\
        --horde-models "Fustercluck,stable_diffusion"

    # Or via environment variables:
    HORDE_REQUESTOR_API_KEYS="key1,key2" HORDE_WORKER_API_KEYS="wkey1" locust ...

    # See all options:
    locust -f tests/stress/locustfile.py --help
"""
import base64
import logging
import random
import string
import time
import uuid

import requests
from locust import HttpUser, between, events, task
from locust.exception import RescheduleTask

logger = logging.getLogger(__name__)

# Tiny 1x1 transparent PNG for interrogation requests (raw base64, no data-URL
# prefix: /interrogate/async's validator expects either a URL or a bare base64
# payload).
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Parsed config populated at test start from CLI/env args
_config: dict = {}

# Response codes / API rcs that the AI-Horde API legitimately returns under
# load and which we therefore should NOT count as test failures. They get
# reported to Locust as successes (so they don't pollute the failure table)
# but are also tracked under a separate "[expected-…]" name so the operator
# can still see the rate-limit / maintenance / contention frequency in the
# Locust UI.
_EXPECTED_RC_RECOVER = {
    "ProfaneWorkerName",          # worker name happened to contain a banned token; pick a new one
    "WorkerMaintenance",          # the simulated worker was put in maintenance for dropping jobs
    "WorkerFlaggedMaintenance",   # the user was auto-flagged for suspicious activity
    "WorkerInviteOnly",           # public worker creation is invite-only on this deployment
    "TooManyWorkers",             # untrusted user exceeded the 3-worker cap: rotate to a different key
    "TooManyWorkersTrusted",      # trusted user exceeded the 20-worker cap
    "TooManySameIPs",             # the same IP is hosting too many workers
    "TooManyNewIPs",              # IP is too new to host workers yet
    "UnsafeIP",                   # IP flagged by countermeasures
    "AnonForbiddenWorker",        # attempted worker action with anon API key
    "PolymorphicNameConflict",    # worker name collides with a different worker_class
    "WrongCredentials",           # the stored API key doesn't own this worker name anymore
}


def _is_expected_rc(body_json: dict | None, rc_set: set[str]) -> bool:
    return bool(body_json and body_json.get("rc") in rc_set)


def _is_too_many_workers(body_json: dict | None) -> bool:
    """Match the 'untrusted users can only have up to 3 distinct workers' 403.

    Older AI-Horde deployments raise this with the generic rc='Forbidden', so
    we fall back to substring matching on the message body.
    """
    if not body_json:
        return False
    if body_json.get("rc") in ("TooManyWorkers", "TooManyWorkersTrusted"):
        return True
    msg = body_json.get("message") or ""
    return "distinct workers" in msg or "onboard more than 20 workers" in msg


def _safe_json(resp) -> dict | None:
    try:
        return resp.json()
    except Exception:
        return None


def _record_expected(environment, request_type: str, name: str, response_time_ms: float, response_length: int) -> None:
    """Record an \"expected failure\" sample under a *_expected name.

    The original request is already counted as success via ``resp.success()``,
    we just additionally surface the expected-failure rate so it shows up in
    the Locust statistics tab.
    """
    environment.events.request.fire(
        request_type=request_type,
        name=f"{name} [expected]",
        response_time=response_time_ms,
        response_length=response_length,
        exception=None,
        context={},
    )


def _handle_async_generate(resp, environment) -> str | None:
    """Common handling for POST /generate/async responses.

    Returns the request id on success, ``None`` otherwise. 429 (rate limit)
    and selected 403 rcs are treated as expected and trigger a back-off via
    ``RescheduleTask`` so Locust skips ahead to the next task instead of
    hammering the same endpoint.
    """
    name = resp.request_meta.get("name", "/api/v2/generate/async")
    if resp.ok:
        body = _safe_json(resp) or {}
        resp.success()
        return body.get("id")

    body = _safe_json(resp)
    if resp.status_code == 429:
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        # Honour the Retry-After header if present, else jittered back-off.
        retry_after = float(resp.headers.get("Retry-After") or random.uniform(2.0, 6.0))
        time.sleep(min(retry_after, 10.0))
        raise RescheduleTask()
    if resp.status_code == 403 and _is_expected_rc(body, {"WorkerInviteOnly"}):
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        raise RescheduleTask()

    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
    return None


def _parse_csv(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Custom CLI arguments (also settable via environment variables)
# ---------------------------------------------------------------------------


@events.init_command_line_parser.add_listener
def _(parser):
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
        ("--image-requestors",       "HORDE_IMAGE_REQUESTORS",       "Concurrent RequestGenerator users (image POST /generate/async)"),
        ("--image-workers",          "HORDE_IMAGE_WORKERS",          "Concurrent WorkerSimulator users (image POST /generate/pop+submit)"),
        ("--text-requestors",        "HORDE_TEXT_REQUESTORS",        "Concurrent TextRequester users"),
        ("--text-workers",           "HORDE_TEXT_WORKERS",           "Concurrent TextWorkerSimulator users"),
        ("--interrogate-requestors", "HORDE_INTERROGATE_REQUESTORS", "Concurrent InterrogationRequester users"),
        ("--interrogate-workers",    "HORDE_INTERROGATE_WORKERS",    "Concurrent InterrogationWorkerSimulator users"),
        ("--status-pollers",         "HORDE_STATUS_POLLERS",         "Concurrent StatusPoller users (image /check + /status)"),
        ("--hot-path-requestors",    "HORDE_HOT_PATH_REQUESTORS",    "Concurrent HotPathRequester users (identical-payload image POST /async)"),
        ("--meta-browsers",          "HORDE_META_BROWSERS",          "Concurrent MetaBrowser users (read-only meta endpoints)"),
        ("--misuse-users",           "HORDE_MISUSE_USERS",           "Concurrent MisuseUser users (4xx validation paths)"),
    ):
        fixed_count_group.add_argument(_flag, type=int, env_var=_envvar, default=0, help=_help + " (0 = use weight-based distribution)")

    # Polling behavior. We want to mirror real clients: submit, then aggressively
    # poll /check until done, only escalating to /status (which is rate-limited at
    # 10/min/path) on completion. Defaults match a typical SDK loop (~1-3 Hz checks
    # while ≤ 4 outstanding requests in flight per client).
    poll_group = parser.add_argument_group("AI Horde Stress Test - requestor poll loop")
    poll_group.add_argument("--requestor-max-pending", type=int, env_var="HORDE_REQUESTOR_MAX_PENDING", default=4,
                            help="Max in-flight /async requests per RequestGenerator before they pause submitting and only poll")
    poll_group.add_argument("--requestor-wait-min", type=float, env_var="HORDE_REQUESTOR_WAIT_MIN", default=0.2,
                            help="Min wait between RequestGenerator/StatusPoller task ticks (seconds)")
    poll_group.add_argument("--requestor-wait-max", type=float, env_var="HORDE_REQUESTOR_WAIT_MAX", default=1.0,
                            help="Max wait between RequestGenerator/StatusPoller task ticks (seconds)")
    poll_group.add_argument("--status-fetch-on-done", action="store_true", env_var="HORDE_STATUS_FETCH_ON_DONE", default=True,
                            help="When a /check returns done=true, also fetch /status (mirrors real client behavior)")


# ---------------------------------------------------------------------------
# Test lifecycle
# ---------------------------------------------------------------------------


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
    _fixed_count_overrides = (
        (StatusPoller,                  "status_pollers"),
        (RequestGenerator,              "image_requestors"),
        (WorkerSimulator,               "image_workers"),
        (TextRequester,                 "text_requestors"),
        (TextWorkerSimulator,           "text_workers"),
        (InterrogationRequester,        "interrogate_requestors"),
        (InterrogationWorkerSimulator,  "interrogate_workers"),
        (HotPathRequester,              "hot_path_requestors"),
        (MetaBrowser,                   "meta_browsers"),
        (MisuseUser,                    "misuse_users"),
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
            "Per-class fixed counts total %d users; remaining -u budget will be "
            "distributed by weight across the unpinned User classes.",
            fixed_total,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(api_key: str) -> dict[str, str]:
    return {
        "apikey": api_key,
        "Client-Agent": _config.get("client_agent", "aihorde_stress_test:1.0.0:(discord)stress_tester"),
    }


def _random_prompt() -> str:
    words = [
        "landscape",
        "portrait",
        "cyberpunk",
        "medieval",
        "futuristic",
        "underwater",
        "forest",
        "city",
        "desert",
        "mountain",
        "space",
        "robot",
        "dragon",
        "castle",
        "sunset",
        "neon",
        "abstract",
    ]
    return " ".join(random.sample(words, k=random.randint(3, 8)))


def _pick_requestor_key() -> str:
    keys = _config.get("requestor_api_keys", [])
    if not keys:
        return _config.get("anonymous_api_key", "0000000000")
    return random.choice(keys)


def _pick_worker_key() -> str:
    keys = _config.get("worker_api_keys", [])
    if not keys:
        return _config.get("anonymous_api_key", "0000000000")
    return random.choice(keys)


class StatusPoller(HttpUser):
    """Simulates clients polling /generate/check and /generate/status.

    This is the highest-traffic endpoint in production (~10 req/s per client).
    Exercises: wp_has_valid_workers, get_wp_queue_stats, get_request_avg, count_active_workers.
    """

    weight = 5
    fixed_count = 0  # set via --status-pollers in on_test_start
    wait_time = between(0.5, 2)

    def on_start(self):
        self.pending_ids = []
        # Seed request always uses a real requestor key if available
        self.api_key = _pick_requestor_key()
        # `_submit_request` calls `_handle_async_generate`, which raises
        # `RescheduleTask` on 429/expected-403 responses. Locust treats any
        # exception out of `on_start` as a hard error (stack trace to stderr,
        # the user is torn down) — but for our seed call, a rate-limit just
        # means "don't seed pending_ids; the first @task will retry". Swallow
        # it explicitly here.
        try:
            self._submit_request()
        except RescheduleTask:
            pass

        opts = self.environment.parsed_options
        if random.random() < opts.anon_chance_poller:
            self.api_key = _config["anonymous_api_key"]
        else:
            self.api_key = _pick_requestor_key()

    def _submit_request(self):
        opts = self.environment.parsed_options
        models = _config.get("models", [])
        request_models = random.sample(models, k=random.randint(0, len(models)))
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler",
            },
            "models": request_models,
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/async [poller-seed]",
        ) as resp:
            req_id = _handle_async_generate(resp, self.environment)
            if req_id:
                self.pending_ids.append(req_id)

    @task(8)
    def poll_check(self):
        """Lightweight status check — exercises the 1s-cached DB helpers."""
        if not self.pending_ids:
            self._submit_request()
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/check/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/check/[id]",
        ) as resp:
            if resp.ok:
                data = resp.json()
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                # Request expired or was pruned — normal end-of-life.
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/check/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(1.0, 3.0))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(2)
    def poll_status(self):
        """Full status — exercises procgen detail retrieval + R2 presigned URLs."""
        if not self.pending_ids:
            self._submit_request()
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/status/[id]",
        ) as resp:
            if resp.ok:
                data = resp.json()
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                # /status/ has its own per-IP limiter ("10 per 1 minute"); back off hard.
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/status/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(6.0, 12.0))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class RequestGenerator(HttpUser):
    """Simulates a real client: submit /generate/async, then aggressively poll
    /generate/check/<id> at ~1-3 Hz until done, escalate to /generate/status/<id>
    on completion.

    The previous implementation fire-and-forgot every request, leaving zero
    /check pressure unless --status-pollers was non-zero. With long simulated
    gen times (typical of production), this meant the check-path was wildly
    under-exercised relative to its real-world load.

    Each instance keeps up to ``--requestor-max-pending`` ids in-flight; once
    full, submission tasks short-circuit and the poll task does all the work.
    Server-side rate limits (`/check` = 10/sec/path, `/status` = 10/min/path)
    are honoured by routing all traffic through `_handle_check_response`.

    Exercises: prompt detection (PromptChecker.__call__), WP creation +
    activate, count_waiting_requests, is_ip_safe countermeasure checks,
    plus the full poll-loop hot path (wp_has_valid_workers, get_wp_queue_stats,
    get_request_avg, count_active_workers).
    """

    weight = 3
    fixed_count = 0  # set via --image-requestors in on_test_start
    # `wait_time` is overridden in on_start so it picks up CLI knobs; kept here
    # so Locust doesn't complain at class-load time.
    wait_time = between(0.2, 1.0)

    def on_start(self):
        opts = self.environment.parsed_options
        rand = random.random()
        if rand < opts.anon_chance_requester:
            self.api_key = _config["anonymous_api_key"]
        elif rand < opts.anon_chance_requester + 0.10:
            self.api_key = _pick_worker_key()
        else:
            self.api_key = _pick_requestor_key()

        models = _config.get("models", [])
        self.request_models = random.sample(models, k=random.randint(0, len(models)))
        self.pending_ids: list[str] = []
        self.max_pending: int = max(1, int(opts.requestor_max_pending))
        # Override wait_time per-instance using the configured min/max so
        # operators can tune the polling cadence without editing source.
        self.wait_time = lambda: random.uniform(opts.requestor_wait_min, opts.requestor_wait_max)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _at_capacity(self) -> bool:
        return len(self.pending_ids) >= self.max_pending

    def _submit(self, name: str, payload: dict) -> None:
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            try:
                req_id = _handle_async_generate(resp, self.environment)
            except RescheduleTask:
                # Bubble the back-off up so Locust skips ahead, but make sure
                # we don't bubble it out of `on_start`-style contexts (only
                # tasks call _submit, so this is safe here).
                raise
            if req_id:
                self.pending_ids.append(req_id)

    def _fetch_status(self, req_id: str) -> None:
        """Mirror real client: on done, fetch /status once for the full payload.

        /status is limited to 10/min/path, but each id is only fetched once
        per completion, so we stay well under the limit.
        """
        with self.client.get(
            f"/api/v2/generate/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/status/[id]",
        ) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/status/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    @task(30)
    def poll_pending(self):
        """Aggressive /check loop — the dominant traffic source.

        Weight 30 vs the submit tasks' combined weight of 8 means roughly
        ~3.75 polls per submission per user, which combined with up to
        --requestor-max-pending ids per user produces the 10s-of-Hz
        per-id polling that real clients generate.
        """
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/check/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/check/[id]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                resp.success()
                if data.get("done") or data.get("faulted"):
                    # Drop before fetching /status so we don't double-poll if
                    # the next task tick lands on this id.
                    if req_id in self.pending_ids:
                        self.pending_ids.remove(req_id)
                    if data.get("done") and self.environment.parsed_options.status_fetch_on_done:
                        self._fetch_status(req_id)
            elif resp.status_code in (404, 410):
                if req_id in self.pending_ids:
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                # /check is 10/sec/path — if we hit this we're polling a single
                # id far too aggressively. Back off briefly.
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/check/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(0.5, 1.5))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(5)
    def generate_simple(self):
        """Basic txt2img — exercises prompt detection + WP pipeline."""
        if self._at_capacity():
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler",
            },
            "models": self.request_models,
        }
        self._submit("/api/v2/generate/async [simple]", payload)

    @task(2)
    def generate_large(self):
        """High-res request — tests resolution-based cost calculations."""
        if self._at_capacity():
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.large_gen_width,
                "height": opts.large_gen_height,
                "steps": opts.large_gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler_a",
            },
            "models": self.request_models,
        }
        self._submit("/api/v2/generate/async [large]", payload)

    @task(1)
    def generate_multi_model(self):
        """Multi-model request — broader candidate evaluation."""
        if self._at_capacity():
            return
        opts = self.environment.parsed_options
        payload = {
            "prompt": _random_prompt(),
            "nsfw": False,
            "r2": True,
            "trusted_workers": False,
            "params": {
                "width": opts.gen_width,
                "height": opts.gen_height,
                "steps": opts.gen_steps,
                "cfg_scale": opts.gen_cfg_scale,
                "sampler_name": "k_euler",
            },
            "models": self.request_models,
        }
        self._submit("/api/v2/generate/async [multi-model]", payload)


class WorkerSimulator(HttpUser):
    """Simulates workers popping and submitting jobs.

    Exercises: get_sorted_wp (the big DB query), candidate evaluation loop,
    start_generation, set_generation (record_contribution, R2, webhook).
    """

    weight = 2
    fixed_count = 0  # set via --image-workers in on_test_start
    wait_time = between(1, 4)

    def create_worker_name(self):
        return f"StressWorker-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

        models = _config.get("models", [])
        self.worker_models = random.sample(models, k=random.randint(1, max(1, len(models))))

        # Check that this worker doesn't already exist (from a previous test run), and choose a new name if so.
        # The endpoint returns 200 + worker JSON if a worker by that name exists,
        # or 404 ("WorkerNotFound") when the name is free — the latter is the
        # success case for *us*, so we treat 404 as "name available".
        for _ in range(10):
            with self.client.get(
                f"/api/v2/workers/name/{self.worker_name}",
                headers=_headers(self.api_key),
                name="/api/v2/workers [check-name]",
                catch_response=True,
            ) as resp:
                if resp.status_code == 404:
                    resp.success()
                    break
                if resp.ok:
                    # Name already in use — pick a different one and retry.
                    resp.success()
                    self.worker_name = self.create_worker_name()
                    continue
                # Any other status: don't loop forever, just proceed.
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                break

    def on_stop(self):
        """Cleanup: delete the worker we created (best-effort)."""
        with self.client.get(
            f"/api/v2/workers/name/{self.worker_name}",
            headers=_headers(self.api_key),
            name="/api/v2/workers [check-name]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 404:
                resp.success()
                return
            if not resp.ok:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
                return
            resp.success()
            data = _safe_json(resp) or {}
            worker_id = data.get("id") if isinstance(data, dict) else None
            if not worker_id:
                return
            with self.client.delete(
                f"/api/v2/workers/{worker_id}",
                headers=_headers(self.api_key),
                name="/api/v2/workers [delete]",
                catch_response=True,
            ) as del_resp:
                # 423 LOCKED: worker has contributions and can't be deleted — expected after load.
                if del_resp.ok or del_resp.status_code in (404, 410, 423):
                    del_resp.success()
                else:
                    del_resp.failure(f"Status {del_resp.status_code}: {del_resp.text[:200]}")

    @task
    def pop_and_submit(self):
        """Full worker loop: pop a job, then submit a fake result."""
        opts = self.environment.parsed_options
        pop_payload = {
            "name": self.worker_name,
            "models": self.worker_models,
            "bridge_agent": opts.worker_bridge_agent,
            "nsfw": True,
            "amount": 1,
            "max_pixels": opts.worker_max_pixels,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_lora": True,
        }
        with self.client.post(
            "/api/v2/generate/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                # 400 ProfaneWorkerName → unlucky random suffix; rotate name and skip.
                # 403 WorkerMaintenance  → the simulated worker has been disabled by the
                #     server for dropping jobs; rotate name so the next pop creates a
                #     fresh worker rather than hammering the disabled one.
                if resp.status_code in (400, 403) and (
                    _is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)
                ):
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    # Too-many-workers / flagged-account: this user account is saturated,
                    # switch to a *different* worker key so we can still exercise /pop under load.
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    time.sleep(random.uniform(2.0, 6.0))
                    raise RescheduleTask()
                resp.failure(f"Pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            pop_data = body or {}
            job_id = pop_data.get("id", None)
            if not job_id:
                resp.success()
                return
            resp.success()

        time.sleep(random.uniform(opts.sim_gen_time_min, opts.sim_gen_time_max))
        submit_payload = {
            "id": job_id,
            "generation": "R2",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            # 404 = the WP/procgen was pruned while we were "generating".
            # 400 with rc "InvalidJobID" = same root cause, different surface.
            # Both are realistic outcomes after long simulated gen times.
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                time.sleep(random.uniform(2.0, 6.0))
                return
            resp.failure(f"Submit failed: {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Hot/cold helpers
# ---------------------------------------------------------------------------
#
# Hot path  = repeated identical payload. Exercises any request-level caches
#             (e.g. prompt-hash dedup, is_ip_safe caching, model marshal cache,
#             get_request_avg 1s cache) and the "happy" DB fast paths.
# Cold path = randomized payload/key per call. Bypasses caches, forces fresh
#             WP inserts, broader candidate evaluation, and more expensive
#             joins for get_sorted_wp.

_HOT_PROMPT = "a serene cyberpunk landscape at sunset, ultra detailed"
_HOT_TEXT_PROMPT = "Once upon a time in a faraway land,"


def _hot_image_payload(opts):
    return {
        "prompt": _HOT_PROMPT,
        "nsfw": False,
        "r2": True,
        "trusted_workers": False,
        "params": {
            "width": opts.gen_width,
            "height": opts.gen_height,
            "steps": opts.gen_steps,
            "cfg_scale": opts.gen_cfg_scale,
            "sampler_name": "k_euler",
        },
        "models": _config.get("models", [])[:1],
    }


def _cold_image_payload(opts):
    models = _config.get("models", [])
    return {
        "prompt": _random_prompt() + f" seed-{random.randint(0, 10**9)}",
        "nsfw": random.random() < 0.1,
        "r2": True,
        "trusted_workers": False,
        "params": {
            "width": random.choice([512, 576, 640, 768]),
            "height": random.choice([512, 576, 640, 768]),
            "steps": random.choice([15, 20, 25, 30]),
            "cfg_scale": round(random.uniform(4.0, 10.0), 1),
            "sampler_name": random.choice(["k_euler", "k_euler_a", "k_dpmpp_2m", "k_heun"]),
        },
        "models": random.sample(models, k=random.randint(0, max(1, len(models)))) if models else [],
    }


def _handle_async_text(resp, environment, name):
    """Common handling for POST /generate/text/async responses."""
    if resp.ok:
        body = _safe_json(resp) or {}
        resp.success()
        return body.get("id")
    body = _safe_json(resp)
    if resp.status_code == 429:
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        time.sleep(random.uniform(2.0, 6.0))
        raise RescheduleTask()
    if resp.status_code == 400 and _is_expected_rc(body, {"KudosUpfront", "SharedKeyInsufficientKudos"}):
        resp.success()
        _record_expected(environment, "POST", name, resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
        raise RescheduleTask()
    resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")
    return None


# ---------------------------------------------------------------------------
# Text generation
# ---------------------------------------------------------------------------


class TextRequester(HttpUser):
    """Simulates clients submitting text generation requests and polling them.

    Exercises: /generate/text/async (TextAsyncGenerate), /generate/text/status
    (TextAsyncStatus), kobold WP creation, text kudos calculation path.
    """

    weight = 2
    fixed_count = 0  # set via --text-requestors in on_test_start
    wait_time = between(1, 4)

    def on_start(self):
        self.pending_ids: list[str] = []
        self.api_key = _pick_requestor_key()

    def _post_async(self, payload: dict, name: str):
        with self.client.post(
            "/api/v2/generate/text/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            req_id = _handle_async_text(resp, self.environment, name)
            if req_id:
                self.pending_ids.append(req_id)

    @task(4)
    def text_async_hot(self):
        """Repeated identical text request — exercises hot cache path."""
        payload = {
            "prompt": _HOT_TEXT_PROMPT,
            "params": {"max_length": 80, "max_context_length": 1024, "temperature": 0.7, "top_p": 0.9},
            "models": [],
            "trusted_workers": False,
        }
        self._post_async(payload, "/api/v2/generate/text/async [hot]")

    @task(3)
    def text_async_cold(self):
        """Randomized text request — exercises cold/WP-creation path."""
        payload = {
            "prompt": _random_prompt() + " " + uuid.uuid4().hex[:12],
            "params": {
                "max_length": random.choice([40, 80, 128, 200]),
                "max_context_length": random.choice([1024, 1536, 2048]),
                "temperature": round(random.uniform(0.3, 1.2), 2),
                "top_p": round(random.uniform(0.7, 1.0), 2),
                "top_k": random.choice([0, 40, 60]),
            },
            "models": [],
            "trusted_workers": False,
        }
        self._post_async(payload, "/api/v2/generate/text/async [cold]")

    @task(8)
    def text_status(self):
        if not self.pending_ids:
            return
        req_id = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/generate/text/status/{req_id}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/status/[id]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                if data.get("done") or data.get("faulted"):
                    self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(req_id)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "GET", "/api/v2/generate/text/status/[id]",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class TextWorkerSimulator(HttpUser):
    """Simulates text workers: /generate/text/pop + /generate/text/submit."""

    weight = 1
    fixed_count = 0  # set via --text-workers in on_test_start
    wait_time = between(1, 4)

    def create_worker_name(self):
        return f"StressTextWorker-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

    @task
    def pop_and_submit_text(self):
        opts = self.environment.parsed_options
        pop_payload = {
            "name": self.worker_name,
            "models": _config.get("models", [])[:2] or ["koboldcpp/llama-3"],
            "bridge_agent": "KoboldAI Client:1.19.2-stress:https://github.com/koboldai/koboldai-client",
            "nsfw": True,
            "max_length": 512,
            "max_context_length": 2048,
            "softprompts": [],
            "threads": 1,
        }
        with self.client.post(
            "/api/v2/generate/text/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (
                    _is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)
                ):
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/text/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/generate/text/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    raise RescheduleTask()
                resp.failure(f"Text pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            job_id = data.get("id")
            if not job_id:
                resp.success()
                return
            resp.success()

        time.sleep(random.uniform(opts.sim_gen_time_min / 2, opts.sim_gen_time_max / 2))
        submit_payload = {
            "id": job_id,
            "generation": "Once upon a time there was a stress test that completed successfully.",
            "state": "ok",
            "seed": random.randint(0, 999999999),
        }
        with self.client.post(
            "/api/v2/generate/text/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/text/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/text/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/generate/text/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            resp.failure(f"Text submit failed: {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Interrogation (Alchemy) generation
# ---------------------------------------------------------------------------


_INTERROGATION_FORMS = ["caption", "interrogation", "nsfw"]


class InterrogationRequester(HttpUser):
    """Submits /interrogate/async requests with a tiny 1x1 source image."""

    weight = 2
    fixed_count = 0  # set via --interrogate-requestors in on_test_start
    wait_time = between(2, 6)

    def on_start(self):
        self.pending_ids: list[str] = []
        self.api_key = _pick_requestor_key()

    @task(3)
    def interrogate_hot(self):
        payload = {
            "source_image": _TINY_PNG_B64,
            "forms": [{"name": "caption"}],
            "trusted_workers": False,
            "slow_workers": True,
        }
        self._post(payload, "/api/v2/interrogate/async [hot]")

    @task(2)
    def interrogate_cold(self):
        forms = random.sample(_INTERROGATION_FORMS, k=random.randint(1, len(_INTERROGATION_FORMS)))
        payload = {
            "source_image": _TINY_PNG_B64,
            "forms": [{"name": f} for f in forms],
            "trusted_workers": False,
            "slow_workers": True,
        }
        self._post(payload, "/api/v2/interrogate/async [cold]")

    def _post(self, payload: dict, name: str):
        with self.client.post(
            "/api/v2/interrogate/async",
            json=payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name=name,
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                rid = data.get("id")
                if rid:
                    self.pending_ids.append(rid)
                resp.success()
                return
            if resp.status_code == 429:
                resp.success()
                _record_expected(self.environment, "POST", name,
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(6)
    def interrogate_status(self):
        if not self.pending_ids:
            return
        rid = random.choice(self.pending_ids)
        with self.client.get(
            f"/api/v2/interrogate/status/{rid}",
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/interrogate/status/[id]",
        ) as resp:
            if resp.ok:
                data = _safe_json(resp) or {}
                if data.get("state") in ("done", "faulted", "cancelled"):
                    self.pending_ids.remove(rid)
                resp.success()
            elif resp.status_code in (404, 410):
                self.pending_ids.remove(rid)
                resp.success()
            elif resp.status_code == 429:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")


class InterrogationWorkerSimulator(HttpUser):
    """Simulates an alchemy worker: /interrogate/pop + /interrogate/submit."""

    weight = 1
    fixed_count = 0  # set via --interrogate-workers in on_test_start
    wait_time = between(2, 5)

    def create_worker_name(self):
        return f"StressAlchemist-{''.join(random.choices(string.ascii_lowercase, k=4))}"

    def on_start(self):
        self.worker_name = self.create_worker_name()
        self.api_key = _pick_worker_key()

    @task
    def pop_and_submit_interrogation(self):
        opts = self.environment.parsed_options
        pop_payload = {
            "name": self.worker_name,
            "forms": _INTERROGATION_FORMS,
            "amount": 1,
            "bridge_agent": "AI Horde Worker Alchemist:stress:https://github.com/Haidra-Org",
            "threads": 1,
            "max_tiles": 16,
        }
        with self.client.post(
            "/api/v2/interrogate/pop",
            json=pop_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/interrogate/pop",
        ) as resp:
            body = _safe_json(resp)
            if not resp.ok:
                if resp.status_code in (400, 403) and (
                    _is_expected_rc(body, _EXPECTED_RC_RECOVER) or _is_too_many_workers(body)
                ):
                    resp.success()
                    _record_expected(self.environment, "POST", "/api/v2/interrogate/pop",
                                     resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                    rc = (body or {}).get("rc") if isinstance(body, dict) else None
                    if _is_too_many_workers(body) or rc in ("TooManySameIPs", "WrongCredentials", "WorkerFlaggedMaintenance"):
                        self.api_key = _pick_worker_key()
                    self.worker_name = self.create_worker_name()
                    raise RescheduleTask()
                if resp.status_code == 429:
                    resp.success()
                    raise RescheduleTask()
                resp.failure(f"Interrogate pop failed: {resp.status_code}: {resp.text[:200]}")
                return
            data = body or {}
            forms = data.get("forms") or []
            resp.success()
            if not forms:
                return
            form_id = forms[0].get("id")
            form_name = forms[0].get("form") or "caption"

        if not form_id:
            return
        time.sleep(random.uniform(1.0, 3.0))
        submit_payload = {
            "id": form_id,
            "result": {form_name: "stress-test-result"},
            "state": "ok",
        }
        with self.client.post(
            "/api/v2/interrogate/submit",
            json=submit_payload,
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/interrogate/submit",
        ) as resp:
            body = _safe_json(resp)
            if resp.ok:
                resp.success()
                return
            if resp.status_code == 404 or _is_expected_rc(body, {"InvalidJobID", "InvalidProcGen"}):
                resp.success()
                _record_expected(self.environment, "POST", "/api/v2/interrogate/submit",
                                 resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
                return
            if resp.status_code == 429:
                resp.success()
                return
            resp.failure(f"Interrogate submit failed: {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Meta / status / browse endpoints
# ---------------------------------------------------------------------------


class MetaBrowser(HttpUser):
    """Read-only endpoints consumed by dashboards, the web UI, and clients.

    These are cache-heavy in production. We hit both the hot (same path repeatedly)
    and cold (random id / random model) variants so the response cache is exercised
    while still flushing through the underlying DB helpers sometimes.
    """

    weight = 3
    fixed_count = 0  # set via --meta-browsers in on_test_start
    wait_time = between(1, 3)

    def on_start(self):
        self.api_key = _pick_requestor_key()
        # Prime with a worker id we can fetch details for (cold path). Best-effort.
        self.worker_ids: list[str] = []
        self.user_ids: list[str] = []
        try:
            resp = self.client.get("/api/v2/workers?type=image", headers=_headers(self.api_key),
                                    name="/api/v2/workers [bootstrap]")
            if resp.ok:
                data = resp.json() or []
                self.worker_ids = [w.get("id") for w in data[:20] if w.get("id")]
            resp = self.client.get("/api/v2/users", headers=_headers(self.api_key),
                                    name="/api/v2/users [bootstrap]")
            if resp.ok:
                data = resp.json() or []
                self.user_ids = [str(u.get("id")) for u in data[:20] if u.get("id") is not None]
        except Exception as err:
            logger.debug("MetaBrowser bootstrap skipped: %s", err)

    @task(5)
    def heartbeat(self):
        self.client.get("/api/v2/status/heartbeat", name="/api/v2/status/heartbeat [hot]")

    @task(3)
    def models(self):
        # /status/models is @cache.cached — this is the canonical hot path.
        self.client.get("/api/v2/status/models?type=image", name="/api/v2/status/models [hot]")

    @task(1)
    def models_cold(self):
        # Vary the query string to bypass the response cache.
        variant = random.choice(["?type=text", "?type=image&min_count=1", "?model_state=known"])
        self.client.get(f"/api/v2/status/models{variant}", name="/api/v2/status/models [cold]")

    @task(2)
    def performance(self):
        self.client.get("/api/v2/status/performance", name="/api/v2/status/performance")

    @task(1)
    def horde_modes(self):
        self.client.get("/api/v2/status/modes", name="/api/v2/status/modes")

    @task(1)
    def news(self):
        self.client.get("/api/v2/status/news", name="/api/v2/status/news")

    @task(2)
    def workers_list(self):
        self.client.get("/api/v2/workers?type=image", name="/api/v2/workers [list]")

    @task(2)
    def teams_list(self):
        self.client.get("/api/v2/teams", name="/api/v2/teams [list]")

    @task(1)
    def worker_single(self):
        if not self.worker_ids:
            return
        wid = random.choice(self.worker_ids)
        with self.client.get(f"/api/v2/workers/{wid}", name="/api/v2/workers/[id]",
                             catch_response=True) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(1)
    def user_single(self):
        if not self.user_ids:
            return
        uid = random.choice(self.user_ids)
        with self.client.get(f"/api/v2/users/{uid}", name="/api/v2/users/[id]",
                             catch_response=True) as resp:
            if resp.ok or resp.status_code in (404, 410):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(2)
    def find_user_self(self):
        """Hot path: identity lookup with a valid key."""
        with self.client.get("/api/v2/find_user", headers=_headers(self.api_key),
                             name="/api/v2/find_user [hot]", catch_response=True) as resp:
            if resp.ok or resp.status_code in (401,):
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(2)
    def stats_img_totals(self):
        self.client.get("/api/v2/stats/img/totals", name="/api/v2/stats/img/totals")

    @task(1)
    def stats_img_models(self):
        self.client.get("/api/v2/stats/img/models", name="/api/v2/stats/img/models")

    @task(1)
    def stats_text_totals(self):
        self.client.get("/api/v2/stats/text/totals", name="/api/v2/stats/text/totals")


# ---------------------------------------------------------------------------
# Misuse / abuse simulation
# ---------------------------------------------------------------------------
#
# Volume-tests the *defensive* code paths: input validation, rate limiting,
# auth, countermeasures. All 4xx responses from these tasks are EXPECTED; we
# only record them as failures if they come back as 5xx (indicating the server
# didn't validate the input cleanly).


class MisuseUser(HttpUser):
    """Common endpoint misuse. Exercises validation & auth rejection paths."""

    weight = 1
    fixed_count = 0  # set via --misuse-users in on_test_start
    wait_time = between(1, 3)

    def _expect_4xx(self, resp, name: str):
        if 400 <= resp.status_code < 500:
            resp.success()
            _record_expected(self.environment, resp.request_meta.get("method", "POST"), name,
                             resp.elapsed.total_seconds() * 1000, len(resp.content or b""))
            return
        if resp.ok:
            # The request *succeeded* — unexpected for a misuse probe, but not a bug.
            resp.success()
            return
        resp.failure(f"Server error on misuse probe: {resp.status_code}: {resp.text[:200]}")

    @task(3)
    def invalid_api_key(self):
        with self.client.get(
            "/api/v2/find_user",
            headers={"apikey": "this-is-not-a-real-key", "Client-Agent": _config.get("client_agent", "stress")},
            catch_response=True,
            name="/api/v2/find_user [misuse-bad-key]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/find_user [misuse-bad-key]")

    @task(2)
    def status_not_found(self):
        fake = uuid.uuid4().hex
        with self.client.get(
            f"/api/v2/generate/status/{fake}",
            catch_response=True,
            name="/api/v2/generate/status/[id] [misuse-missing]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/status/[id] [misuse-missing]")

    @task(2)
    def text_status_not_found(self):
        fake = uuid.uuid4().hex
        with self.client.get(
            f"/api/v2/generate/text/status/{fake}",
            catch_response=True,
            name="/api/v2/generate/text/status/[id] [misuse-missing]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/text/status/[id] [misuse-missing]")

    @task(2)
    def empty_prompt(self):
        payload = {"prompt": "", "params": {"width": 512, "height": 512, "steps": 20}, "models": []}
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/generate/async [misuse-empty-prompt]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/async [misuse-empty-prompt]")

    @task(2)
    def oversized_image(self):
        payload = {
            "prompt": _random_prompt(),
            "params": {"width": 4096, "height": 4096, "steps": 150, "cfg_scale": 25.0},
            "models": _config.get("models", []),
            "r2": True,
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/generate/async [misuse-oversized]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/async [misuse-oversized]")

    @task(2)
    def invalid_model_name(self):
        payload = {
            "prompt": _random_prompt(),
            "params": {"width": 512, "height": 512, "steps": 20},
            "models": ["definitely_not_a_real_model_" + uuid.uuid4().hex[:6]],
        }
        with self.client.post(
            "/api/v2/generate/async",
            json=payload,
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/generate/async [misuse-bad-model]",
        ) as resp:
            # The server accepts unknown models (they're just advisory) — this exercises
            # the "no valid workers" response branch in the async handler.
            if resp.ok or 400 <= resp.status_code < 500:
                resp.success()
            else:
                resp.failure(f"Status {resp.status_code}: {resp.text[:200]}")

    @task(2)
    def worker_not_found(self):
        fake_name = f"NonexistentWorker-{uuid.uuid4().hex[:8]}"
        with self.client.get(
            f"/api/v2/workers/name/{fake_name}",
            catch_response=True,
            name="/api/v2/workers/name/[name] [misuse-missing]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/workers/name/[name] [misuse-missing]")

    @task(1)
    def submit_unknown_job(self):
        with self.client.post(
            "/api/v2/generate/submit",
            json={"id": uuid.uuid4().hex, "generation": "R2", "state": "ok", "seed": 0},
            headers=_headers(_pick_worker_key()),
            catch_response=True,
            name="/api/v2/generate/submit [misuse-bad-id]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/submit [misuse-bad-id]")

    @task(1)
    def transfer_kudos_to_self(self):
        with self.client.post(
            "/api/v2/kudos/transfer",
            json={"username": "anon#0", "amount": 1},
            headers=_headers(_pick_requestor_key()),
            catch_response=True,
            name="/api/v2/kudos/transfer [misuse]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/kudos/transfer [misuse]")

    @task(1)
    def pop_missing_fields(self):
        with self.client.post(
            "/api/v2/generate/pop",
            json={"name": "StressMissingFields"},
            headers=_headers(_pick_worker_key()),
            catch_response=True,
            name="/api/v2/generate/pop [misuse-missing-fields]",
        ) as resp:
            self._expect_4xx(resp, "/api/v2/generate/pop [misuse-missing-fields]")


# ---------------------------------------------------------------------------
# Hot-path variants for the existing RequestGenerator
# ---------------------------------------------------------------------------
#
# The original ``generate_simple`` is effectively cold (randomized prompt each
# call); we add an explicit hot-path task that reuses the same payload so the
# prompt-hash / kudos caches actually get exercised, and rename the cold variant
# in metrics so the operator can compare them side-by-side.


class HotPathRequester(HttpUser):
    """Dedicated hot-path requester — identical payload every call.

    Complements ``RequestGenerator``. Isolating the hot path into its own User
    keeps the stats table readable: every row under this class is a cache hit.
    """

    weight = 1
    fixed_count = 0  # set via --hot-path-requestors in on_test_start
    wait_time = between(1, 3)

    def on_start(self):
        self.api_key = _pick_requestor_key()

    @task(5)
    def async_hot(self):
        opts = self.environment.parsed_options
        with self.client.post(
            "/api/v2/generate/async",
            json=_hot_image_payload(opts),
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/async [hot]",
        ) as resp:
            _handle_async_generate(resp, self.environment)

    @task(2)
    def async_cold(self):
        opts = self.environment.parsed_options
        with self.client.post(
            "/api/v2/generate/async",
            json=_cold_image_payload(opts),
            headers=_headers(self.api_key),
            catch_response=True,
            name="/api/v2/generate/async [cold]",
        ) as resp:
            _handle_async_generate(resp, self.environment)

