# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import base64
import binascii
import re
from collections.abc import Callable
from io import BytesIO

import logfire
from PIL import Image, UnidentifiedImageError

from horde.exceptions import ImageValidationFailed
from horde.logger import logger
from horde.outbound import (
    HTTP_REDIRECT_STATUSES,
    HostResolver,
    OutboundRequestFailed,
    OutboundRequestRefused,
    OutboundResponseTooLarge,
    is_public_address,
    resolve_host_addresses,
    send_public_request,
)
from horde.r2 import upload_source_image

MAX_SOURCE_IMAGE_PIXELS = 3072 * 3072
"""The most pixels a source image may have. It bounds the memory and time spent decoding and re-encoding an
image a client sent."""

IMAGE_DATA_URL_PATTERN = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,(?P<base64_payload>.*)", re.DOTALL)
"""A base64 image data URL, the form an OpenAI image part carries an inline image in. The declared media type is
not trusted; the decoded bytes are opened as whatever image they are."""

MAX_CHAT_IMAGE_SIDE = 2048
"""The longest side a stored chat image keeps. koboldcpp's vision projector reads at most 2048 pixels per side, so
a larger image costs transfer without informing the reply."""

CHAT_IMAGE_JPEG_QUALITY = 90
"""The JPEG quality a chat image is stored at. JPEG is a format every chat worker's image loader decodes: koboldcpp
reads images with llama.cpp's multimodal loader, which decodes PNG and JPEG and does not decode WebP. The quality 50
source images are stored at blurs small text, which vision prompts often ask the model to read, such as the text in a
screenshot."""

CHAT_IMAGE_BACKGROUND = (255, 255, 255)
"""The colour transparent areas of a chat image are flattened onto before encoding, since JPEG holds no alpha and
vision models do not read it. White matches how most documents and screenshots with transparency are viewed."""

MAX_FETCHED_IMAGE_BYTES = 5 * 1024 * 1024
"""The most bytes an image fetched from a client's URL may have, as declared and after decoding. The cap bounds the
memory the server holds for one source image URL before parsing the image."""

IMAGE_FETCH_TIMEOUT_SECONDS = 2
"""Connect timeout and libcurl low-speed window for an image URL. The total deadline bounds trickled responses."""

IMAGE_FETCH_DEADLINE_SECONDS = 10
"""The connection and response budget per image URL, across redirects. A synchronous DNS lookup can exceed it."""

MAX_IMAGE_FETCH_REDIRECTS = 3
"""The most redirects an image URL fetch follows. Each hop is resolved and checked again, so the cap also bounds
the resolutions one URL can cause."""


def convert_b64_to_pil(source_image_b64):
    base64_bytes = source_image_b64.encode("utf-8")
    try:
        img_bytes = base64.b64decode(base64_bytes)
    except Exception:
        return None
    try:
        image = Image.open(BytesIO(img_bytes))
    except UnidentifiedImageError:
        return None
    return image


def convert_pil_to_b64(source_image, quality=95):
    buffer = BytesIO()
    source_image.save(buffer, format="webp", exact=True)
    img_bytes = buffer.getvalue()
    return base64.b64encode(img_bytes).decode("utf-8")


# TODO: Merge with convert_b64_to_pil()
def convert_source_image_to_pil(source_image_b64):
    base64_bytes = source_image_b64.encode("utf-8")
    img_bytes = base64.b64decode(base64_bytes)
    image = Image.open(BytesIO(img_bytes))
    width, height = image.size
    resolution = width * height
    resolution_threshold = MAX_SOURCE_IMAGE_PIXELS
    if resolution > resolution_threshold:
        except_msg = "Image size cannot exceed 3072*3072 pixels"
        # Not sure e exists here?
        raise ImageValidationFailed(except_msg, rc="SourceImageResolutionExceeded")
    quality = 100
    # We adjust the amount of compression based on the starting image to avoid running out of bandwidth
    if resolution > resolution_threshold * 0.9:
        quality = 50
    elif resolution > resolution_threshold * 0.8:
        quality = 60
    elif resolution > resolution_threshold * 0.6:
        logger.debug([resolution, resolution_threshold * 0.6])
        quality = 70
    elif resolution > resolution_threshold * 0.4:
        logger.debug([resolution, resolution_threshold * 0.4])
        quality = 80
    elif resolution > resolution_threshold * 0.3:
        logger.debug([resolution, resolution_threshold * 0.4])
        quality = 90
    elif resolution > resolution_threshold * 0.15:
        quality = 95

    return image, quality, width, height


