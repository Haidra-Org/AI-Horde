# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Helpers for exercising worker-facing presigned object-storage URLs."""

from io import BytesIO

import requests
from PIL import Image


def make_test_webp(*, size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    """Return a small deterministic image suitable for object-transfer tests."""
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def assert_presigned_image_download(url: str, expected: bytes) -> None:
    """Download a presigned object and verify both its bytes and image payload."""
    response = requests.get(url, timeout=10)
    assert response.status_code == 200, response.text
    assert response.content == expected

    with Image.open(BytesIO(response.content)) as image:
        image.verify()


def upload_to_presigned_url(url: str, payload: bytes) -> None:
    """Upload bytes using only the URL a real worker receives."""
    response = requests.put(url, data=payload, timeout=10)
    assert response.status_code < 400, response.text
