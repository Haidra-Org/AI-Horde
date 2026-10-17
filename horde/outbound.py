# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP requests to URLs a client supplied, without letting the URL reach a private address.

[`send_public_request`][horde.outbound.send_public_request] resolves each source image, chat image or webhook
hostname once per hop and refuses the request if any resolved address is not allowed. Connections use the checked IP directly to
prevent DNS rebinding. libcurl handles connections and transfer deadlines. urllib3 provides URL parsing and
case-insensitive headers.
"""

from __future__ import annotations

import ipaddress
import json
import math
import socket
import time
from base64 import b64encode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin

import certifi
import pycurl
import urllib3

__all__ = [
    "HTTP_REDIRECT_STATUSES",
    "HTTP_SCHEMES",
    "HTTPS_SCHEMES",
    "HostResolver",
    "OutboundRequestError",
    "OutboundRequestFailed",
    "OutboundRequestRefused",
    "OutboundResponse",
    "OutboundResponseTooLarge",
    "is_public_address",
    "resolve_host_addresses",
    "send_public_request",
]

HTTP_REDIRECT_STATUSES = frozenset(
    {
        301,
        302,
        303,
        307,
        308,
    },
)
"""Response statuses whose ``Location`` a request follows."""

HTTP_SCHEMES = frozenset(
    {
        "http",
        "https",
    },
)
"""The schemes a client URL may use when the caller accepts plain http."""

HTTPS_SCHEMES = frozenset(
    {
        "https",
    },
)
"""The schemes a client URL may use when the caller accepts only https."""

_IPV4_EMBEDDING_NETWORKS = (
    ipaddress.IPv6Network("::/96"),
    ipaddress.IPv6Network("64:ff9b::/96"),
)
"""IPv6 prefixes whose low 32 bits carry an IPv4 address: IPv4-compatible addresses and the RFC 6052 NAT64
well-known prefix. The standard library reports these as global whatever IPv4 address they carry, but a NAT64
translator or an automatic tunnel on the server's network routes them to that IPv4 address."""

HostResolver = Callable[[str, int], Sequence[str]]
"""Resolves a host name and port to the IP addresses a connection may use."""


class OutboundRequestError(Exception):
    """Base class of the errors a request to a client-supplied URL raises."""


class OutboundRequestRefused(OutboundRequestError):
    """The URL or redirect target is refused by the request policy.

    Raised before any connection is made to the refused hop: the scheme is not allowed, the host is missing or does
    not resolve, a resolved address is not allowed, or the redirect limit is passed.
    """


class OutboundRequestFailed(OutboundRequestError):
    """The connection or the exchange failed, timed out, or passed the total deadline."""


class OutboundResponseTooLarge(OutboundRequestError):
    """The response body declared or reached more bytes than the cap."""


@dataclass(frozen=True)
class OutboundResponse:
    """Represents the final response to a request to a client-supplied URL."""

    status: int
    """The HTTP status."""

    headers: Mapping[str, str]
    """The response headers, matched case-insensitively."""

    body: bytes
    """The body of a 2xx response. The body of any other status is not read and is empty."""


