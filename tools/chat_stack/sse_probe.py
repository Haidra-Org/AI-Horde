# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Print the arrival time of every server-sent event of one streamed chat request to the front.

Each ``data:`` event prints its wall-clock time, the offset from the request, the content length, the role, the
finish reason and whether it carries usage; other lines (keepalive comments, ``[DONE]``) print their first characters.
The request id from ``x_horde`` prints last, for looking the request up on the native status route.
"""

import argparse
import json
import os
import time

import httpx

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/Llama-3.2-3B-Instruct")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")

body = {
    "model": a.model,
    "stream": True,
    "max_tokens": 400,
    "messages": [{"role": "user", "content": "Write a long story about a lighthouse keeper and a storm, at least twelve sentences."}],
}
t0 = time.time()
rid = None
with (
    httpx.Client(timeout=600) as c,
    c.stream("POST", f"{a.base_url.rstrip('/')}/chat/completions", json=body, headers={"Authorization": f"Bearer {a.api_key}"}) as r,
):
    print("headers at", round(time.time() - t0, 3), r.status_code)
    for line in r.iter_lines():
        if not line.strip():
            continue
        now = time.time()
        if line.startswith("data: {"):
            d = json.loads(line[6:])
            ch = d.get("choices") or [{}]
            delta = ch[0].get("delta", {}) if ch else {}
            xh = d.get("x_horde") or {}
            rid = xh.get("request_id") or rid
            print(
                f"{now:.3f} +{now - t0:6.3f}s content_len={len(delta.get('content') or '')} role={delta.get('role')} "
                f"finish={ch[0].get('finish_reason') if ch else None} usage={'usage' in d and d['usage'] is not None}",
            )
        else:
            print(f"{now:.3f} +{now - t0:6.3f}s {line[:60]}")
print("request_id", rid)
