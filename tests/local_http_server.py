# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A local HTTP server and fake resolvers for exercising requests to client-supplied URLs.

The server listens on [`TEST_SERVER_ADDRESS`][tests.local_http_server.TEST_SERVER_ADDRESS], a loopback address the
production address check refuses, so tests resolve their made-up host names to it and pass
[`allow_test_server_or_public`][tests.local_http_server.allow_test_server_or_public] as the address check.
"""

from __future__ import annotations

import ssl
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from horde.outbound import is_public_address

__all__ = [
    "TEST_SERVER_ADDRESS",
    "RecordedRequest",
    "RecordedServer",
    "redirect_to",
    "resolver",
    "send_body",
    "serve_recorded",
    "allow_test_server_or_public",
]

TEST_SERVER_ADDRESS = "127.0.0.1"
"""The address the local test server listens on, which the tests' address check treats as the only public one."""


@dataclass(frozen=True)
class RecordedRequest:
    """Represents one request the local server received."""

    method: str
    """The HTTP method."""

    host_header: str
    """The ``Host`` header."""

    body: bytes
    """The request body."""


@dataclass
class RecordedServer:
    """Represents a local HTTP server whose response each test sets, recording the requests it receives."""

    port: int
    """The port it listens on."""

    respond: Callable[[BaseHTTPRequestHandler], None] = lambda handler: handler.send_error(500)
    """Writes the response to one request."""

    requests: list[RecordedRequest] = field(default_factory=list)
    """Each request received, in order."""

    @property
    def host_headers(self) -> list[str]:
        """The ``Host`` header of each request received, in order."""
        return [recorded.host_header for recorded in self.requests]


@contextmanager
def serve_recorded(*, ssl_context: ssl.SSLContext | None = None) -> Iterator[RecordedServer]:
    """Run a local server for the duration of the block."""
    recorded_server = RecordedServer(port=0)

    class RecordingHandler(BaseHTTPRequestHandler):
        def _record_and_respond(self) -> None:
            body_length = int(self.headers.get("Content-Length") or 0)
            recorded_server.requests.append(
                RecordedRequest(
                    method=self.command,
                    host_header=self.headers["Host"],
                    body=self.rfile.read(body_length),
                ),
            )
            recorded_server.respond(self)

        def do_GET(self) -> None:  # noqa: N802 (http.server handler name)
            self._record_and_respond()

        def do_POST(self) -> None:  # noqa: N802 (http.server handler name)
            self._record_and_respond()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (http.server signature)
            return

    server = ThreadingHTTPServer((TEST_SERVER_ADDRESS, 0), RecordingHandler)
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    recorded_server.port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield recorded_server
    finally:
        server.shutdown()
        server.server_close()


def send_body(body: bytes, status: int = 200) -> Callable[[BaseHTTPRequestHandler], None]:
    """Return a responder that sends ``body`` with ``status``."""

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    return respond


def redirect_to(location: str, status: int = 302) -> Callable[[BaseHTTPRequestHandler], None]:
    """Return a responder that redirects to ``location``."""

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return respond


def allow_test_server_or_public(address: str) -> bool:
    """Allow the local test server's address as well as every public one."""
    return address == TEST_SERVER_ADDRESS or is_public_address(address)


def resolver(addresses_by_host: dict[str, list[str]]) -> Callable[[str, int], list[str]]:
    """Return a resolver that returns the given addresses for each host."""

    def resolve(host: str, port: int) -> list[str]:
        return addresses_by_host[host]

    return resolve