def convert_source_image_to_webp(source_image_b64):
    """Convert img2img sources to 90% compressed webp, to avoid wasting bandwidth, while still supporting all types"""
    try:
        if source_image_b64 is None:
            return source_image_b64
        image, quality, width, height = convert_source_image_to_pil(source_image_b64)
        buffer = BytesIO()
        image.save(buffer, format="WebP", quality=quality, exact=True)
        final_image_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
        logger.debug(
            f"Received img2img source of {width}*{height}. "
            f"Started {round(len(source_image_b64) / 1000)} base64 kilochars. "
            f"Ended with quality {quality} = {round(len(final_image_b64) / 1000)} base64 kilochars",
        )
        return final_image_b64
    except ImageValidationFailed as err:
        raise err
    except Exception:
        raise ImageValidationFailed


def upload_source_image_to_r2(source_image_b64, uuid_string):
    """Convert source images to webp and uploads it to r2,
    to avoid wasting bandwidth, while still supporting all types
    """
    try:
        if source_image_b64 is None:
            return (None, None)
        image, quality, width, height = convert_source_image_to_pil(source_image_b64)
        filename = f"{uuid_string}.webp"
        download_url = upload_source_image(image, filename)
        return (download_url, image)
    except ImageValidationFailed as err:
        raise err
    except Exception:
        raise ImageValidationFailed


def ensure_source_image_uploaded(source_image_string, uuid_string, force_r2=False):
    with logfire.span("horde.image.ensure_uploaded", is_url=source_image_string.startswith("http"), force_r2=force_r2):
        return _ensure_source_image_uploaded(source_image_string, uuid_string, force_r2)


def _ensure_source_image_uploaded(source_image_string, uuid_string, force_r2=False):
    if source_image_string.startswith("http"):
        # A source image URL is fetched on the client's behalf, so it goes through the same address guard as a
        # chat image URL: a URL that resolves or redirects to a private address is refused.
        try:
            img_data = fetch_public_image(source_image_string)
            try:
                img = Image.open(BytesIO(img_data))
            except UnidentifiedImageError:
                raise ImageValidationFailed("Url does not contain a valid image.", rc="SourceImageUrlInvalid")
            except Exception:
                raise ImageValidationFailed("Something went wrong when opening image.", rc="SourceImageUnreadable")
            if force_r2:
                logger.debug(f"uploading {img} {uuid_string}")
                # The same key a base64 source image is stored under, which the R2 download and delete helpers read.
                download_url = upload_source_image(img, f"{uuid_string}.webp")
                return (download_url, img, True)
        except Exception as err:
            if type(err) is ImageValidationFailed:
                raise err
            raise ImageValidationFailed("Something went wrong when retrieving image url.")
        return (source_image_string, img, False)
    download_url, img = upload_source_image_to_r2(source_image_string, uuid_string)
    return (download_url, img, True)


def parse_image_data_url(image_url: str) -> str | None:
    """Parse the base64 payload out of an image data URL.

    Args:
        image_url: An image part's ``url``.

    Returns:
        The base64 payload, or None when the URL is not a base64 image data URL.
    """
    data_url_match = IMAGE_DATA_URL_PATTERN.fullmatch(image_url)
    if data_url_match is None:
        return None
    return data_url_match.group("base64_payload")


def normalize_chat_image(image: Image.Image) -> Image.Image:
    """Create the copy of a chat image that is stored and sent to workers.

    The copy is RGB, or RGBA when the image has transparency, and its longest side is at most
    [`MAX_CHAT_IMAGE_SIDE`][horde.image.MAX_CHAT_IMAGE_SIDE]; a smaller image keeps its size.

    Args:
        image: The decoded image.

    Returns:
        The normalised copy.

    Raises:
        OSError: If the image data cannot be decoded.
    """
    normalized_image = image.convert("RGBA" if image.has_transparency_data else "RGB")
    normalized_image.thumbnail((MAX_CHAT_IMAGE_SIDE, MAX_CHAT_IMAGE_SIDE), Image.Resampling.LANCZOS)
    return normalized_image


