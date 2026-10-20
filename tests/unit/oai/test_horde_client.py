# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's Horde client: bounded concurrent calls and the backoff of a failing Horde root."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from horde.oai.horde_client import (
    CANCEL_MIN_CONNECTIONS,
    HTTP_KEEPALIVE_EXPIRY_SECONDS,
    HTTP_MAX_CONNECTIONS,
    UNHEALTHY_URL_BACKOFF_SECONDS,
    CancelSlotUnavailableError,
    HordeClient,
    HordeCredentials,
    HordeUnavailableError,
    cancel_max_connections,
    create_http_client,
)
from horde.oai.waiter import ChatRequestWatcher, ChatWaitTimeoutError, CompletionSubscription, WaitBudget
from tests.unit.oai.conftest import REQUEST_ID, UNRECORDED_METRICS, done_status

pytestmark = pytest.mark.asyncio

FIRST_URL: str = "http://horde-first.test/api"
"""The first Horde root in the rotation."""

SECOND_URL: str = "http://horde-second.test/api"
"""The second Horde root in the rotation."""

CREDENTIALS: HordeCredentials = HordeCredentials(apikey="test-key", client_agent="test:1:test")
"""The identity every call in these tests is made with."""

STATUS_PATH: str = f"/v2/generate/chat/status/{REQUEST_ID}"
"""The status route of the one request these tests ask about."""


async def test_watcher_deadline_includes_wait_for_client_capacity() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_slot(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=done_status())

    with respx.mock() as router:
        route = router.get(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=hold_slot)
        async with httpx.AsyncClient() as http_client:
            client = HordeClient(
                horde_urls=(FIRST_URL,),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                max_concurrent_calls=1,
            )
            occupied = asyncio.create_task(client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS))
            await asyncio.wait_for(entered.wait(), timeout=2)
            watcher = ChatRequestWatcher(
                horde_client=client,
                credentials=CREDENTIALS,
                subscription=CompletionSubscription(REQUEST_ID),
                front_metrics=UNRECORDED_METRICS,
                budget=WaitBudget(
                    deadline=time.monotonic() + 0.05,
                    final_status_timeout_seconds=0.05,
                    fallback_poll_seconds=1,
                    signal_recheck_seconds=0.1,
                    fail_fast_grace_seconds=10,
                ),
            )
            try:
                with pytest.raises(ChatWaitTimeoutError):
                    await asyncio.wait_for(watcher.wait_until_finished(), timeout=2)
                assert route.call_count == 0  # The occupied mock response has not returned yet.
            finally:
                release.set()
                await occupied
            # The timed-out waiter must not leak a slot or send a delayed status call.
            assert (await client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)).done
            assert route.call_count == 2


class ManualClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now: float = 0.0
        """The current reading."""

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


async def test_concurrent_calls_beyond_the_limit_wait_outside_the_pool() -> None:
    in_flight = 0
    peak_in_flight = 0
    release_calls = asyncio.Event()

    async def answer_slowly(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await release_calls.wait()
        in_flight -= 1
        return httpx.Response(200, json=done_status())

    with respx.mock() as router:
        router.get(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=answer_slowly)
        async with httpx.AsyncClient() as http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL,),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                max_concurrent_calls=2,
            )
            calls = [asyncio.create_task(horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)) for _ in range(6)]
            await asyncio.sleep(0.1)
            peak_before_release = peak_in_flight
            release_calls.set()
            statuses = await asyncio.gather(*calls)

    assert peak_before_release == 2
    assert peak_in_flight == 2
    assert all(status.done for status in statuses)


@pytest.mark.parametrize("connection_error", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_refused_root_is_left_out_of_the_rotation_until_its_backoff_ends(connection_error: type[httpx.TransportError]) -> None:
    clock = ManualClock()
    with respx.mock() as router:
        first_route = router.get(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=connection_error("refused"))
        second_route = router.get(f"{SECOND_URL}{STATUS_PATH}").mock(
            return_value=httpx.Response(200, json=done_status()),
        )
        async with httpx.AsyncClient() as http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL, SECOND_URL),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                clock=clock,
            )
            for _ in range(4):
                await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)
            calls_to_first_during_backoff = first_route.call_count

            clock.now += UNHEALTHY_URL_BACKOFF_SECONDS + 0.1
            for _ in range(2):
                await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)

    assert calls_to_first_during_backoff == 1
    assert first_route.call_count == 2
    assert second_route.call_count == 6


async def test_every_root_in_backoff_fails_without_connecting() -> None:
    clock = ManualClock()
    with respx.mock() as router:
        only_route = router.get(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL,),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                clock=clock,
            )
            for _ in range(3):
                with pytest.raises(HordeUnavailableError):
                    await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)

    assert only_route.call_count == 1


async def test_connection_dropped_after_sending_is_not_retried_elsewhere() -> None:
    with respx.mock(assert_all_called=False) as router:
        first_route = router.post(f"{FIRST_URL}/v2/generate/chat/async").mock(
            side_effect=httpx.ReadError("connection reset"),
        )
        second_route = router.post(f"{SECOND_URL}/v2/generate/chat/async").mock(
            return_value=httpx.Response(202, json={"id": REQUEST_ID}),
        )
        async with httpx.AsyncClient() as http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL, SECOND_URL), http_client=http_client, proxy_passkey=None, front_metrics=UNRECORDED_METRICS
            )
            with pytest.raises(HordeUnavailableError):
                await horde_client.submit_chat(request_body={"model": "m"}, credentials=CREDENTIALS)

    assert first_route.call_count == 1
    assert second_route.call_count == 0


