# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Requests to client-supplied URLs: address checks before any connection, redirects, deadline and byte cap."""

from __future__ import annotations

import gzip
import json
import socket
import ssl
import time
from base64 import b64encode
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler
from secrets import token_urlsafe
from urllib.parse import quote

import pytest

import horde.outbound as outbound
from horde.outbound import (
    HTTPS_SCHEMES,
    OutboundRequestFailed,
    OutboundRequestRefused,
    OutboundResponse,
    OutboundResponseTooLarge,
    send_public_request,
)
from tests.local_http_server import (
    TEST_SERVER_ADDRESS,
    RecordedServer,
    allow_test_server_or_public,
    redirect_to,
    resolver,
    send_body,
    serve_recorded,
)

pytestmark = pytest.mark.unit

HOOK_HOST = "hook.example"
"""The made-up host name the tests resolve to the local server."""

UNROUTED_ADDRESS = "192.0.2.1"
"""An RFC 5737 documentation address. Networks do not route it, so a connection to it times out during the TCP handshake."""


@pytest.fixture
def server() -> Iterator[RecordedServer]:
    with serve_recorded() as recorded_server:
        yield recorded_server


@pytest.fixture
def generated_auth() -> tuple[str, str, str]:
    """Generate URL credentials and a caller header without committing reusable credential-like values."""
    return token_urlsafe(18), token_urlsafe(18), f"Bearer {token_urlsafe(24)}"


def _basic_auth_header(username: str, password: str) -> str:
    return "Basic " + b64encode(f"{username}:{password}".encode("latin-1")).decode("ascii")


def _url_credentials(username: str, password: str) -> str:
    return f"{quote(username, safe='')}:{quote(password, safe='')}"


def _send(url: str, **overrides: object) -> OutboundResponse:
    arguments: dict[str, object] = {
        "hop_timeout_seconds": 2,
        "max_redirects": 3,
        "max_response_bytes": 1024,
        "resolve": resolver({HOOK_HOST: [TEST_SERVER_ADDRESS], "internal.example": ["10.0.0.5"]}),
        "address_allowed": allow_test_server_or_public,
    }
    arguments.update(overrides)
    return send_public_request(url, **arguments)  # type: ignore[arg-type]


def _respond_slowly(delay_seconds: float) -> Callable[[BaseHTTPRequestHandler], None]:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        time.sleep(delay_seconds)
        send_body(b"late")(handler)

    return respond


def _trickle_body(byte_count: int, interval_seconds: float) -> Callable[[BaseHTTPRequestHandler], None]:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(byte_count))
        handler.end_headers()
        try:
            for _ in range(byte_count):
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(interval_seconds)
        except OSError:
            return

    return respond


def _stream_without_length(byte_count: int) -> Callable[[BaseHTTPRequestHandler], None]:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            handler.wfile.write(b"x" * byte_count)
        except OSError:
            return

    return respond


class TestAddressGuard:
    def test_a_name_resolving_to_a_private_address_is_refused_before_connecting(self, server: RecordedServer) -> None:
        with pytest.raises(OutboundRequestRefused):
            _send(f"http://internal.example:{server.port}/hook")

        assert server.requests == []

    def test_a_private_address_literal_is_refused_by_the_default_check(self) -> None:
        with pytest.raises(OutboundRequestRefused):
            send_public_request("http://10.0.0.5/hook", hop_timeout_seconds=2, max_redirects=0, max_response_bytes=1024)

    def test_a_redirect_to_a_private_address_is_refused(self, server: RecordedServer) -> None:
        server.respond = redirect_to("http://internal.example/secret")

        with pytest.raises(OutboundRequestRefused):
            _send(f"http://{HOOK_HOST}:{server.port}/hook")

        assert len(server.requests) == 1

    def test_a_scheme_the_caller_does_not_allow_is_refused_before_connecting(self, server: RecordedServer) -> None:
        with pytest.raises(OutboundRequestRefused):
            _send(f"http://{HOOK_HOST}:{server.port}/hook", allowed_schemes=HTTPS_SCHEMES)

        assert server.requests == []


