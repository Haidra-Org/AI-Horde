# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavior of the chat front's Horde instance tiers: drain agent polling, tier selection, in-flight counts and ``/routing``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
import pytest
import respx

from horde.oai.horde_client import (
    DrainAgentState,
    HordeAPIError,
    HordeClient,
    HordeCredentials,
    HordeUnavailableError,
    InstanceTier,
)
from tests.unit.oai.conftest import CALL_WAIT_SECONDS, FAST_TEST_SETTINGS, REQUEST_ID, UNRECORDED_METRICS, StartFront, done_status

pytestmark = pytest.mark.asyncio

AGENT_HOST: str = "127.0.0.1"
"""The host the fake drain agent listens on, which is also the host of every Horde instance in these tests."""

PRIMARY_PORT: int = 7004
"""The port of the primary Horde instance."""

FALLBACK_PORT: int = 7104
"""The port of the fallback Horde instance."""

PRIMARY_URL: str = f"http://{AGENT_HOST}:{PRIMARY_PORT}/api"
"""The API root URL of the primary Horde instance."""

FALLBACK_URL: str = f"http://{AGENT_HOST}:{FALLBACK_PORT}/api"
"""The API root URL of the fallback Horde instance."""

STATUS_PATH: str = f"/v2/generate/chat/status/{REQUEST_ID}"
"""The status route of the one request used in these tests."""

SUBMIT_PATH: str = "/v2/generate/chat/async"
"""The native submit route."""

CREDENTIALS: HordeCredentials = HordeCredentials(apikey="test-key", client_agent="test:1:test")
"""The identity every call in these tests is made with."""

AGENT_TIMEOUT_SECONDS: float = 0.5
"""The bound on one agent query in these tests."""