async def test_http_client_pool_matches_the_call_limit_and_expires_idle_connections() -> None:
    async with create_http_client() as http_client:
        pool = http_client._transport._pool  # type: ignore[attr-defined]

    assert pool._max_connections == HTTP_MAX_CONNECTIONS
    assert pool._max_keepalive_connections == HTTP_MAX_CONNECTIONS
    assert pool._keepalive_expiry == HTTP_KEEPALIVE_EXPIRY_SECONDS


async def test_cancels_use_their_own_pool_while_status_reads_fill_the_main_one() -> None:
    release_status = asyncio.Event()

    async def hold_status(_request: httpx.Request) -> httpx.Response:
        await release_status.wait()
        return httpx.Response(200, json=done_status())

    with respx.mock() as router:
        router.get(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=hold_status)
        cancel_route = router.delete(f"{FIRST_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        async with httpx.AsyncClient() as http_client, httpx.AsyncClient() as cancel_http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL,),
                http_client=http_client,
                cancel_http_client=cancel_http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                max_concurrent_calls=1,
            )
            held_read = asyncio.create_task(horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS))
            await asyncio.sleep(0.05)
            cancelled = await asyncio.wait_for(
                horde_client.cancel_chat(request_id=REQUEST_ID, credentials=CREDENTIALS, slot_wait_seconds=1, call_timeout_seconds=1),
                timeout=2,
            )
            release_status.set()
            await held_read

    assert cancelled.done
    assert cancel_route.call_count == 1


@pytest.mark.parametrize(
    ("max_inflight", "expected_cancel_connections"),
    [(1, CANCEL_MIN_CONNECTIONS), (100, CANCEL_MIN_CONNECTIONS), (1000, 50), (4000, 200)],
)
async def test_cancel_pool_grows_with_the_inflight_cap_above_its_floor(max_inflight: int, expected_cancel_connections: int) -> None:
    assert cancel_max_connections(max_inflight) == expected_cancel_connections


async def test_cancel_waiting_for_a_slot_is_not_sent_and_is_told_apart_from_a_slow_call() -> None:
    release_cancel = asyncio.Event()
    cancels_received = 0

    async def hold_cancel(_request: httpx.Request) -> httpx.Response:
        nonlocal cancels_received
        cancels_received += 1
        await release_cancel.wait()
        return httpx.Response(200, json=done_status())

    with respx.mock() as router:
        router.delete(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=hold_cancel)
        async with httpx.AsyncClient() as http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL,),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                max_concurrent_cancels=1,
            )
            held_cancel = asyncio.create_task(
                horde_client.cancel_chat(request_id=REQUEST_ID, credentials=CREDENTIALS, slot_wait_seconds=1, call_timeout_seconds=5),
            )
            await asyncio.sleep(0.05)
            with pytest.raises(CancelSlotUnavailableError):
                await horde_client.cancel_chat(
                    request_id=REQUEST_ID,
                    credentials=CREDENTIALS,
                    slot_wait_seconds=0.05,
                    call_timeout_seconds=5,
                )
            assert cancels_received == 1
            release_cancel.set()
            await held_cancel
            release_cancel.clear()
            # With a slot free at once, only the HTTP call counts against the call timeout.
            with pytest.raises(TimeoutError):
                await horde_client.cancel_chat(
                    request_id=REQUEST_ID,
                    credentials=CREDENTIALS,
                    slot_wait_seconds=1,
                    call_timeout_seconds=0.05,
                )
            release_cancel.set()

    assert cancels_received == 2


async def test_submits_proceed_while_status_reads_hold_every_read_slot() -> None:
    release_status = asyncio.Event()

    async def hold_status(_request: httpx.Request) -> httpx.Response:
        await release_status.wait()
        return httpx.Response(200, json=done_status())

    with respx.mock() as router:
        router.get(f"{FIRST_URL}{STATUS_PATH}").mock(side_effect=hold_status)
        submit_route = router.post(f"{FIRST_URL}/v2/generate/chat/async").mock(return_value=httpx.Response(202, json={"id": REQUEST_ID}))
        async with httpx.AsyncClient() as http_client:
            horde_client = HordeClient(
                horde_urls=(FIRST_URL,),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
                max_concurrent_calls=2,
                max_concurrent_submits=1,
            )
            held_reads = [
                asyncio.create_task(horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)) for _ in range(3)
            ]
            await asyncio.sleep(0.05)
            submission = await asyncio.wait_for(horde_client.submit_chat(request_body={"model": "m"}, credentials=CREDENTIALS), timeout=1)
            reads_done_at_submit = sum(held_read.done() for held_read in held_reads)
            release_status.set()
            statuses = await asyncio.gather(*held_reads)

    assert submission.request_id == REQUEST_ID
    assert submit_route.call_count == 1
    assert reads_done_at_submit == 0
    assert all(status.done for status in statuses)


async def test_credentials_representation_omits_the_api_key() -> None:
    assert "test-key" not in repr(CREDENTIALS)
    assert "test-key" not in str(CREDENTIALS)
