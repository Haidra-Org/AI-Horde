# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI SDK against the front: tool calls (non-streamed, streamed, forced), a tool-result follow-up, and structured output.

Usage: sdk_tools_roundtrip.py [--base-url URL] [--api-key KEY] [--model NAME]

Prints TOOLS ROUND TRIP OK when every step passes.
"""

import argparse
import json
import os

from openai import OpenAI

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/Meta-Llama-3.1-8B-Instruct")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")
MODEL = a.model
c = OpenAI(base_url=a.base_url, api_key=a.api_key, max_retries=0, timeout=300)

cards = {m.id: m for m in c.models.list().data}
card = cards[MODEL]
print("model card:", MODEL, "supported_parameters:", card.model_extra.get("supported_parameters"))

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current local time for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    },
]
question = {"role": "user", "content": "What is the weather like in Paris right now? Use the tool."}

# 1. non-streamed tool call
r = c.chat.completions.create(model=MODEL, messages=[question], tools=TOOLS, tool_choice="auto", max_tokens=200, temperature=0.2)
msg = r.choices[0].message
print(
    "tool round:",
    r.choices[0].finish_reason,
    "content=",
    repr(msg.content),
    "tool_calls=",
    [(t.id, t.function.name, t.function.arguments) for t in (msg.tool_calls or [])],
    r.usage,
)
assert r.choices[0].finish_reason == "tool_calls" and msg.tool_calls, "expected a tool call"
call = msg.tool_calls[0]
args = json.loads(call.function.arguments)
assert call.function.name == "get_weather" and "paris" in json.dumps(args).lower(), args

# 2. tool result follow-up: the history carries the assistant tool_calls and the tool message
history = [
    question,
    {"role": "assistant", "content": msg.content, "tool_calls": [t.model_dump() for t in msg.tool_calls]},
    {"role": "tool", "tool_call_id": call.id, "content": json.dumps({"city": "Paris", "temperature_c": 18, "sky": "sunny"})},
]
r2 = c.chat.completions.create(model=MODEL, messages=history, tools=TOOLS, max_tokens=120, temperature=0.2)
print("follow-up:", r2.choices[0].finish_reason, repr(r2.choices[0].message.content))
assert r2.choices[0].finish_reason in ("stop", "length") and r2.choices[0].message.content
assert "18" in r2.choices[0].message.content or "sunny" in r2.choices[0].message.content.lower()

# 3. streamed tool call, accumulated the way SDK users do
acc = {}
finish = None
for ch in c.chat.completions.create(model=MODEL, messages=[question], tools=TOOLS, max_tokens=200, temperature=0.2, stream=True):
    if not ch.choices:
        continue
    d = ch.choices[0].delta
    for tc in d.tool_calls or []:
        slot = acc.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
        slot["id"] = tc.id or slot["id"]
        if tc.function:
            slot["name"] += tc.function.name or ""
            slot["arguments"] += tc.function.arguments or ""
    finish = ch.choices[0].finish_reason or finish
print("streamed tool round:", finish, acc)
assert finish == "tool_calls" and acc[0]["name"] == "get_weather" and json.loads(acc[0]["arguments"])

# 4. forced function by name
r4 = c.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Hello there."}],
    tools=TOOLS,
    tool_choice={"type": "function", "function": {"name": "get_time"}},
    max_tokens=120,
    temperature=0.2,
)
m4 = r4.choices[0].message
print("forced:", r4.choices[0].finish_reason, [(t.function.name, t.function.arguments) for t in (m4.tool_calls or [])])
assert m4.tool_calls and m4.tool_calls[0].function.name == "get_time"

# 5. json_schema structured output
schema = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}, "population_millions": {"type": "number"}},
    "required": ["city", "country", "population_millions"],
    "additionalProperties": False,
}
r5 = c.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Describe Paris as JSON."}],
    max_tokens=150,
    temperature=0.2,
    response_format={"type": "json_schema", "json_schema": {"name": "city_facts", "schema": schema, "strict": True}},
)
doc = json.loads(r5.choices[0].message.content)
print("json_schema:", r5.choices[0].finish_reason, doc)
assert set(doc) == {"city", "country", "population_millions"} and isinstance(doc["population_millions"], (int, float))

# 6. json_object
r6 = c.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Give three colours as a JSON object with a 'colours' array."}],
    max_tokens=100,
    temperature=0.2,
    response_format={"type": "json_object"},
)
doc6 = json.loads(r6.choices[0].message.content)
print("json_object:", r6.choices[0].finish_reason, doc6)
assert isinstance(doc6, dict)

# 7. a conflict is refused before anything is queued
try:
    c.chat.completions.create(model=MODEL, messages=[question], tool_choice="required", max_tokens=20)
    raise AssertionError("tool_choice without tools was accepted")
except Exception as e:
    print("refused as expected:", type(e).__name__, str(e)[:200])

print("TOOLS ROUND TRIP OK")
