# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Send a generated PNG as a data URL to a vision model through the front, non-streamed and streamed.

Waits up to five minutes for the model to appear in /v1/models first. A correct reply describes a blue rectangle.
"""

import argparse
import base64
import io
import json
import os
import sys
import time

from openai import OpenAI
from PIL import Image, ImageDraw

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/gemma-3-4b-it")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")
c = OpenAI(base_url=a.base_url, api_key=a.api_key, max_retries=0, timeout=240)
model = a.model
for _ in range(60):
    cards = {m.id: m for m in c.models.list().data}
    if model in cards:
        break
    time.sleep(5)
else:
    sys.exit("vision worker never appeared in /v1/models")
card = cards[model].model_extra
print(
    "model card:",
    json.dumps(
        {
            "architecture": card.get("architecture"),
            "supported_parameters": card.get("supported_parameters"),
            "x_horde": card.get("x_horde"),
            "top_provider": card.get("top_provider"),
        }
    ),
)
img = Image.new("RGB", (256, 256), (255, 255, 255))
d = ImageDraw.Draw(img)
d.rectangle((40, 60, 216, 200), fill=(20, 60, 220))
buf = io.BytesIO()
img.save(buf, format="PNG")
data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
t0 = time.time()
r = c.chat.completions.create(
    model=model,
    max_tokens=48,
    temperature=0.2,
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "In one sentence, what shape and colour is in this image?"},
                {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
            ],
        }
    ],
)
print(
    f"non-streamed in {time.time() - t0:.1f}s:",
    repr(r.choices[0].message.content),
    r.choices[0].finish_reason,
    r.usage.model_dump(),
    r.model_extra.get("x_horde"),
)
parts = []
for ch in c.chat.completions.create(
    model=model,
    max_tokens=48,
    stream=True,
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Name the colour of the shape in two words."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ],
):
    if ch.choices and ch.choices[0].delta.content:
        parts.append(ch.choices[0].delta.content)
print("streamed:", repr("".join(parts)))
