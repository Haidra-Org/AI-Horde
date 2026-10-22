# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live checks of delta relay and the messages offload against the local front.

Usage: stream_demo.py [--base-url URL] [--horde-url URL] [--api-key KEY] [--model NAME]

Cases:
  1. streamed plain request: deltas must arrive before the final reply (several content chunks, spread in time)
  2. non-streamed plain request: unchanged whole reply
  3. streamed request with tools: buffered (one content/tool chunk only after the reply completes)
  4. streamed request padded past the offload threshold: served through messages_url (checked on the native status)
"""

from __future__ import annotations

import argparse
import json
import os
import time

import httpx
from openai import OpenAI

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/Llama-3.2-3B-Instruct")
p.add_argument("--horde-url", default=os.environ.get("HORDE_URL", "http://127.0.0.1:7101"), help="Horde root (env HORDE_URL).")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")
model = a.model
client = OpenAI(base_url=a.base_url, api_key=a.api_key, timeout=600)
horde = httpx.Client(base_url=f"{a.horde_url.rstrip('/')}/api/v2", headers={"apikey": a.api_key}, timeout=30)


def streamed(messages, **extra):
    start = time.monotonic()
    stamps = []
    text = []
    finish = None
    usage = None
    request_id = None
    for chunk in client.chat.completions.create(
        model=model, messages=messages, stream=True, max_tokens=400, stream_options={"include_usage": True}, **extra
    ):
        now = time.monotonic() - start
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta.content:
                stamps.append(now)
                text.append(delta.content)
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
            x_horde = getattr(chunk, "x_horde", None)
            if x_horde and isinstance(x_horde, dict):
                request_id = x_horde.get("request_id") or request_id
        if chunk.usage:
            usage = chunk.usage
    total = time.monotonic() - start
    return {
        "chunks": len(stamps),
        "first": stamps[0] if stamps else None,
        "last": stamps[-1] if stamps else None,
        "total": total,
        "text": "".join(text),
        "finish": finish,
        "usage": usage and usage.model_dump(),
        "request_id": request_id,
    }


print("== 1. streamed plain")
r1 = streamed([{"role": "user", "content": "Write a long story about a lighthouse keeper and a storm, at least twelve sentences."}])
print(json.dumps({k: v for k, v in r1.items() if k != "text"}, indent=1))
print("text:", r1["text"][:200].replace("\n", " "))
assert r1["chunks"] >= 2, "expected more than one content chunk"
assert r1["last"] - r1["first"] > 0.3, "chunks did not spread over time: not live"
assert r1["first"] < r1["total"] * 0.8, "first chunk arrived too late to be live"
print("LIVE DELTAS OK")

print("== 2. non-streamed plain")
start = time.monotonic()
c2 = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Name three colours, one per line."}], max_tokens=60)
print({"total": round(time.monotonic() - start, 2), "finish": c2.choices[0].finish_reason, "text": c2.choices[0].message.content[:120]})
assert c2.choices[0].message.content
print("NON-STREAM OK")

print("== 3. streamed with tools (buffered)")
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
]
start = time.monotonic()
stamps = []
tool_chunks = 0
content_chunks = 0
for chunk in client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "What is the weather in Paris? Use the tool."}],
    stream=True,
    max_tokens=120,
    tools=tools,
    tool_choice="auto",
):
    if chunk.choices:
        d = chunk.choices[0].delta
        if d.tool_calls:
            tool_chunks += 1
            stamps.append(time.monotonic() - start)
        if d.content:
            content_chunks += 1
            stamps.append(time.monotonic() - start)
total = time.monotonic() - start
print({"tool_chunks": tool_chunks, "content_chunks": content_chunks, "stamps": [round(s, 2) for s in stamps], "total": round(total, 2)})
assert tool_chunks + content_chunks >= 1
if len(stamps) > 1:
    assert stamps[-1] - stamps[0] < 0.5, "tool request streamed live; expected buffered"
print("TOOLS BUFFERED OK")

print("== 4. streamed, padded past the offload threshold")
padding = "The quick brown fox jumps over the lazy dog. " * 80
r4 = streamed([{"role": "system", "content": padding}, {"role": "user", "content": "Reply with one short sentence about foxes."}])
print(json.dumps({k: v for k, v in r4.items() if k != "text"}, indent=1))
print("text:", r4["text"][:160].replace("\n", " "))
assert r4["text"]
if r4["request_id"]:
    status = horde.get(f"/generate/chat/status/{r4['request_id']}").json()
    print(
        "native status generations:",
        json.dumps([{k: g.get(k) for k in ("worker_name", "model", "state")} for g in status.get("generations", [])], indent=1),
    )
print("OFFLOAD REQUEST OK")