def fetch_public_image(
    image_url: str,
    *,
    max_bytes: int = MAX_FETCHED_IMAGE_BYTES,
    resolve: HostResolver = resolve_host_addresses,
    address_allowed: Callable[[str], bool] = is_public_address,
) -> bytes:
    """Fetch the bytes at a client's http(s) URL without letting the URL reach a private address.

    The request goes through [`send_public_request`][horde.outbound.send_public_request], which checks every hop's
    resolved addresses and connects to the checked address, following at most
    [`MAX_IMAGE_FETCH_REDIRECTS`][horde.image.MAX_IMAGE_FETCH_REDIRECTS] redirects.

    Args:
        image_url: The URL a client sent.
        max_bytes: The most bytes the body may have.
        resolve: Resolves a host and port to addresses.
        address_allowed: Whether a resolved address may be connected to.

    Returns:
        The response body.

    Raises:
        horde.exceptions.ImageValidationFailed: With ``SourceImageUrlRefused`` when a hop's scheme is not http(s),
            its host does not resolve or resolves to an address that is not allowed, or there are too many
            redirects; ``SourceImageSizeExceeded`` when the body passes ``max_bytes``; ``SourceImageUrlInvalid``
            when the URL returns a status other than 200 or a redirect without a location; or the plain code
            when the connection fails.
    """
    try:
        response = send_public_request(
            image_url,
            hop_timeout_seconds=IMAGE_FETCH_TIMEOUT_SECONDS,
            deadline_seconds=IMAGE_FETCH_DEADLINE_SECONDS,
            max_redirects=MAX_IMAGE_FETCH_REDIRECTS,
            max_response_bytes=max_bytes,
            resolve=resolve,
            address_allowed=address_allowed,
        )
    except OutboundRequestRefused as err:
        raise ImageValidationFailed(str(err), rc="SourceImageUrlRefused") from err
    except OutboundResponseTooLarge as err:
        raise ImageValidationFailed("Provided image cannot be larger than 5Mb", rc="SourceImageSizeExceeded") from err
    except OutboundRequestFailed as err:
        raise ImageValidationFailed("Something went wrong when retrieving image url.") from err
    if response.status in HTTP_REDIRECT_STATUSES:
        raise ImageValidationFailed("The image URL redirected without a location.", rc="SourceImageUrlInvalid")
    if response.status != 200:
        raise ImageValidationFailed(f"The image URL returned status {response.status}.", rc="SourceImageUrlInvalid")
    return response.body


def encode_chat_image(image: Image.Image) -> bytes:
    """Return a normalised chat image encoded as the JPEG that is stored and sent to workers.

    An image with transparency is flattened onto [`CHAT_IMAGE_BACKGROUND`][horde.image.CHAT_IMAGE_BACKGROUND] first.

    Args:
        image: The image [`load_chat_image`][horde.image.load_chat_image] returned.

    Returns:
        The JPEG bytes, at [`CHAT_IMAGE_JPEG_QUALITY`][horde.image.CHAT_IMAGE_JPEG_QUALITY].
    """
    if image.has_transparency_data:
        transparent_image = image.convert("RGBA")
        opaque_image = Image.new("RGB", transparent_image.size, CHAT_IMAGE_BACKGROUND)
        opaque_image.paste(transparent_image, mask=transparent_image.getchannel("A"))
    else:
        opaque_image = image.convert("RGB")
    image_buffer = BytesIO()
    opaque_image.save(image_buffer, format="JPEG", quality=CHAT_IMAGE_JPEG_QUALITY)
    return image_buffer.getvalue()


def load_chat_image(image_url: str, *, resolve: HostResolver = resolve_host_addresses) -> Image.Image:
    """Create the normalised image a chat image part points at, decoding a data URL or fetching an http(s) URL.

    A URL is fetched with [`fetch_public_image`][horde.image.fetch_public_image], with the 5 MiB cap a source image
    URL has. Both forms are held to the source image resolution cap, checked on the decoded header before any
    pixel data is decoded.

    Args:
        image_url: An image part's ``url``, already checked for form and encoded size.
        resolve: Resolves a URL's host, passed to the fetch.

    Returns:
        The image as [`normalize_chat_image`][horde.image.normalize_chat_image] returns it.

    Raises:
        horde.exceptions.ImageValidationFailed: If the image cannot be fetched or read, or breaks a size cap.
    """
    with logfire.span("horde.image.load_chat_image", is_url=image_url.startswith("http")):
        base64_payload = parse_image_data_url(image_url)
        try:
            if base64_payload is None:
                image = Image.open(BytesIO(fetch_public_image(image_url, resolve=resolve)))
            else:
                image, _, _, _ = convert_source_image_to_pil(base64_payload)
            width, height = image.size
            if width * height > MAX_SOURCE_IMAGE_PIXELS:
                raise ImageValidationFailed("Image size cannot exceed 3072*3072 pixels", rc="SourceImageResolutionExceeded")
            return normalize_chat_image(image)
        except Image.DecompressionBombError as err:
            raise ImageValidationFailed("Image size cannot exceed 3072*3072 pixels", rc="SourceImageResolutionExceeded") from err
        except (binascii.Error, OSError) as err:
            raise ImageValidationFailed("The image could not be read.", rc="SourceImageUnreadable") from err


def calculate_image_tiles(image):
    """Returns the amount of 512x512 tiles the image
    is composed of
    image is a PIL object
    """
    width, height = image.size
    tiles_x = (width + 511) // 512
    tiles_y = (height + 511) // 512
    return tiles_x * tiles_y
