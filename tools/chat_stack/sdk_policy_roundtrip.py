# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OpenAI SDK against the front: the chat conformance policy (ADR 0036) cases a-g, plus optional jinja-mode checks.

Prints one RESULT line per case and does not stop on a failed expectation; read every line.

Usage: sdk_policy_roundtrip.py [--base-url URL] [--api-key KEY] [--model NAME] [--only a,b,...]
Cases: a named tool_choice, b tool_choice none, c parallel_tool_calls false, d json_schema minimum,
e temperature 1.5 strict, f temperature 1.5 best_effort, g temperature 1.0 strict,
j named tool_choice strict (expected fail fast under a worker without tool_choice_enforced),
k named tool_choice best_effort (expected served). Cases j and k run only when --only is given.
"""

import argparse
import json
import os
import time

import openai
from openai import OpenAI

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key (env HORDE_API_KEY).")
p.add_argument("--model", default="koboldcpp/Meta-Llama-3.1-8B-Instruct")
p.add_argument("--only", help="Comma-separated case letters to run, such as a,b,j.")
a = p.parse_args()
if not a.api_key:
    p.error("an API key is required: pass --api-key or set HORDE_API_KEY")
only = set(a.only.split(",")) if a.only else None
MODEL = a.model
c = OpenAI(base_url=a.base_url, api_key=a.api_key, max_retries=0, timeout=300)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
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
WEATHER_Q = {"role": "user", "content": "What is the weather like in Paris right now?"}
THREE_Q = {"role": "user", "content": "Get the weather in Paris, in Tokyo and in Lima. Call the tool once for each city, all at once."}
BEST_EFFORT = {"horde": {"conformance": "best_effort"}}


def calls(r):
    m = r.choices[0].message
    return [(t.function.name, t.function.arguments) for t in (m.tool_calls or [])]


def run(case, expect, fn):
    if only and case not in only:
        return
    t0 = time.time()
    try:
        r = fn()
        dt = time.time() - t0
        m = r.choices[0].message
        print(f"RESULT {case} served in {dt:.1f}s finish={r.choices[0].finish_reason} content={m.content!r} tool_calls={calls(r)}")
        return r
    except openai.APIStatusError as e:
        dt = time.time() - t0
        print(f"RESULT {case} refused in {dt:.1f}s status={e.status_code} body={json.dumps(e.body)}")
    except Exception as e:  # noqa: BLE001
        dt = time.time() - t0
        print(f"RESULT {case} error in {dt:.1f}s {type(e).__name__}: {e}")
    finally:
        print(f"EXPECT {case} {expect}")


run(
    "a",
    "a call to get_time",
    lambda: c.chat.completions.create(
        model=MODEL,
        messages=[WEATHER_Q],
        tools=TOOLS,
        max_tokens=150,
        temperature=0.2,
        tool_choice={"type": "function", "function": {"name": "get_time"}},
    ),
)

run(
    "b",
    "prose, no tool call",
    lambda: c.chat.completions.create(model=MODEL, messages=[WEATHER_Q], tools=TOOLS, tool_choice="none", max_tokens=150, temperature=0.2),
)

run(
    "c",
    "at most one tool call",
    lambda: c.chat.completions.create(
        model=MODEL, messages=[THREE_Q], tools=TOOLS, tool_choice="auto", parallel_tool_calls=False, max_tokens=300, temperature=0.2
    ),
)

schema = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "score": {"type": "number", "minimum": 2}},
    "required": ["name", "score"],
    "additionalProperties": False,
}
r = run(
    "d",
    "valid JSON; record whether score >= 2",
    lambda: c.chat.completions.create(
        model=MODEL,
        max_tokens=120,
        temperature=0.2,
        messages=[
            {
                "role": "user",
                "content": "Rate the film 'Cats' (2019) as JSON with a name and a score from 0 to 10. It was terrible, give it 0.",
            }
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "rating", "schema": schema, "strict": True}},
    ),
)
if r is not None:
    try:
        doc = json.loads(r.choices[0].message.content)
        print(
            f"RESULT d-parse valid_json=True doc={doc} keys_ok={set(doc) == {'name', 'score'}} "
            f"minimum_honoured={isinstance(doc.get('score'), (int, float)) and doc['score'] >= 2}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"RESULT d-parse valid_json=False {type(e).__name__}: {e}")

run(
    "e",
    "refused fast (tools_max_temperature 1.0)",
    lambda: c.chat.completions.create(model=MODEL, messages=[WEATHER_Q], tools=TOOLS, max_tokens=150, temperature=1.5),
)

run(
    "f",
    "refused fast as well (limits apply under best_effort)",
    lambda: c.chat.completions.create(
        model=MODEL, messages=[WEATHER_Q], tools=TOOLS, max_tokens=150, temperature=1.5, extra_body=BEST_EFFORT
    ),
)

run("g", "served", lambda: c.chat.completions.create(model=MODEL, messages=[WEATHER_Q], tools=TOOLS, max_tokens=150, temperature=1.0))

if only:
    run(
        "j",
        "refused fast under a worker without tool_choice_enforced",
        lambda: c.chat.completions.create(
            model=MODEL,
            messages=[WEATHER_Q],
            tools=TOOLS,
            max_tokens=150,
            temperature=0.2,
            tool_choice={"type": "function", "function": {"name": "get_time"}},
        ),
    )
    run(
        "k",
        "served under best_effort",
        lambda: c.chat.completions.create(
            model=MODEL,
            messages=[WEATHER_Q],
            tools=TOOLS,
            max_tokens=150,
            temperature=0.2,
            tool_choice={"type": "function", "function": {"name": "get_time"}},
            extra_body=BEST_EFFORT,
        ),
    )

print("POLICY DRIVER DONE")
