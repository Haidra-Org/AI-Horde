# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Requests shaped exactly as KoboldAI Lite's OpenAI mode builds them (read from lite.koboldai.net index.html).

Lite sends `Authorization: Bearer <key>`, `max_tokens`, `model`, `temperature`, `top_p`, up to four `stop` strings,
`presence_penalty` from its rep_pen slider when above zero, `stream: true`, and, with an MCP server connected, `tools`
mapped from the MCP tool list (`type: function`, `name`, `description`, `parameters: inputSchema`). A tool-call turn is
replayed as `{"role": "assistant", "content": null, "tool_calls": [...]}` and the result as the raw tool message
`{"role": "tool", "tool_call_id": ..., "content": ...}`. The browser also sends a CORS preflight.

Usage: lite_shaped_roundtrip.py [--base-url URL] [--api-key KEY] [--model NAME] [--origin URL]

Prints LITE-SHAPED ROUND TRIP OK when every step passes.
"""

import argparse
import json
import os

import requests

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/Meta-Llama-3.1-8B-Instruct")
p.add_argument("--origin", default="https://lite.koboldai.net", help="Origin header the browser would send.")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")
MODEL = a.model
FRONT = a.base_url.rstrip("/")
ORIGIN = a.origin
headers = {"Content-Type": "application/json", "Authorization": "Bearer " + a.api_key, "Origin": ORIGIN}

# 1. the browser's preflight for the chat endpoint and the models endpoint
for path, method, req_headers in (("/chat/completions", "POST", "authorization,content-type"), ("/models", "GET", "authorization")):
    pre = requests.options(
        FRONT + path,
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": req_headers},
        timeout=10,
    )
    acao = pre.headers.get("access-control-allow-origin")
    allow_headers = pre.headers.get("access-control-allow-headers")
    print(f"preflight {method} {path}: {pre.status_code} allow-origin={acao!r} allow-headers={allow_headers!r}")
    assert pre.status_code in (200, 204) and acao in ("*", ORIGIN), "preflight refused: a browser client cannot reach the front"

MCP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
]
base = {
    "max_tokens": 200,
    "model": MODEL,
    "temperature": 0.7,
    "top_p": 0.92,
    "stop": ["\nUser:", "\n### Instruction:"],
    "presence_penalty": 0.1,
    "stream": True,
}


def stream(payload):
    """Return (content, tool_calls as Lite accumulates them, finish_reason, cors header) for one streamed request."""
    r = requests.post(FRONT + "/chat/completions", json=payload, headers=headers, stream=True, timeout=300)
    assert r.status_code == 200, r.text[:300]
    content, calls, finish = "", {}, None
    for line in r.iter_lines():
        if not line.startswith(b"data: ") or line == b"data: [DONE]":
            continue
        ev = json.loads(line[6:])
        if not ev.get("choices"):
            continue
        ch = ev["choices"][0]
        delta = ch.get("delta", {})
        if delta.get("content"):
            content += delta["content"]
        # Lite's parser: tool call deltas are read only from chunks without content
        elif delta.get("tool_calls"):
            for itm in delta["tool_calls"]:
                slot = calls.setdefault(itm.get("id") or len(calls), {"id": itm.get("id"), "name": "", "arguments": ""})
                if itm.get("function"):
                    slot["name"] = slot["name"] or itm["function"].get("name") or ""
                    slot["arguments"] += itm["function"].get("arguments") or ""
        finish = ch.get("finish_reason") or finish
    return content, list(calls.values()), finish, r.headers.get("access-control-allow-origin")


# 2. a plain chat turn as Lite's chat mode sends it
plain = dict(
    base, messages=[{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "Say hello in five words."}]
)
content, calls, finish, acao = stream(plain)
print("plain:", repr(content), finish, "allow-origin on response:", acao)
assert content and not calls and acao in ("*", ORIGIN)

# 3. an MCP tool call
tool_turn = dict(base, messages=[{"role": "user", "content": "What's the weather in Paris? Use the tool."}], tools=MCP_TOOLS)
content, calls, finish, _ = stream(tool_turn)
print("tool turn:", repr(content), calls, finish)
assert finish == "tool_calls" and calls and calls[0]["name"] == "get_weather", "Lite's stream parser would see no tool call"

# 4. Lite replays the call and the raw tool message, then expects prose
follow = dict(
    base,
    tools=MCP_TOOLS,
    messages=[
        {"role": "user", "content": "What's the weather in Paris? Use the tool."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": calls[0]["id"], "type": "function", "function": {"name": calls[0]["name"], "arguments": calls[0]["arguments"]}}
            ],
        },
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": json.dumps({"temperature_c": 18, "sky": "sunny"})},
    ],
)
content, calls2, finish, _ = stream(follow)
print("follow-up:", repr(content), calls2, finish)
assert content and not calls2

# 5. Lite's opt-in "send non-standard params" would add kobold sampler keys; the front refuses them by name
odd = dict(plain, rep_pen=1.1, top_k=40, min_p=0.05)
r = requests.post(FRONT + "/chat/completions", json=odd, headers=headers, timeout=30)
print("non-standard params:", r.status_code, r.json().get("error", {}).get("param"), r.json().get("error", {}).get("code"))
assert r.status_code == 400

print("LITE-SHAPED ROUND TRIP OK")