class TestRequestAndRedirects:
    def test_a_json_post_reaches_the_checked_address_with_the_original_host(self, server: RecordedServer) -> None:
        server.respond = send_body(b"accepted")

        response = _send(f"http://{HOOK_HOST}:{server.port}/hook", method="POST", json_body={"id": "gen"}, max_redirects=0)

        assert response.status == 200
        assert response.body == b"accepted"
        assert len(server.requests) == 1
        assert server.requests[0].method == "POST"
        assert server.requests[0].host_header == f"{HOOK_HOST}:{server.port}"
        assert json.loads(server.requests[0].body) == {"id": "gen"}

    @pytest.mark.parametrize("status", [302, 307, 308])
    def test_a_post_with_redirects_disabled_does_not_follow_one(self, server: RecordedServer, status: int) -> None:
        server.respond = redirect_to(f"http://{HOOK_HOST}:{server.port}/elsewhere", status=status)

        with pytest.raises(OutboundRequestRefused):
            _send(f"http://{HOOK_HOST}:{server.port}/hook", method="POST", json_body={"id": "gen"}, max_redirects=0)

        assert [recorded.method for recorded in server.requests] == ["POST"]

    def test_a_redirect_to_a_public_address_is_followed(self, server: RecordedServer) -> None:
        responses = iter([redirect_to("/image"), send_body(b"image bytes")])
        server.respond = lambda handler: next(responses)(handler)

        response = _send(f"http://{HOOK_HOST}:{server.port}/a")

        assert response.body == b"image bytes"
        assert len(server.requests) == 2

    def test_the_body_of_a_status_other_than_2xx_is_not_read(self, server: RecordedServer) -> None:
        server.respond = send_body(b"x" * 4096, status=500)

        response = _send(f"http://{HOOK_HOST}:{server.port}/hook")

        assert response.status == 500
        assert response.body == b""


