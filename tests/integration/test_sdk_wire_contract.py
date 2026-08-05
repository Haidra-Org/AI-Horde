# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cross-repo wire-contract proof: live HTTP server parsed by the local horde_sdk.

The unit/integration suites exercise the server in isolation. This module proves
the wire contract between the AI-Horde server tree and the horde_sdk tree: the
server's real HTTP pop responses parse into the local horde_sdk response models
with the new annotation/extended-controlnet fields intact, and the SDK's request
models re-serialize those same fields. The SDK runs in its own venv as a
subprocess so the two trees never share an interpreter.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from werkzeug.serving import make_server

from horde.bridge_reference import CAPABILITY_EXPANDED_REGEN_VERSION

# The SDK-side interpreter is host-specific: point HORDE_SDK_WIRE_PYTHON at a python whose
# environment has the horde_sdk under test installed; the suite skips when it is not set.
SDK_PYTHON = Path(os.environ["HORDE_SDK_WIRE_PYTHON"]) if os.environ.get("HORDE_SDK_WIRE_PYTHON") else None
SDK_CLIENT_SCRIPT = Path(__file__).resolve().parent / "_sdk_wire_client.py"

TEST_MODELS = ["stable_diffusion"]
NEW_BRIDGE_AGENT = f"AI Horde Worker reGen:{CAPABILITY_EXPANDED_REGEN_VERSION}:https://github.com/Haidra-Org/horde-worker-reGen"

pytestmark = [
    pytest.mark.object_storage,
    pytest.mark.usefixtures("object_store_ready"),
    pytest.mark.skipif(
        SDK_PYTHON is None or not SDK_PYTHON.exists(),
        reason="HORDE_SDK_WIRE_PYTHON not set or does not exist",
    ),
]


def _load_image_as_b64(image_path: str) -> str:
    final_src_img = Image.open(image_path)
    buffer = BytesIO()
    final_src_img.save(buffer, format="Webp", quality=50, exact=True)
    return base64.b64encode(buffer.getvalue()).decode("utf8")


@pytest.fixture
def live_server(app) -> Iterator[str]:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_sdk_parses_live_pop_responses(
    client,
    live_server: str,
    api_key: str,
    request_headers: dict[str, str],
) -> None:
    annotation_async = {
        "forms": [{"name": "annotation", "payload": {"control_type": "canny"}}],
        "source_image": "https://github.com/Haidra-Org/AI-Horde/blob/main/icon.png?raw=true",
    }
    annotation_req = client.post("/api/v2/interrogate/async", json=annotation_async, headers=request_headers)
    assert annotation_req.status_code < 400, annotation_req.get_data(as_text=True)
    annotation_id = annotation_req.get_json()["id"]

    image_async = {
        "prompt": "a controlnet-guided horde of robots",
        "nsfw": True,
        "censor_nsfw": False,
        "r2": True,
        "shared": True,
        "trusted_workers": True,
        "params": {
            "width": 512,
            "height": 512,
            "steps": 20,
            "cfg_scale": 7.5,
            "sampler_name": "k_euler_a",
            "control_type": "lineart",
        },
        "models": TEST_MODELS,
        "source_image": _load_image_as_b64("img_stable/0.jpg"),
        "source_processing": "img2img",
    }
    image_req = client.post("/api/v2/generate/async", json=image_async, headers=request_headers)
    assert image_req.status_code < 400, image_req.get_data(as_text=True)
    image_id = image_req.get_json()["id"]

    cfg = {
        "base_url": live_server,
        "apikey": api_key,
        "client_agent": request_headers["Client-Agent"],
        "alchemy_pop_body": {
            "name": "CICD Fake Alchemist",
            "forms": ["annotation"],
            "annotation_types": ["canny"],
            "bridge_agent": request_headers["Client-Agent"],
            "max_tiles": 96,
        },
        "image_pop_body": {
            "name": "CICD Fake Dreamer",
            "models": TEST_MODELS,
            "bridge_agent": NEW_BRIDGE_AGENT,
            "nsfw": True,
            "amount": 10,
            "max_pixels": 4194304,
            "allow_img2img": True,
            "allow_painting": True,
            "allow_unsafe_ipaddr": True,
            "allow_post_processing": True,
            "allow_controlnet": True,
            "allow_extended_controlnet": True,
            "allow_sdxl_controlnet": True,
            "allow_lora": True,
        },
    }

    try:
        completed = subprocess.run(
            [str(SDK_PYTHON), str(SDK_CLIENT_SCRIPT)],
            input=json.dumps(cfg),
            capture_output=True,
            text=True,
            cwd=str(SDK_CLIENT_SCRIPT.parent),
            timeout=120,
        )
        assert completed.returncode == 0, (
            f"SDK wire client failed (rc={completed.returncode}).\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
        verdict = json.loads(completed.stdout.strip().splitlines()[-1])
        assert verdict["ok"] is True, verdict
        assert verdict["alchemy"]["control_type"] == "canny", verdict
        assert verdict["alchemy"]["has_r2_upload"] is True, verdict
        assert verdict["alchemy"]["request_roundtrip_annotation_types"] == ["canny"], verdict
        assert verdict["image"]["control_type"] == "lineart", verdict
        assert verdict["image"]["request_roundtrip_allow_extended_controlnet"] is True, verdict
    finally:
        client.delete(f"/api/v2/interrogate/status/{annotation_id}", headers=request_headers)
        client.delete(f"/api/v2/generate/status/{image_id}", headers=request_headers)