class FakeDrainAgent:
    """A local TCP drain agent that sends a configured reply per port, like the host agents."""

    def __init__(self, replies: Mapping[int, str]) -> None:
        """Reply ``replies[port]`` for each port received. An unknown port gets ``ready``."""
        self.replies: dict[int, str] = dict(replies)
        """The reply line, without the newline, by replica port."""
        self.received_lines: list[bytes] = []
        """Every line sent by a client, in arrival order."""
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Return the port the agent listens on."""
        assert self._server is not None
        agent_port: int = self._server.sockets[0].getsockname()[1]
        return agent_port

    async def start(self) -> None:
        """Start listening on an ephemeral port."""
        self._server = await asyncio.start_server(self._reply, AGENT_HOST, 0)

    async def stop(self) -> None:
        """Stop listening, so connections to the port are refused."""
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _reply(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read one port line, send the configured reply for that port and close."""
        received_line = await reader.readline()
        self.received_lines.append(received_line)
        instance_port = int(received_line.strip())
        writer.write(f"{self.replies.get(instance_port, DrainAgentState.READY)}\n".encode("ascii"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()


@asynccontextmanager
async def running_agent(replies: Mapping[int, str]) -> AsyncIterator[FakeDrainAgent]:
    """Run a fake drain agent for the duration of the block."""
    agent = FakeDrainAgent(replies)
    await agent.start()
    try:
        yield agent
    finally:
        await agent.stop()


def tiered_client(http_client: httpx.AsyncClient) -> HordeClient:
    """Return a client with the primary and the fallback Horde instance."""
    return HordeClient(
        horde_urls=(PRIMARY_URL,),
        fallback_horde_urls=(FALLBACK_URL,),
        http_client=http_client,
        proxy_passkey=None,
        front_metrics=UNRECORDED_METRICS,
    )


def agent_states(horde_client: HordeClient) -> dict[str, DrainAgentState]:
    """Return the last drain agent state of each Horde instance from the routing view."""
    return {instance.horde_url: instance.agent_state for instance in horde_client.routing_view().instances}


def inflight_calls(horde_client: HordeClient) -> dict[str, int]:
    """Return the calls in flight of each Horde instance from the routing view."""
    return {instance.horde_url: instance.inflight_calls for instance in horde_client.routing_view().instances}


async def test_drained_primary_moves_calls_to_fallback() -> None:
    async with running_agent({PRIMARY_PORT: DrainAgentState.DRAIN}) as agent:
        with respx.mock(assert_all_called=False) as router:
            primary_route = router.get(f"{PRIMARY_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
            fallback_route = router.get(f"{FALLBACK_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
            async with httpx.AsyncClient() as http_client:
                horde_client = tiered_client(http_client)
                await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)
                calls_to_primary_before_poll = primary_route.call_count

                await horde_client.poll_drain_agents(agent_port=agent.port, timeout_seconds=AGENT_TIMEOUT_SECONDS)
                for _ in range(3):
                    await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)
                routing_view = horde_client.routing_view()
                agent_address = f"{AGENT_HOST}:{agent.port}"

    assert sorted(agent.received_lines) == [f"{PRIMARY_PORT}\n".encode(), f"{FALLBACK_PORT}\n".encode()]
    assert calls_to_primary_before_poll == 1
    assert primary_route.call_count == 1
    assert fallback_route.call_count == 3
    instances_by_url = {instance.horde_url: instance for instance in routing_view.instances}
    assert instances_by_url[PRIMARY_URL].tier == InstanceTier.PRIMARY
    assert instances_by_url[PRIMARY_URL].agent_state == DrainAgentState.DRAIN
    assert not instances_by_url[PRIMARY_URL].usable
    assert instances_by_url[FALLBACK_URL].tier == InstanceTier.FALLBACK
    assert instances_by_url[FALLBACK_URL].usable
    assert [drain_agent.agent_address for drain_agent in routing_view.drain_agents] == [agent_address]
    assert routing_view.drain_agents[0].last_success_unix_seconds is not None


async def test_ready_primary_is_preferred_over_a_ready_fallback() -> None:
    with respx.mock(assert_all_called=False) as router:
        primary_route = router.get(f"{PRIMARY_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        fallback_route = router.get(f"{FALLBACK_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        async with httpx.AsyncClient() as http_client:
            horde_client = tiered_client(http_client)
            for _ in range(4):
                await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)

    assert primary_route.call_count == 4
    assert fallback_route.call_count == 0


async def test_refused_primary_moves_the_same_call_to_fallback() -> None:
    with respx.mock() as router:
        primary_route = router.get(f"{PRIMARY_URL}{STATUS_PATH}").mock(side_effect=httpx.ConnectError("refused"))
        fallback_route = router.get(f"{FALLBACK_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        async with httpx.AsyncClient() as http_client:
            horde_client = tiered_client(http_client)
            status = await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)
            routing_view = horde_client.routing_view()

    assert status.done
    assert primary_route.call_count == 1
    assert fallback_route.call_count == 1
    primary_routing = next(instance for instance in routing_view.instances if instance.horde_url == PRIMARY_URL)
    assert primary_routing.unhealthy
    assert not primary_routing.usable


async def test_every_instance_drained_fails_unavailable_without_connecting() -> None:
    drained_replies = {PRIMARY_PORT: DrainAgentState.DRAIN, FALLBACK_PORT: DrainAgentState.DRAIN}
    async with running_agent(drained_replies) as agent:
        with respx.mock(assert_all_called=False) as router:
            status_routes = [
                router.get(f"{horde_url}{STATUS_PATH}").mock(side_effect=AssertionError) for horde_url in (PRIMARY_URL, FALLBACK_URL)
            ]
            submit_routes = [
                router.post(f"{horde_url}{SUBMIT_PATH}").mock(side_effect=AssertionError) for horde_url in (PRIMARY_URL, FALLBACK_URL)
            ]
            async with httpx.AsyncClient() as http_client:
                horde_client = tiered_client(http_client)
                await horde_client.poll_drain_agents(agent_port=agent.port, timeout_seconds=AGENT_TIMEOUT_SECONDS)
                with pytest.raises(HordeUnavailableError) as status_error:
                    await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)
                # A recovery may fall back to unhealthy Horde instances. It does not fall back to drained Horde instances.
                with pytest.raises(HordeUnavailableError) as recovery_error:
                    await horde_client.submit_chat(
                        request_body={"model": "m"}, credentials=CREDENTIALS, fall_back_to_unhealthy_instances=True
                    )

    assert not status_error.value.request_sent
    assert not recovery_error.value.request_sent
    assert all(route.call_count == 0 for route in status_routes + submit_routes)


async def test_unreachable_or_unexpected_agent_does_not_change_the_state() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PRIMARY_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        fallback_route = router.get(f"{FALLBACK_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        async with httpx.AsyncClient() as http_client:
            horde_client = tiered_client(http_client)
            async with running_agent({PRIMARY_PORT: DrainAgentState.DRAIN}) as agent:
                await horde_client.poll_drain_agents(agent_port=agent.port, timeout_seconds=AGENT_TIMEOUT_SECONDS)

                agent.replies[PRIMARY_PORT] = "maint"
                await horde_client.poll_drain_agents(agent_port=agent.port, timeout_seconds=AGENT_TIMEOUT_SECONDS)
                states_after_unexpected_reply = agent_states(horde_client)
                # The fallback Horde instance's reply from the same agent still counts as a success.
                last_success_before_refusal = horde_client.routing_view().drain_agents[0].last_success_unix_seconds
                stopped_agent_port = agent.port

            await horde_client.poll_drain_agents(agent_port=stopped_agent_port, timeout_seconds=AGENT_TIMEOUT_SECONDS)
            states_after_refused_agent = agent_states(horde_client)
            last_success_after_refusal = horde_client.routing_view().drain_agents[0].last_success_unix_seconds
            await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)

    expected_states = {PRIMARY_URL: DrainAgentState.DRAIN, FALLBACK_URL: DrainAgentState.READY}
    assert states_after_unexpected_reply == expected_states
    assert states_after_refused_agent == expected_states
    assert last_success_after_refusal == last_success_before_refusal
    assert fallback_route.call_count == 1


async def test_agent_that_does_not_reply_in_time_does_not_change_the_state() -> None:
    accepted = asyncio.Event()

    async def accept_and_stay_silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        await reader.read()
        writer.close()

    silent_server = await asyncio.start_server(accept_and_stay_silent, AGENT_HOST, 0)
    silent_port: int = silent_server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as http_client:
            horde_client = tiered_client(http_client)
            started_at = time.monotonic()
            await horde_client.poll_drain_agents(agent_port=silent_port, timeout_seconds=0.1)
            poll_duration = time.monotonic() - started_at
    finally:
        silent_server.close()
        await silent_server.wait_closed()

    assert accepted.is_set()
    assert poll_duration < CALL_WAIT_SECONDS
    assert agent_states(horde_client) == {PRIMARY_URL: DrainAgentState.READY, FALLBACK_URL: DrainAgentState.READY}
    assert horde_client.routing_view().drain_agents[0].last_success_unix_seconds is None


async def test_without_polling_every_instance_is_ready_and_fallbacks_are_unused() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PRIMARY_URL}{STATUS_PATH}").mock(return_value=httpx.Response(200, json=done_status()))
        async with httpx.AsyncClient() as http_client:
            horde_client = tiered_client(http_client)
            await horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS)
            routing_view = horde_client.routing_view()

    assert all(instance.agent_state == DrainAgentState.READY and instance.usable for instance in routing_view.instances)
    assert routing_view.drain_agents == []