def resolve_host_addresses(host: str, port: int) -> list[str]:
    """Return every IP address the system resolver gives for a host.

    Args:
        host: The host name or IP literal.
        port: The port the connection will use.

    Returns:
        The addresses, in resolver order.

    Raises:
        OSError: If the name does not resolve.
    """
    return [str(address_info[4][0]) for address_info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def is_public_address(address: str) -> bool:
    """Return whether an IP address may be requested on a client's behalf.

    Private, loopback, link-local, multicast, reserved, shared and unspecified addresses are refused. An IPv6
    address that carries an IPv4 address (IPv4-mapped, IPv4-compatible or NAT64) is checked as that IPv4 address.

    Args:
        address: An IPv4 or IPv6 address, as a resolver returns it.

    Returns:
        True when the address is globally routable unicast.
    """
    parsed_address = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(parsed_address, ipaddress.IPv6Address):
        if parsed_address.ipv4_mapped is not None:
            parsed_address = parsed_address.ipv4_mapped
        elif any(parsed_address in network for network in _IPV4_EMBEDDING_NETWORKS):
            parsed_address = ipaddress.IPv4Address(int(parsed_address) & 0xFFFFFFFF)
    return parsed_address.is_global and not parsed_address.is_multicast


def _format_host_header(host: str, port: int, default_port: int) -> str:
    """Return the ``Host`` header for a URL's host and port, bracketing an IPv6 literal."""
    host_name = f"[{host}]" if ":" in host else host
    if port == default_port:
        return host_name
    return f"{host_name}:{port}"


class _ConnectionFailed(OutboundRequestFailed):
    """Connection establishment failed before an HTTP request could be sent."""


def _open_bound_response(
    scheme: str,
    host: str,
    port: int,
    request_target: str,
    address: str,
    *,
    method: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
    deadline: float | None,
    max_response_bytes: int,
) -> OutboundResponse:
    """Transfer one hop through libcurl, pinned to a checked numeric address.

    CONNECT_TO changes only the socket destination, preserving the URL hostname for TLS and HTTP.
    Automatic redirects and environment proxies are disabled. Every hop must pass the caller's address policy.
    TIMEOUT_MS bounds the entire transfer, including partial headers and chunk framing. LOW_SPEED_TIME
    aborts transfers below one byte per second for the configured whole-second window.
    """
    # Numeric resolver results prevent CONNECT_TO from performing another DNS lookup.
    ipaddress.ip_address(address.split("%", 1)[0])
    target_address = f"[{address}]" if ":" in address else address
    authority = _format_host_header(host, port, 443 if scheme == "https" else 80)
    response_headers = urllib3.HTTPHeaderDict()
    response_body = bytearray()
    status = 0
    headers_complete = False
    stop_without_body = False
    too_large = False

    def receive_header(line: bytes) -> int:
        nonlocal status, headers_complete, stop_without_body, too_large
        if line.startswith(b"HTTP/"):
            status = int(line.split(b" ", 2)[1])
            response_headers.clear()
            headers_complete = False
        elif line == b"\r\n" and not headers_complete:
            if 100 <= status < 200 and status != 101:
                return len(line)
            headers_complete = True
            if not 200 <= status < 300:
                stop_without_body = True
                return 0
            length = response_headers.get("Content-Length", "")
            if length.isdigit() and int(length) > max_response_bytes:
                too_large = True
                return 0
        elif not headers_complete and b":" in line:
            name, value = line.decode("latin-1").split(":", 1)
            response_headers.add(name, value.strip())
        return len(line)

    def receive_body(chunk: bytes) -> int:
        nonlocal too_large
        if len(response_body) + len(chunk) > max_response_bytes:
            too_large = True
            return 0
        response_body.extend(chunk)
        return len(chunk)

    with pycurl.Curl() as curl:
        curl.setopt(pycurl.URL, f"{scheme}://{authority}{request_target}")
        curl.setopt(pycurl.CONNECT_TO, [f"::{target_address}:{port}"])
        curl.setopt(pycurl.PROXY, "")
        curl.setopt(pycurl.FOLLOWLOCATION, False)
        curl.setopt(pycurl.PATH_AS_IS, True)
        curl.setopt(pycurl.PROTOCOLS, pycurl.PROTO_HTTP | pycurl.PROTO_HTTPS)
        curl.setopt(pycurl.NOSIGNAL, True)
        curl.setopt(pycurl.SSL_VERIFYPEER, True)
        curl.setopt(pycurl.SSL_VERIFYHOST, 2)
        curl.setopt(pycurl.CAINFO, certifi.where())
        curl.setopt(pycurl.CONNECTTIMEOUT_MS, max(1, math.ceil(timeout_seconds * 1000)))
        curl.setopt(pycurl.LOW_SPEED_LIMIT, 1)
        curl.setopt(pycurl.LOW_SPEED_TIME, max(1, math.ceil(timeout_seconds)))
        if deadline is not None:
            remaining = _remaining_seconds(deadline, float("inf"))
            curl.setopt(pycurl.TIMEOUT_MS, max(1, math.ceil(remaining * 1000)))
        request_headers = urllib3.HTTPHeaderDict(headers)
        request_headers["Host"] = authority
        # Avoid libcurl's automatic Expect/continue exchange for JSON webhook payloads.
        if "Expect" not in request_headers:
            request_headers["Expect"] = ""
        curl.setopt(pycurl.HTTPHEADER, [f"{key}: {value}".encode("latin-1") for key, value in request_headers.items()])
        curl.setopt(pycurl.ACCEPT_ENCODING, "gzip, deflate")
        if body is not None:
            curl.setopt(pycurl.POSTFIELDS, body)
        curl.setopt(pycurl.CUSTOMREQUEST, method)
        curl.setopt(pycurl.HEADERFUNCTION, receive_header)
        curl.setopt(pycurl.WRITEFUNCTION, receive_body)
        try:
            curl.perform()
        except pycurl.error as error:
            if too_large:
                raise OutboundResponseTooLarge(f"The response passed {max_response_bytes} bytes.") from error
            if not (stop_without_body and error.args[0] == pycurl.E_WRITE_ERROR):
                # A timeout after TCP establishment (including TLS) is deliberately not replayed.
                failed_to_connect = error.args[0] == pycurl.E_COULDNT_CONNECT or (
                    error.args[0] == pycurl.E_OPERATION_TIMEDOUT and not curl.getinfo(pycurl.PRIMARY_IP)
                )
                error_type = _ConnectionFailed if failed_to_connect else OutboundRequestFailed
                raise error_type(f"The request to the URL failed: {error}") from error
    return OutboundResponse(status=status, headers=response_headers, body=bytes(response_body))


def _remaining_seconds(deadline: float | None, hop_timeout_seconds: float) -> float:
    """Return the smaller of the supplied timeout and the time left before the deadline."""
    if deadline is None:
        return hop_timeout_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OutboundRequestFailed("The request passed its total deadline.")
    return min(hop_timeout_seconds, remaining)


def send_public_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    hop_timeout_seconds: float,
    deadline_seconds: float | None = None,
    max_redirects: int,
    max_response_bytes: int,
    allowed_schemes: frozenset[str] = HTTP_SCHEMES,
    resolve: HostResolver = resolve_host_addresses,
    address_allowed: Callable[[str], bool] = is_public_address,
) -> OutboundResponse:
    """Send a request to a client-supplied URL without letting the URL, or a redirect, reach a private address.

    Every hop resolves its host once, is refused when any resolved address is not allowed, and connects to the
    checked addresses in order until a connection succeeds (see [`_open_bound_response`][horde.outbound._open_bound_response]).
    Only connection-establishment failures try another address. The client does not replay a request that may have
    been sent. Connections use checked IPs without resolving the hostname again to prevent DNS rebinding between
    validation and connection. Redirect targets pass the same checks and use the same method and body.

    Args:
        url: The URL a client supplied.
        method: The HTTP method.
        headers: Extra request headers. URL credentials override Authorization with HTTP Basic authentication.
            Authorization is preserved on same-origin redirects and stripped when the scheme, host or port changes.
            Credentials in a redirect URL may supply authorization for that target.
        json_body: When not None, sent as the JSON request body.
        hop_timeout_seconds: Connect timeout and low-speed window (rounded up to whole seconds at <1 byte/s).
        deadline_seconds: Connection and response deadline across redirects. Synchronous system DNS cannot be
            interrupted. Its elapsed time is checked before connecting. libcurl bounds the entire transfer,
            including status, headers, body and chunk framing.
        max_redirects: The most redirects followed. A redirect past it is refused, so 0 refuses every redirect.
        max_response_bytes: The most bytes a 2xx body may have.
        allowed_schemes: The URL schemes each hop may use.
        resolve: Resolves a host and port to addresses.
        address_allowed: Whether a resolved address may be connected to.

    Returns:
        The final response. A redirect status is final only when it carries no ``Location``.

    Raises:
        OutboundRequestRefused: When a hop's scheme is not allowed, its host is missing, does not resolve or resolves
            to an address that is not allowed, or there are more than ``max_redirects`` redirects.
        OutboundRequestFailed: When a connection or read fails or times out, or the deadline passes.
        OutboundResponseTooLarge: When a 2xx body declares or reaches more than ``max_response_bytes``.
    """
    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    request_headers = urllib3.HTTPHeaderDict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    current_url = url
    previous_origin = None
    for _ in range(max_redirects + 1):
        try:
            parsed_url = urllib3.util.parse_url(current_url)
        except ValueError as err:
            raise OutboundRequestRefused("The URL is malformed.") from err
        scheme = (parsed_url.scheme or "").lower()
        if scheme not in allowed_schemes or not parsed_url.host:
            raise OutboundRequestRefused(f"The URL must use {' or '.join(sorted(allowed_schemes))} and have a host.")
        host = parsed_url.host.strip("[]")
        port = parsed_url.port or (443 if scheme == "https" else 80)
        origin = (scheme, host.lower(), port)
        if previous_origin is not None and origin != previous_origin:
            request_headers.pop("Authorization", None)
        previous_origin = origin
        if parsed_url.auth is not None:
            username, separator, password = parsed_url.auth.partition(":")
            username, password = unquote(username), unquote(password)
            if separator and (username or password):
                try:
                    credentials = f"{username}:{password}".encode("latin-1")
                except UnicodeEncodeError as err:
                    raise OutboundRequestRefused("The URL credentials cannot be encoded as HTTP Basic authentication.") from err
                request_headers["Authorization"] = "Basic " + b64encode(credentials).decode("ascii")
        try:
            addresses = list(resolve(host, port))
        except OSError as err:
            raise OutboundRequestRefused("The URL's host does not resolve.") from err
        if not addresses or not all(address_allowed(address) for address in addresses):
            raise OutboundRequestRefused("The URL resolves to an address the horde does not connect to.")
        for index, address in enumerate(addresses):
            try:
                response = _open_bound_response(
                    scheme,
                    host,
                    port,
                    parsed_url.request_uri,
                    address,
                    method=method,
                    headers=request_headers,
                    body=body,
                    timeout_seconds=_remaining_seconds(deadline, hop_timeout_seconds),
                    deadline=deadline,
                    max_response_bytes=max_response_bytes,
                )
                break
            except _ConnectionFailed:
                if index == len(addresses) - 1:
                    raise
        location = response.headers.get("Location")
        if response.status in HTTP_REDIRECT_STATUSES and location:
            try:
                current_url = urljoin(current_url, location)
            except ValueError as err:
                raise OutboundRequestRefused("The redirect URL is malformed.") from err
            continue
        return response
    raise OutboundRequestRefused(f"The URL redirected more than {max_redirects} times.")
