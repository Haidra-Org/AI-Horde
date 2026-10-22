# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validation pass for one chat worker's model through an OpenAI-compatible front. Writes raw results to a JSON file.

Runs a plain, a streamed and a vision request, each tool mode, a tool replay, both structured output formats and the
expected refusals (tools above the worker's temperature limit, tools with a response format, an oversized prompt),
printing one line per case. The key defaults to the anonymous key, which the front accepts for small requests.
"""

import argparse
import base64
import io
import json
import os
import time

import httpx
from PIL import Image

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("--model", required=True, help="Model the worker serves, as /v1/models lists it.")
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY", "0000000000"), help="API key (env HORDE_API_KEY, default anonymous).")
p.add_argument("--out", default="validate_chat_worker_results.json", help="File the raw results are written to.")
p.add_argument("--cases", help="Comma-separated case names to run (default: all).")
a = p.parse_args()
FRONT = a.base_url.rstrip("/")
MODEL = a.model
OUT = a.out
H = {"Authorization": f"Bearer {a.api_key}"}

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
TIME = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current local time for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}


def image_url():
    img = Image.new("RGB", (256, 256), "white")
    img.paste((20, 60, 220), (64, 64, 192, 192))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def user(text):
    return [{"role": "user", "content": text}]


CASES = [
    ("plain", {"messages": user("In one sentence, why is the sky blue?"), "max_tokens": 120}),
    (
        "stream",
        {"messages": user("Count from one to five in words."), "max_tokens": 60, "stream": True, "stream_options": {"include_usage": True}},
    ),
    (
        "vision",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "In one sentence, what shape and colour is in this image?"},
                        {"type": "image_url", "image_url": {"url": image_url()}},
                    ],
                }
            ],
            "max_tokens": 60,
        },
    ),
    ("tools_auto", {"messages": user("What is the weather in Paris right now?"), "tools": [WEATHER, TIME], "max_tokens": 200}),
    ("tools_required", {"messages": user("Hello there."), "tools": [WEATHER, TIME], "tool_choice": "required", "max_tokens": 200}),
    (
        "tools_named",
        {
            "messages": user("What is the weather in Paris?"),
            "tools": [WEATHER, TIME],
            "tool_choice": {"type": "function", "function": {"name": "get_time"}},
            "max_tokens": 200,
        },
    ),
    ("tools_none", {"messages": user("What is the weather in Paris?"), "tools": [WEATHER], "tool_choice": "none", "max_tokens": 120}),
    (
        "tools_single",
        {
            "messages": user("What is the weather in Paris, Tokyo and Lima?"),
            "tools": [WEATHER],
            "parallel_tool_calls": False,
            "max_tokens": 200,
        },
    ),
    ("tools_stream", {"messages": user("What is the weather in Oslo?"), "tools": [WEATHER], "stream": True, "max_tokens": 200}),
    (
        "tool_result",
        {
            "messages": [
                {"role": "user", "content": "What is the weather in Paris?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": '{"temp_c": 14, "sky": "light rain"}'},
            ],
            "tools": [WEATHER],
            "max_tokens": 150,
        },
    ),
    (
        "json_schema",
        {
            "messages": user("Give a city and a number of at least 2 for its rating."),
            "max_tokens": 120,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "rating",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["city", "rating"],
                        "properties": {"city": {"type": "string"}, "rating": {"type": "number", "minimum": 2}},
                    },
                },
            },
        },
    ),
    (
        "json_object",
        {
            "messages": user("Reply in json with keys name and age for a fictional person."),
            "max_tokens": 120,
            "response_format": {"type": "json_object"},
        },
    ),
    ("expect_404_temperature", {"messages": user("Weather in Paris?"), "tools": [WEATHER], "temperature": 1.5, "max_tokens": 100}),
    (
        "expect_404_tools_with_format",
        {"messages": user("Weather in Paris as json?"), "tools": [WEATHER], "max_tokens": 100, "response_format": {"type": "json_object"}},
    ),
    (
        "expect_refusal_overflow",
        {"messages": user("Summarise: " + "the quick brown fox jumps over the lazy dog. " * 1200), "max_tokens": 64},
    ),
]


def run(name, body):
    body = {"model": MODEL, **body}
    t0 = time.time()
    rec = {"case": name}
    try:
        if body.get("stream"):
            events = []
            with httpx.stream("POST", f"{FRONT}/chat/completions", json=body, headers=H, timeout=600) as r:
                rec["status"] = r.status_code
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        events.append(data if data == "[DONE]" else json.loads(data))
                    elif line.startswith(":"):
                        events.append({"comment": line})
                if r.status_code != 200:
                    rec["body"] = r.read().decode(errors="replace")
            rec["events"] = events
        else:
            r = httpx.post(f"{FRONT}/chat/completions", json=body, headers=H, timeout=600)
            rec["status"] = r.status_code
            rec["body"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except Exception as ex:  # noqa: BLE001
        rec["exception"] = repr(ex)
    rec["seconds"] = round(time.time() - t0, 1)
    return rec


def summary(rec):
    s = f"{rec['case']:30} {rec.get('status')} {rec['seconds']:6}s "
    if "exception" in rec:
        return s + rec["exception"]
    if "events" in rec:
        kinds = []
        content, calls, finish, usage = "", [], None, None
        for ev in rec["events"]:
            if ev == "[DONE]":
                kinds.append("DONE")
            elif "comment" in ev:
                kinds.append(":")
            elif "error" in ev:
                kinds.append("error")
                content += json.dumps(ev["error"])
            else:
                for ch in ev.get("choices", []):
                    d = ch.get("delta", {})
                    if "role" in d:
                        kinds.append("role")
                    if d.get("content"):
                        kinds.append("content")
                        content += d["content"]
                    if d.get("tool_calls"):
                        kinds.append("tool_calls")
                        calls += [(c["function"]["name"], c["function"]["arguments"]) for c in d["tool_calls"]]
                    if ch.get("finish_reason"):
                        kinds.append("finish")
                        finish = ch["finish_reason"]
                if ev.get("usage"):
                    kinds.append("usage")
                    usage = ev["usage"]
        return s + f"events={','.join(kinds)} finish={finish} calls={calls} usage={usage} content={content[:160]!r}"
    b = rec.get("body")
    if isinstance(b, dict) and b.get("choices"):
        c = b["choices"][0]
        m = c["message"]
        calls = [(x["function"]["name"], x["function"]["arguments"]) for x in (m.get("tool_calls") or [])]
        return s + f"finish={c['finish_reason']} calls={calls} usage={b.get('usage')} content={(m.get('content') or '')[:200]!r}"
    return s + json.dumps(b)[:300]


results = []
only = set(a.cases.split(",")) if a.cases else None
for name, body in CASES:
    if only and name not in only:
        continue
    rec = run(name, body)
    results.append(rec)
    print(summary(rec), flush=True)
json.dump(results, open(OUT, "w", encoding="utf-8"), indent=1)