async def test_instance_in_both_tiers_is_rejected() -> None:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ValueError, match="both primary and fallback"):
            HordeClient(
                horde_urls=(PRIMARY_URL,),
                fallback_horde_urls=(PRIMARY_URL,),
                http_client=http_client,
                proxy_passkey=None,
                front_metrics=UNRECORDED_METRICS,
            )


async def test_inflight_count_returns_to_zero_after_success_and_failure() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_call(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=done_status())

    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PRIMARY_URL}{STATUS_PATH}").mock(side_effect=hold_call)
        router.post(f"{PRIMARY_URL}{SUBMIT_PATH}").mock(side_effect=httpx.ReadError("connection reset"))
        async with httpx.AsyncClient() as http_client:
            horde_client = tiered_client(http_client)
            held_call = asyncio.create_task(horde_client.get_chat_status(request_id=REQUEST_ID, credentials=CREDENTIALS))
            await asyncio.wait_for(entered.wait(), timeout=CALL_WAIT_SECONDS)
            counts_while_held = inflight_calls(horde_client)
            release.set()
            await held_call
            counts_after_success = inflight_calls(horde_client)

            with pytest.raises(HordeUnavailableError):
                await horde_client.submit_chat(request_body={"model": "m"}, credentials=CREDENTIALS)
            counts_after_transport_failure = inflight_calls(horde_client)

            # The dropped submit marked the primary unhealthy. The cancel goes to the fallback and fails there.
            router.delete(f"{FALLBACK_URL}{STATUS_PATH}").mock(return_value=httpx.Response(500, json={"message": "boom"}))
            with pytest.raises(HordeAPIError, match="boom"):
                await horde_client.cancel_chat(request_id=REQUEST_ID, credentials=CREDENTIALS, slot_wait_seconds=1, call_timeout_seconds=1)
            counts_after_refusal = inflight_calls(horde_client)

    assert counts_while_held == {PRIMARY_URL: 1, FALLBACK_URL: 0}
    zero_counts = {PRIMARY_URL: 0, FALLBACK_URL: 0}
    assert counts_after_success == zero_counts
    assert counts_after_transport_failure == zero_counts
    assert counts_after_refusal == zero_counts


