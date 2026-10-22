# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify documentation claims against the live local stack: image tax, horde.only, max_tokens above the worker limit.

The max_tokens check expects the model's only worker to declare ``--max-length`` as its output limit (koboldcpp's
``--hordegenlen``): one token above it is refused as ineligible and the limit itself is served.
"""

import argparse
import base64
import io
import json
import os

from openai import OpenAI
from PIL import Image

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/Meta-Llama-3.1-8B-Instruct")
p.add_argument("--max-length", type=int, default=1024, help="Output limit the model's only worker declares (default 1024).")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")
MODEL = a.model
c = OpenAI(base_url=a.base_url, api_key=a.api_key, max_retries=0, timeout=300)

card = {m.id: m for m in c.models.list().data}[MODEL]
print("card extras:", {k: v for k, v in card.model_extra.items()})
limits = card.model_extra.get("horde") or card.model_extra
print("max_completion_tokens on card:", json.dumps(limits)[:300])

r = c.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hi."}], max_tokens=16)
x = r.model_extra["x_horde"]
print("plain x_horde:", x)
worker_id = x.get("worker_id")

# horde.only pins the worker; a bogus id makes the request ineligible
r2 = c.chat.completions.create(
    model=MODEL, messages=[{"role": "user", "content": "Say hi."}], max_tokens=16, extra_body={"horde": {"only": [worker_id]}}
)
print("only=real worker ->", r2.model_extra["x_horde"].get("worker_id") == worker_id)
try:
    c.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Say hi."}],
        max_tokens=16,
        extra_body={"horde": {"only": ["00000000-0000-0000-0000-000000000000"]}},
    )
    print("only=bogus -> served (unexpected)")
except Exception as e:
    print("only=bogus ->", type(e).__name__, str(e)[:140])

# max_tokens above the only worker's max_length is ineligible everywhere
try:
    c.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hi."}], max_tokens=a.max_length + 1)
    print(f"max_tokens {a.max_length + 1} -> served (unexpected)")
except Exception as e:
    print(f"max_tokens {a.max_length + 1} ->", type(e).__name__, str(e)[:160])
r3 = c.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": "Say hi."}], max_tokens=a.max_length)
print(f"max_tokens {a.max_length} ->", r3.choices[0].finish_reason)

# image tax: kudos with and without an image (fixed max_tokens, same text)
buf = io.BytesIO()
Image.new("RGB", (64, 64), (0, 0, 255)).save(buf, format="PNG")
data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
msgs = [
    {"role": "user", "content": [{"type": "text", "text": "Reply with one word."}, {"type": "image_url", "image_url": {"url": data_url}}]}
]
try:
    r4 = c.chat.completions.create(model=MODEL, messages=msgs, max_tokens=16)
    print("image request kudos:", r4.model_extra["x_horde"].get("kudos"), "vs plain:", x.get("kudos"))
except Exception as e:
    print("image request ->", type(e).__name__, str(e)[:160], "(this worker declares no vision)")
