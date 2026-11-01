# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read the chat front's settings from the environment.

[`FrontSettings`][horde.oai.settings.FrontSettings] is a ``pydantic-settings`` model reading every ``HORDE_OAI_*``
variable, and a ``.env`` file in the working directory when one exists.
[`load_settings`][horde.oai.settings.load_settings] is the entry point ``python -m horde.oai`` uses. A variable that
is set but invalid fails the start with a validation error that lists it.
"""

from __future__ import annotations

from typing import Annotated

from horde_sdk.generation_parameters.text.chat_limits import MAX_CHAT_TEXT_BYTES
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from horde.chat_contract import MAX_CHAT_BODY_BYTES, MAX_CHAT_MESSAGE_COUNT
from horde.oai.horde_client import HTTP_MAX_CONNECTIONS, SUBMIT_MAX_CONCURRENT_CALLS

__all__ = [
    "DEFAULT_HORDE_URL",
    "SETTINGS_ENV_PREFIX",
    "FrontSettings",
    "load_settings",
]

SETTINGS_ENV_PREFIX: str = "HORDE_OAI_"
"""The prefix of every environment variable the front reads."""

DEFAULT_HORDE_URL: str = "http://127.0.0.1:7001/api"
"""The Horde API root the front calls when ``HORDE_OAI_HORDE_URLS`` is unset: a Horde on the same host, default port."""

HORDE_URL_SEPARATOR: str = ","
"""The separator between roots in ``HORDE_OAI_HORDE_URLS``."""

HIGHEST_PORT: int = 65535
"""The highest TCP port number."""


class FrontSettings(BaseSettings):
    """Represents the chat front's configuration, read from the ``HORDE_OAI_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix=SETTINGS_ENV_PREFIX,
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    horde_urls: Annotated[tuple[str, ...], NoDecode] = Field(default=(DEFAULT_HORDE_URL,), min_length=1)
    """The private Horde API roots (each ending in ``/api``), comma-separated, used round-robin.

    More than one root lets a refused connection move to another instance instead of failing the request.
    """

    listen: str = "0.0.0.0"
    """The address the front binds; the same default as the Horde's ``--listen``, since the front runs in a container
    reached only through the listener."""

    port: int = Field(default=7002, ge=1, le=HIGHEST_PORT)
    """The port the front binds; the port after the Horde's default 7001."""

    redis_url: str | None = Field(default=None, repr=False)
    """The primary Redis the Horde publishes chat signals on.

    Unset selects poll-only mode, where every request wakes on the fallback poll alone. The URL can carry a password,
    so it is left out of the settings' representation.
    """

    stream_redis_db: int = Field(default=7, ge=0)
    """The logical database, on the host of ``redis_url``, the Horde appends chat content deltas to.

    The default is the Horde's chat signal database (``chat_signal_db`` in ``horde/redis_ctrl.py``), where its progress
    route writes ``horde:chat:stream:<request id>``. Pub/sub ignores the database, so ``redis_url`` may select any;
    streams are keys and live in one, so the delta reader selects this database on a client of its own.
    """

    redis_liveness_seconds: float = Field(default=30.0, gt=0)
    """How long the signal subscription may receive nothing before it sends a PING, and how long the PONG may take.

    An idle subscription otherwise sends nothing, so a connection a network fault black-holes looks like a quiet one
    and ``/healthz`` keeps reporting it subscribed. A PONG that does not arrive in time drops the subscription, which
    is then made again; tens of seconds keeps the extra traffic negligible and a dead connection short-lived.
    """

    proxy_passkey: str | None = Field(default=None, repr=False)
    """The front operator's service-account proxy passkey.

    When set, each Horde call carries it with the end-user address as ``Proxied-For``, so the Horde's limiter and
    suspicion apply to the user rather than to the front. It is a credential, so it is left out of the representation.
    """

    max_inflight: int = Field(default=1000, ge=1)
    """Open requests per process before 503 ``FrontAtCapacity``.

    A waiting request holds its slot until it ends, so when the Horde stalls and requests wait to their deadline the
    cap trips at ``max_inflight`` divided by the budget in requests per second (1000 over 300 seconds is 3.3 per
    second). The cap therefore detects a stall rather than limiting throughput, and it is set well above the open
    requests a working Horde leaves. It also sizes the cancel pool; see
    [`cancel_max_connections`][horde.oai.horde_client.cancel_max_connections].
    """

    max_inflight_per_source: int = Field(default=5, ge=1)
    """Open requests per end-user address per process before 503 ``FrontAtCapacity``.

    Stops one client from filling the process. Users behind one egress address share it, so the value needs measuring
    before it is raised or lowered.
    """

    max_body_bytes: int = Field(default=MAX_CHAT_BODY_BYTES, ge=1)
    """The largest request body accepted, counted while it is received and before JSON is parsed.

    Defaults to the native route's limit, so a body the front accepts is not refused for size after the submit; the
    bound keeps one body from holding memory the process needs for thousands of waits.
    """

    max_message_count: int = Field(default=MAX_CHAT_MESSAGE_COUNT, ge=1)
    """The most messages one request may carry; defaults to the native route's limit."""

    max_text_bytes: int = Field(default=MAX_CHAT_TEXT_BYTES, ge=1)
    """The most UTF-8 bytes of message text one request may carry; defaults to the native route's limit."""

    body_read_timeout_seconds: float = Field(default=30.0, gt=0)
    """How long receiving the request body may take, so a slow sender cannot hold a connection indefinitely."""

    nonstream_timeout_seconds: float = Field(default=300.0, gt=0)
    """The total budget of a non-streamed request, starting before the submit; a request still waiting after it is 408."""

    stream_timeout_seconds: float = Field(default=600.0, gt=0)
    """The total budget of a streamed request, starting before the submit.

    Longer than the non-streamed budget because keepalive comments hold the connection open, but finite so a request
    the Horde keeps alive cannot hold a delivery slot forever.
    """

    final_status_timeout_seconds: float = Field(default=2.0, gt=0)
    """The allowance for the one status read made at the deadline, so an outcome reached just in time is returned."""

    cancel_timeout_seconds: float = Field(default=10.0, gt=0)
    """The budget of one cancel's HTTP call, which runs shielded from the client's disconnect.

    The wait for a free cancel slot has a bound of the same length of its own, so a cancel queued behind others is
    reported as not started rather than as a slow Horde.
    """

    cancel_retry_seconds: float = Field(default=30.0, gt=0)
    """How often the cancels that failed or timed out are sent once more.

    A request whose cancel failed may still be queued on the Horde and take a worker; retrying while the process runs,
    instead of only at shutdown, frees that worker once the Horde recovers. Requests older than the Horde's chat
    request expiry are dropped instead of retried.
    """

    shutdown_cancel_seconds: float = Field(default=15.0, gt=0)
    """The process-wide bound for cancelling every open request on a clean stop, not one cancel budget per request."""

    shutdown_drain_seconds: float = Field(default=30.0, gt=0)
    """How long uvicorn waits for open requests to finish on a clean stop before cancelling them."""

    heartbeat_seconds: float = Field(default=5.0, gt=0)
    """The keepalive comment interval of a streamed response; well under the listener's inactivity timeouts."""

    delta_pacing_max_window_seconds: float = Field(default=2.0, ge=0)
    """The longest window over which a relayed stream spreads the text of one worker progress post.

    0 sends each post as one chunk when it arrives. The window is the estimated interval between the worker's posts,
    so the client sees an even flow of text. Pacing adds up to one window of latency. A koboldcpp worker posts about
    once a second, and 2 s covers the jitter in that interval. When a worker stalls, the window does not grow past this
    limit, so the text is not held back longer.
    """

    delta_pacing_max_chunks_per_second: int = Field(default=20, gt=0)
    """The maximum number of content chunks a paced stream sends per second of its window.

    At 20 chunks a second, the text reads as continuous. The limit also bounds the number of events one stream adds per
    post. Each event is a JSON chunk of a few hundred bytes.
    """

    fallback_poll_seconds: float = Field(default=15.0, gt=0)
    """The base status poll interval when no signal arrives; it stretches to half the Horde's ``wait_time``, at most 60."""

    signal_recheck_seconds: float = Field(default=0.5, gt=0)
    """How soon a request reads its status once more after a terminal signal its first read did not yet show.

    The signal is published after the commit, but the status read can reach an instance or replica that lags; one short
    recheck avoids waiting a whole poll interval for that.
    """

    fail_fast_grace_seconds: float = Field(default=20.0, gt=0)
    """How long the Horde may report no eligible worker, with nothing processing, before the request is cancelled and
    answered 503 ``no_eligible_worker``; long enough for a worker restart or a model load."""

    submit_recovery_attempts: int = Field(default=2, ge=0)
    """How many times a submit whose answer was lost is sent again with the same operation key.

    Each attempt goes to the next healthy root within the request's total budget. The native route answers a repeated
    key with the existing request, so a recovery cannot create a second job; the bound keeps a Horde that drops every
    answer from holding the request until its deadline. 0 answers a lost submit with 502 at once.
    """

    native_max_connections: int = Field(default=HTTP_MAX_CONNECTIONS, gt=SUBMIT_MAX_CONCURRENT_CALLS)
    """The most simultaneous calls to the Horde and the size of the main connection pool; cancels use their own pool.

    [`SUBMIT_MAX_CONCURRENT_CALLS`][horde.oai.horde_client.SUBMIT_MAX_CONCURRENT_CALLS] of these are reserved for
    submits and the rest serve status reads and model lists, so the value must be larger than the submit share.
    """

    forwarded_allow_ips: str = "127.0.0.1"
    """The listener addresses uvicorn trusts for ``X-Forwarded-For``; the ASGI client address is then the end user."""

    @field_validator("horde_urls", mode="before")
    @classmethod
    def parse_horde_urls(cls, horde_urls: object) -> object:
        """Parse a comma-separated string of roots into a tuple, without trailing slashes or empty entries."""
        if isinstance(horde_urls, str):
            horde_urls = horde_urls.split(HORDE_URL_SEPARATOR)
        if isinstance(horde_urls, list | tuple):
            return tuple(str(horde_url).strip().rstrip("/") for horde_url in horde_urls if str(horde_url).strip())
        return horde_urls


def load_settings() -> FrontSettings:
    """Return the front's settings read from the ``HORDE_OAI_*`` environment variables and ``.env``.

    Raises:
        pydantic.ValidationError: A variable is set but invalid.
    """
    return FrontSettings()