async def test_routing_route_serves_direct_requests_and_returns_404_to_forwarded_ones(start_front: StartFront) -> None:
    async with start_front() as front:
        direct_response = await front.client.get("/routing")
        forwarded_response = await front.client.get("/routing", headers={"X-Forwarded-For": "198.51.100.7"})
        unknown_path_response = await front.client.get("/no-such-route")
        openapi_paths = (await front.client.get("/openapi.json")).json()["paths"]

    assert direct_response.status_code == 200
    routing_body = direct_response.json()
    assert routing_body["drain_agents"] == []
    assert routing_body["instances"] == [
        {
            "horde_url": FAST_TEST_SETTINGS.horde_urls[0],
            "tier": "primary",
            "agent_state": "ready",
            "usable": True,
            "unhealthy": False,
            "inflight_calls": 0,
        },
    ]
    assert forwarded_response.status_code == 404
    assert forwarded_response.json() == unknown_path_response.json()
    assert "/routing" not in openapi_paths


async def test_front_lifespan_polls_the_drain_agents_only_when_a_port_is_set(start_front: StartFront) -> None:
    async with start_front() as front:
        poll_task_names = {task.get_name() for task in asyncio.all_tasks()}
        assert "horde-oai-drain-agent-poll" not in poll_task_names
        assert front.app.state.front.horde_client.routing_view().drain_agents == []

    async with running_agent({PRIMARY_PORT: DrainAgentState.DRAIN}) as agent:
        polling_settings = FAST_TEST_SETTINGS.model_copy(
            update={
                "horde_urls": (PRIMARY_URL,),
                "fallback_horde_urls": (FALLBACK_URL,),
                "drain_agent_port": agent.port,
                "drain_agent_poll_seconds": 0.05,
            },
        )
        async with start_front(polling_settings) as front:
            deadline = time.monotonic() + CALL_WAIT_SECONDS
            while agent_states(front.app.state.front.horde_client)[PRIMARY_URL] != DrainAgentState.DRAIN:
                assert time.monotonic() < deadline, "The front did not poll the drain agent."
                await asyncio.sleep(0.01)
            routing_body = (await front.client.get("/routing")).json()
        poll_task_names_after_stop = {task.get_name() for task in asyncio.all_tasks()}

    assert [instance["usable"] for instance in routing_body["instances"]] == [False, True]
    assert "horde-oai-drain-agent-poll" not in poll_task_names_after_stop