class TestAddressFallback:
    def test_connection_failure_tries_the_next_checked_address_without_resolving_again(
        self, server: RecordedServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server.respond = send_body(b"accepted")
        attempted = []
        resolved = []
        checked = []
        real_open = outbound._open_bound_response

        def resolve_once(host: str, port: int) -> list[str]:
            resolved.append((host, port))
            return ["8.8.8.8", TEST_SERVER_ADDRESS]

        def check(address: str) -> bool:
            checked.append(address)
            return allow_test_server_or_public(address)

        def open_response(*args: object, **kwargs: object) -> OutboundResponse:
            address = args[4]
            attempted.append(address)
            assert checked == ["8.8.8.8", TEST_SERVER_ADDRESS]
            if address == "8.8.8.8":
                raise outbound._ConnectionFailed("simulated unreachable address")
            return real_open(*args, **kwargs)

        monkeypatch.setattr(outbound, "_open_bound_response", open_response)
        response = _send(
            f"http://{HOOK_HOST}:{server.port}/hook",
            method="POST",
            json_body={"id": "gen"},
            resolve=resolve_once,
            address_allowed=check,
        )

        assert response.body == b"accepted"
        assert attempted == ["8.8.8.8", TEST_SERVER_ADDRESS]
        assert resolved == [(HOOK_HOST, server.port)]
        assert len(server.requests) == 1
        assert server.requests[0].method == "POST"
        assert json.loads(server.requests[0].body) == {"id": "gen"}

    def test_all_connection_failures_are_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempted = []

        def fail(*args: object, **kwargs: object) -> None:
            attempted.append(args[4])
            raise outbound._ConnectionFailed("simulated unreachable address")

        monkeypatch.setattr(outbound, "_open_bound_response", fail)
        with pytest.raises(OutboundRequestFailed) as failure:
            _send("http://hook.example/hook", resolve=lambda host, port: ["8.8.8.8", "1.1.1.1"])

        assert attempted == ["8.8.8.8", "1.1.1.1"]
        assert isinstance(failure.value, outbound._ConnectionFailed)

    @pytest.mark.parametrize("exhaust_deadline", [False, True])
    def test_each_attempt_uses_the_remaining_total_deadline(self, monkeypatch: pytest.MonkeyPatch, exhaust_deadline: bool) -> None:
        clock = [100.0]
        timeouts = []

        def fail(*args: object, **kwargs: object) -> None:
            timeouts.append(kwargs["timeout_seconds"])
            assert kwargs["deadline"] == 101.0
            clock[0] += 1.0 if exhaust_deadline else 0.4
            raise outbound._ConnectionFailed("simulated connect timeout")

        monkeypatch.setattr(outbound.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(outbound, "_open_bound_response", fail)
        with pytest.raises(OutboundRequestFailed):
            _send(
                "http://hook.example/hook",
                resolve=lambda host, port: ["8.8.8.8", "1.1.1.1"],
                hop_timeout_seconds=2,
                deadline_seconds=1,
            )

        assert timeouts == pytest.approx([1.0] if exhaust_deadline else [1.0, 0.6])

    def test_a_failure_after_sending_a_post_does_not_try_another_address(
        self, server: RecordedServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server.respond = send_body(b"accepted")
        attempted = []
        real_open = outbound._open_bound_response

        def fail_after_sending(*args: object, **kwargs: object) -> None:
            attempted.append(args[4])
            real_open(*args, **kwargs)
            raise OutboundRequestFailed("simulated failure after transmission")

        monkeypatch.setattr(outbound, "_open_bound_response", fail_after_sending)
        with pytest.raises(OutboundRequestFailed):
            _send(
                f"http://{HOOK_HOST}:{server.port}/hook",
                method="POST",
                json_body={"id": "gen"},
                resolve=lambda host, port: [TEST_SERVER_ADDRESS, "8.8.8.8"],
            )

        assert attempted == [TEST_SERVER_ADDRESS]
        assert len(server.requests) == 1

    def test_an_unsafe_later_address_prevents_all_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def unexpected_connect(*args: object, **kwargs: object) -> None:
            raise AssertionError("Every address must pass the check before connecting")

        monkeypatch.setattr(outbound, "_open_bound_response", unexpected_connect)
        with pytest.raises(OutboundRequestRefused):
            _send("http://hook.example/hook", resolve=lambda host, port: ["8.8.8.8", "10.0.0.1"])


class TestAuthorization:
    @pytest.mark.parametrize(
        "credential_case",
        ["plain", "escaped_delimiters", "latin1", "empty_password", "empty_username", "empty_pair", "username_only"],
    )
    @pytest.mark.parametrize("header_name", ["Authorization", "authorization", "AUTHORIZATION"])
    def test_url_credentials_match_basic_auth_and_override_caller_headers_without_mutation(
        self, server: RecordedServer, generated_auth: tuple[str, str, str], credential_case: str, header_name: str
    ) -> None:
        observed = []
        username, password, caller_header = generated_auth
        if credential_case == "escaped_delimiters":
            username += ":"
            password += "@:"
        elif credential_case == "latin1":
            username += chr(0xE9)
            password += chr(0xE4)
        elif credential_case == "empty_password":
            password = ""
        elif credential_case == "empty_username":
            username = ""
        elif credential_case == "empty_pair":
            username = password = ""
        credentials = quote(username, safe="") if credential_case == "username_only" else _url_credentials(username, password)
        expected = caller_header if credential_case in {"empty_pair", "username_only"} else _basic_auth_header(username, password)

        def respond(handler: BaseHTTPRequestHandler) -> None:
            observed.append(handler.headers.get_all("Authorization"))
            send_body(b"authorized")(handler)

        server.respond = respond
        headers = {header_name: caller_header, "X-Caller": "unchanged"}
        response = _send(f"http://{credentials}@{HOOK_HOST}:{server.port}/hook", headers=headers)

        assert response.body == b"authorized"
        assert observed == [[expected]]
        assert headers == {header_name: caller_header, "X-Caller": "unchanged"}
        assert server.host_headers == [f"{HOOK_HOST}:{server.port}"]

    @pytest.mark.parametrize("absolute", [False, True])
    @pytest.mark.parametrize("url_auth", [False, True])
    def test_same_origin_redirects_preserve_authorization(
        self, server: RecordedServer, generated_auth: tuple[str, str, str], absolute: bool, url_auth: bool
    ) -> None:
        observed = []
        username, password, caller_header = generated_auth
        location = f"http://{HOOK_HOST}:{server.port}/target" if absolute else "/target"

        def respond(handler: BaseHTTPRequestHandler) -> None:
            observed.append(handler.headers.get("Authorization"))
            (redirect_to(location) if len(observed) == 1 else send_body(b"authorized"))(handler)

        server.respond = respond
        credentials = _url_credentials(username, password) + "@" if url_auth else ""
        _send(f"http://{credentials}{HOOK_HOST}:{server.port}/hook", headers={"authorization": caller_header})

        expected = _basic_auth_header(username, password) if url_auth else caller_header
        assert observed == [expected, expected]

    @pytest.mark.parametrize("change", ["host", "port", "downgrade"])
    @pytest.mark.parametrize("url_auth", [False, True])
    def test_redirects_to_another_origin_strip_inherited_authorization(
        self, hook_tls_context: ssl.SSLContext, generated_auth: tuple[str, str, str], change: str, url_auth: bool
    ) -> None:
        observed = []
        username, password, caller_header = generated_auth
        context = hook_tls_context if change == "downgrade" else None
        with serve_recorded(ssl_context=context) as first, serve_recorded() as second:
            target_host = "other.example" if change == "host" else HOOK_HOST
            target_port = first.port if change == "host" else second.port

            def respond(handler: BaseHTTPRequestHandler) -> None:
                observed.append(handler.headers.get("Authorization"))
                response = redirect_to(f"http://{target_host}:{target_port}/target") if len(observed) == 1 else send_body(b"ok")
                response(handler)

            first.respond = second.respond = respond
            scheme = "https" if change == "downgrade" else "http"
            credentials = _url_credentials(username, password) + "@" if url_auth else ""
            _send(
                f"{scheme}://{credentials}{HOOK_HOST}:{first.port}/hook",
                headers={"authorization": caller_header},
                resolve=resolver({HOOK_HOST: [TEST_SERVER_ADDRESS], "other.example": [TEST_SERVER_ADDRESS]}),
            )

        expected = _basic_auth_header(username, password) if url_auth else caller_header
        assert observed == [expected, None]

    def test_a_redirect_target_can_supply_its_own_credentials(self, server: RecordedServer, generated_auth: tuple[str, str, str]) -> None:
        observed = []
        username, password, _ = generated_auth
        target_username, target_password = token_urlsafe(18), token_urlsafe(18)
        target_credentials = _url_credentials(target_username, target_password)

        def respond(handler: BaseHTTPRequestHandler) -> None:
            observed.append(handler.headers.get("Authorization"))
            response = (
                redirect_to(f"http://{target_credentials}@other.example:{server.port}/target")
                if len(observed) == 1
                else send_body(b"authorized")
            )
            response(handler)

        server.respond = respond
        _send(
            f"http://{_url_credentials(username, password)}@{HOOK_HOST}:{server.port}/hook",
            resolve=resolver({HOOK_HOST: [TEST_SERVER_ADDRESS], "other.example": [TEST_SERVER_ADDRESS]}),
        )

        assert observed == [_basic_auth_header(username, password), _basic_auth_header(target_username, target_password)]


class TestLimits:
    def test_a_declared_length_over_the_cap_is_refused(self, server: RecordedServer) -> None:
        server.respond = send_body(b"x" * 2048)

        with pytest.raises(OutboundResponseTooLarge):
            _send(f"http://{HOOK_HOST}:{server.port}/hook")

    def test_a_body_without_a_length_is_refused_once_it_passes_the_cap(self, server: RecordedServer) -> None:
        server.respond = _stream_without_length(256 * 1024)

        with pytest.raises(OutboundResponseTooLarge):
            _send(f"http://{HOOK_HOST}:{server.port}/hook")

    def test_a_slow_answer_fails_at_the_deadline(self, server: RecordedServer) -> None:
        server.respond = _respond_slowly(3)
        started = time.monotonic()

        with pytest.raises(OutboundRequestFailed):
            _send(f"http://{HOOK_HOST}:{server.port}/hook", hop_timeout_seconds=5, deadline_seconds=0.5)

        assert time.monotonic() - started < 2

    def test_a_trickled_body_fails_at_the_deadline(self, server: RecordedServer) -> None:
        """Data above the low-speed threshold must still be bounded by the total deadline."""
        server.respond = _trickle_body(byte_count=200, interval_seconds=0.05)
        started = time.monotonic()

        with pytest.raises(OutboundRequestFailed):
            _send(f"http://{HOOK_HOST}:{server.port}/hook", hop_timeout_seconds=1, deadline_seconds=1)

        assert time.monotonic() - started < 3


@pytest.mark.parametrize("url", ["http://example.com:bad/a", "http://[bad]/"])
def test_malformed_initial_url_is_refused_before_connection(server: RecordedServer, url: str) -> None:
    with pytest.raises(OutboundRequestRefused):
        _send(url)
    assert not server.requests


@pytest.mark.parametrize("location", ["http://example.com:bad/a", "http://[bad]/"])
def test_malformed_redirect_is_refused(server: RecordedServer, location: str) -> None:
    server.respond = redirect_to(location)
    with pytest.raises(OutboundRequestRefused):
        _send(f"http://{HOOK_HOST}:{server.port}/hook")
    assert len(server.requests) == 1


@pytest.mark.parametrize("phase", ["status", "header", "body", "closing_body", "chunk", "trailer"])
@pytest.mark.parametrize("https", [False, True])
def test_absolute_deadline_bounds_each_response_phase(phase: str, https: bool, request: pytest.FixtureRequest) -> None:
    # Partial headers and chunk framing can continue indefinitely while data arrives above the low-speed
    # threshold. The total deadline must bound these responses even before a complete line is available.
    prefixes = {
        "status": b"HTTP/1.1 200 ",
        "header": b"HTTP/1.1 200 OK\r\nX-Slow: ",
        "body": b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n",
        "closing_body": b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n",
        "chunk": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;slow=",
        "trailer": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX-Slow: ",
    }

    def trickle(handler: BaseHTTPRequestHandler) -> None:
        try:
            handler.wfile.write(prefixes[phase])
            handler.wfile.flush()
            for _ in range(100):
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(0.025)
        except OSError:
            pass

    context = request.getfixturevalue("hook_tls_context") if https else None
    with serve_recorded(ssl_context=context) as server:
        server.respond = trickle
        started = time.monotonic()
        with pytest.raises(OutboundRequestFailed):
            _send(f"{'https' if https else 'http'}://{HOOK_HOST}:{server.port}/slow", hop_timeout_seconds=0.5, deadline_seconds=0.2)
        elapsed = time.monotonic() - started
        assert len(server.requests) == 1
    assert 0.15 <= elapsed < 0.65


def test_https_response_succeeds_with_the_checked_host_and_deadline(hook_tls_context: ssl.SSLContext) -> None:
    with serve_recorded(ssl_context=hook_tls_context) as server:
        server.respond = send_body(b"verified TLS")
        response = _send(f"https://{HOOK_HOST}:{server.port}/image", deadline_seconds=1)
        assert len(server.requests) == 1
    assert response.body == b"verified TLS"


def test_dns_overrun_is_checked_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_resolve(_host: str, _port: int) -> list[str]:
        time.sleep(0.1)
        return [TEST_SERVER_ADDRESS]

    def unexpected_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("A resolver overrun must not start a connection")

    monkeypatch.setattr("horde.outbound._open_bound_response", unexpected_connect)
    with pytest.raises(OutboundRequestFailed):
        _send("http://slow.example/image", resolve=slow_resolve, deadline_seconds=0.02)


def test_environment_proxy_cannot_bypass_address_pinning(server: RecordedServer, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    server.respond = send_body(b"direct")
    assert _send(f"http://{HOOK_HOST}:{server.port}/").body == b"direct"


@pytest.mark.parametrize("trusted", [True, False])
def test_tls_rejects_wrong_hostname_or_untrusted_ca(
    hook_tls_context: ssl.SSLContext, monkeypatch: pytest.MonkeyPatch, trusted: bool
) -> None:
    import certifi

    if not trusted:
        monkeypatch.setattr(certifi, "where", lambda: str(certifi.__path__[0]) + "/cacert.pem")
    host = "wrong.example" if trusted else HOOK_HOST
    with serve_recorded(ssl_context=hook_tls_context) as server:
        with pytest.raises(OutboundRequestFailed):
            _send(f"https://{host}:{server.port}/", resolve=lambda *_: [TEST_SERVER_ADDRESS])
        assert not server.requests


@pytest.mark.parametrize("size", [10, 100_000])
def test_compressed_body_is_capped_after_decoding(server: RecordedServer, size: int) -> None:
    body = b"x" * size
    compressed = gzip.compress(body)

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(compressed)))
        handler.end_headers()
        handler.wfile.write(compressed)

    server.respond = respond
    if size > 1024:
        with pytest.raises(OutboundResponseTooLarge):
            _send(f"http://{HOOK_HOST}:{server.port}/")
    else:
        assert _send(f"http://{HOOK_HOST}:{server.port}/").body == body


def test_informational_headers_and_trailers_do_not_replace_final_headers(server: RecordedServer) -> None:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.wfile.write(
            b"HTTP/1.1 103 Early Hints\r\nLink: ignored\r\n\r\n"
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nX-Final: yes\r\n\r\n"
            b"2\r\nok\r\n0\r\nX-Final: trailer\r\n\r\n",
        )

    server.respond = respond
    response = _send(f"http://{HOOK_HOST}:{server.port}/")
    assert response.status == 200
    assert response.body == b"ok"
    assert response.headers["x-final"] == "yes"
    assert "Link" not in response.headers


def test_real_connection_refusal_tries_next_address(server: RecordedServer) -> None:
    server.respond = send_body(b"fallback")
    response = _send(
        f"http://{HOOK_HOST}:{server.port}/",
        resolve=lambda *_: ["127.0.0.2", TEST_SERVER_ADDRESS],
        address_allowed=lambda _: True,
        deadline_seconds=5,
    )
    assert response.body == b"fallback"
    assert len(server.requests) == 1


def test_real_connect_timeout_tries_next_address(server: RecordedServer) -> None:
    """A connect timeout happens before a request is sent, so the next checked address is tried."""
    server.respond = send_body(b"fallback")
    started = time.monotonic()
    response = _send(
        f"http://{HOOK_HOST}:{server.port}/",
        resolve=lambda *_: [UNROUTED_ADDRESS, TEST_SERVER_ADDRESS],
        address_allowed=lambda _: True,
        hop_timeout_seconds=0.5,
        deadline_seconds=5,
    )
    assert response.body == b"fallback"
    assert len(server.requests) == 1
    # A refused connection fails at once, so the elapsed time shows the first address timed out.
    assert time.monotonic() - started >= 0.5


def test_real_disconnect_after_post_does_not_replay(server: RecordedServer) -> None:
    def disconnect(handler: BaseHTTPRequestHandler) -> None:
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()

    server.respond = disconnect
    with pytest.raises(OutboundRequestFailed):
        _send(
            f"http://{HOOK_HOST}:{server.port}/",
            method="POST",
            json_body={"id": "once"},
            resolve=lambda *_: [TEST_SERVER_ADDRESS, TEST_SERVER_ADDRESS],
        )
    assert len(server.requests) == 1
