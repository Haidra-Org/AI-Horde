# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fetching a client's image URL, for chat images and source images: address checks bound to the connection,
redirects, byte caps, and JPEG encoding."""

from __future__ import annotations

import functools
import ssl
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler
from io import BytesIO

import pytest
from PIL import Image

from horde import exceptions as e
from horde import image as horde_image
from horde.image import (
    MAX_IMAGE_FETCH_REDIRECTS,
    encode_chat_image,
    ensure_source_image_uploaded,
    fetch_public_image,
    load_chat_image,
)
from horde.outbound import is_public_address
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


@pytest.fixture
def image_server() -> Iterator[RecordedServer]:
    with serve_recorded() as recorded_server:
        yield recorded_server


def _png_bytes(size: tuple[int, int]) -> bytes:
    image_buffer = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(image_buffer, format="PNG")
    return image_buffer.getvalue()


class TestAddressCheck:
    @pytest.mark.parametrize(
        "address",
        [
            "10.0.0.1",
            "172.16.5.4",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "ff02::1",
            "::ffff:10.0.0.1",
            "::ffff:127.0.0.1",
            "::127.0.0.1",
            "::10.0.0.1",
            "64:ff9b::127.0.0.1",
            "64:ff9b::169.254.169.254",
            "64:ff9b:1::10.0.0.1",
        ],
    )
    def test_private_loopback_link_local_and_multicast_addresses_are_not_public(self, address: str) -> None:
        assert is_public_address(address) is False

    @pytest.mark.parametrize(
        "address",
        [
            "93.184.216.34",
            "1.1.1.1",
            "2606:4700:4700::1111",
            "::ffff:93.184.216.34",
            "64:ff9b::93.184.216.34",
        ],
    )
    def test_global_unicast_addresses_are_public(self, address: str) -> None:
        assert is_public_address(address) is True


class TestBoundFetch:
    def test_the_connection_goes_to_the_checked_address_with_the_original_host(self, image_server: RecordedServer) -> None:
        """A name whose second resolution returns a private address cannot redirect the connection there."""
        image_server.respond = send_body(b"image bytes")
        answers = iter([[TEST_SERVER_ADDRESS], ["10.0.0.7"]])
        resolved_hosts: list[str] = []

        def rebinding_resolver(host: str, port: int) -> list[str]:
            resolved_hosts.append(host)
            return next(answers)

        body = fetch_public_image(
            f"http://images.example:{image_server.port}/a.png",
            resolve=rebinding_resolver,
            address_allowed=lambda address: address == TEST_SERVER_ADDRESS,
        )

        assert body == b"image bytes"
        assert resolved_hosts == ["images.example"]
        assert image_server.host_headers == [f"images.example:{image_server.port}"]

    @pytest.mark.parametrize(
        "addresses",
        [
            ["10.1.2.3"],
            ["93.184.216.34", "127.0.0.1"],
            [],
        ],
    )
    def test_a_host_with_any_private_address_or_none_is_refused_before_connecting(self, addresses: list[str]) -> None:
        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image("https://images.example/a.png", resolve=resolver({"images.example": addresses}))

        assert raised.value.rc == "SourceImageUrlRefused"

    def test_a_host_that_does_not_resolve_is_refused(self) -> None:
        def failing_resolver(host: str, port: int) -> list[str]:
            raise OSError("name does not resolve")

        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image("https://images.example/a.png", resolve=failing_resolver)

        assert raised.value.rc == "SourceImageUrlRefused"

    def test_a_redirect_to_a_private_address_is_refused(self, image_server: RecordedServer) -> None:
        image_server.respond = redirect_to("http://internal.example/secret")
        resolve = resolver({"images.example": [TEST_SERVER_ADDRESS], "internal.example": ["10.0.0.5"]})

        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image(
                f"http://images.example:{image_server.port}/a.png", resolve=resolve, address_allowed=allow_test_server_or_public
            )

        assert raised.value.rc == "SourceImageUrlRefused"
        assert len(image_server.host_headers) == 1

    def test_a_redirect_to_another_scheme_is_refused(self, image_server: RecordedServer) -> None:
        image_server.respond = redirect_to("file:///etc/passwd")

        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image(
                f"http://images.example:{image_server.port}/a.png",
                resolve=resolver({"images.example": [TEST_SERVER_ADDRESS]}),
                address_allowed=allow_test_server_or_public,
            )

        assert raised.value.rc == "SourceImageUrlRefused"

    def test_redirects_are_followed_up_to_the_cap_and_refused_past_it(self, image_server: RecordedServer) -> None:
        image_server.respond = redirect_to("/again")

        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image(
                f"http://images.example:{image_server.port}/a.png",
                resolve=resolver({"images.example": [TEST_SERVER_ADDRESS]}),
                address_allowed=allow_test_server_or_public,
            )

        assert raised.value.rc == "SourceImageUrlRefused"
        assert len(image_server.host_headers) == MAX_IMAGE_FETCH_REDIRECTS + 1

    def test_a_body_over_the_byte_cap_is_refused(self, image_server: RecordedServer) -> None:
        image_server.respond = send_body(b"x" * 2048)

        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image(
                f"http://images.example:{image_server.port}/a.png",
                max_bytes=1024,
                resolve=resolver({"images.example": [TEST_SERVER_ADDRESS]}),
                address_allowed=allow_test_server_or_public,
            )

        assert raised.value.rc == "SourceImageSizeExceeded"

    def test_a_status_other_than_ok_is_refused(self, image_server: RecordedServer) -> None:
        image_server.respond = send_body(b"missing", status=404)

        with pytest.raises(e.ImageValidationFailed) as raised:
            fetch_public_image(
                f"http://images.example:{image_server.port}/a.png",
                resolve=resolver({"images.example": [TEST_SERVER_ADDRESS]}),
                address_allowed=allow_test_server_or_public,
            )

        assert raised.value.rc == "SourceImageUrlInvalid"

    def test_an_https_connection_is_made_to_the_address_and_verified_against_the_host(self, hook_tls_context: ssl.SSLContext) -> None:
        paths = []
        image = _png_bytes((2, 2))

        def respond(handler: BaseHTTPRequestHandler) -> None:
            paths.append(handler.path)
            send_body(image)(handler)

        with serve_recorded(ssl_context=hook_tls_context) as server:
            server.respond = respond
            result = fetch_public_image(
                f"https://hook.example:{server.port}/a.png?size=1",
                resolve=resolver({"hook.example": [TEST_SERVER_ADDRESS]}),
                address_allowed=allow_test_server_or_public,
            )
            assert server.host_headers == [f"hook.example:{server.port}"]
        assert result == image
        assert paths == ["/a.png?size=1"]


class TestChatImageLoading:
    def test_an_image_url_resolving_to_a_private_address_is_refused(self) -> None:
        with pytest.raises(e.ImageValidationFailed) as raised:
            load_chat_image("https://images.example/a.png", resolve=resolver({"images.example": ["192.168.0.10"]}))

        assert raised.value.rc == "SourceImageUrlRefused"

    def test_a_normalised_image_is_encoded_as_jpeg(self) -> None:
        encoded = encode_chat_image(Image.open(BytesIO(_png_bytes((40, 30)))).convert("RGB"))

        assert encoded.startswith(b"\xff\xd8")
        with Image.open(BytesIO(encoded)) as stored_image:
            assert stored_image.format == "JPEG"
            assert stored_image.mode == "RGB"
            assert stored_image.size == (40, 30)

    @pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
    def test_a_transparent_image_is_flattened_onto_white(self, mode: str) -> None:
        transparent_image = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
        transparent_image.paste((0, 0, 0, 255), (0, 0, 20, 30))
        if mode != "RGBA":
            transparent_image = transparent_image.convert(mode)
        assert transparent_image.has_transparency_data

        encoded = encode_chat_image(transparent_image)

        assert encoded.startswith(b"\xff\xd8")
        with Image.open(BytesIO(encoded)) as stored_image:
            assert stored_image.format == "JPEG"
            assert stored_image.mode == "RGB"
            assert all(channel > 240 for channel in stored_image.getpixel((35, 15)))
            assert all(channel < 15 for channel in stored_image.getpixel((5, 15)))


class TestSourceImageUrl:
    """A source image URL on an image request is fetched through the same address guard as a chat image URL."""

    @pytest.fixture
    def guarded_fetch(self, monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, list[str]]], None]:
        def use_resolver(addresses_by_host: dict[str, list[str]]) -> None:
            monkeypatch.setattr(
                horde_image,
                "fetch_public_image",
                functools.partial(fetch_public_image, resolve=resolver(addresses_by_host), address_allowed=allow_test_server_or_public),
            )

        return use_resolver

    def test_a_url_resolving_to_a_private_address_is_refused(self, guarded_fetch: Callable[[dict[str, list[str]]], None]) -> None:
        guarded_fetch({"images.example": ["192.168.0.10"]})

        with pytest.raises(e.ImageValidationFailed) as raised:
            ensure_source_image_uploaded("https://images.example/a.png", "request_src")

        assert raised.value.rc == "SourceImageUrlRefused"

    @pytest.mark.parametrize("image_url", ["http://10.0.0.5/a.png", "http://127.0.0.1:9/a.png", "http://[::1]/a.png"])
    def test_a_url_at_a_private_address_literal_is_refused(self, image_url: str) -> None:
        with pytest.raises(e.ImageValidationFailed) as raised:
            ensure_source_image_uploaded(image_url, "request_src")

        assert raised.value.rc == "SourceImageUrlRefused"

    def test_a_redirect_to_a_private_address_is_refused(
        self,
        image_server: RecordedServer,
        guarded_fetch: Callable[[dict[str, list[str]]], None],
    ) -> None:
        image_server.respond = redirect_to("http://internal.example/secret")
        guarded_fetch({"images.example": [TEST_SERVER_ADDRESS], "internal.example": ["10.0.0.5"]})

        with pytest.raises(e.ImageValidationFailed) as raised:
            ensure_source_image_uploaded(f"http://images.example:{image_server.port}/a.png", "request_src")

        assert raised.value.rc == "SourceImageUrlRefused"
        assert len(image_server.host_headers) == 1

    def test_a_public_url_serving_an_image_is_kept_as_the_source(
        self,
        image_server: RecordedServer,
        guarded_fetch: Callable[[dict[str, list[str]]], None],
    ) -> None:
        image_server.respond = send_body(_png_bytes((40, 30)))
        guarded_fetch({"images.example": [TEST_SERVER_ADDRESS]})
        image_url = f"http://images.example:{image_server.port}/a.png"

        source_image, image, stored_in_r2 = ensure_source_image_uploaded(image_url, "request_src")

        assert source_image == image_url
        assert image.size == (40, 30)
        assert stored_in_r2 is False
        assert image_server.host_headers == [f"images.example:{image_server.port}"]

    def test_a_url_serving_something_other_than_an_image_is_invalid(
        self,
        image_server: RecordedServer,
        guarded_fetch: Callable[[dict[str, list[str]]], None],
    ) -> None:
        image_server.respond = send_body(b"<html>not an image</html>")
        guarded_fetch({"images.example": [TEST_SERVER_ADDRESS]})

        with pytest.raises(e.ImageValidationFailed) as raised:
            ensure_source_image_uploaded(f"http://images.example:{image_server.port}/a.png", "request_src")

        assert raised.value.rc == "SourceImageUrlInvalid"


def test_image_fetch_passes_the_total_deadline_to_the_guarded_client(image_server: RecordedServer, monkeypatch: pytest.MonkeyPatch) -> None:
    def trickle(handler) -> None:
        try:
            handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            for _ in range(100):
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(0.025)
        except OSError:
            pass

    image_server.respond = trickle
    monkeypatch.setattr(horde_image, "IMAGE_FETCH_DEADLINE_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(e.ImageValidationFailed):
        fetch_public_image(
            f"http://images.example:{image_server.port}/slow",
            resolve=resolver({"images.example": [TEST_SERVER_ADDRESS]}),
            address_allowed=allow_test_server_or_public,
        )
    assert 0.15 <= time.monotonic() - started < 0.65
    assert len(image_server.requests) == 1
