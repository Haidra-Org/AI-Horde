# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Probe OpenAI chat completions request and response shapes that the Horde front mirrors.

Sends small requests to OpenAI's API (or any base URL given) and prints one line per case: the finish reason, content
and tool calls of a served case, the error message, type, param and code of a refused one, and the per-event shape of
a streamed one. Raw responses are written to the ``--out`` file for the record. The key comes from ``--api-key`` or
``OPENAI_API_KEY`` and is never printed. 429 credit refusals are retried a few times.

Cases are grouped; pass group names to run only those, ``--case`` to run single cases, or ``--list`` to print them.
"""

import argparse
import json
import os
import sys
import time

import httpx

RETRY_ATTEMPTS = 4
"""Tries per case; OpenAI answers 429 for a moment after a key's credit is topped up or checked."""

RETRY_SECONDS = 2
"""Pause between tries of a case refused with 429."""

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
"""A one-argument function tool shared by most cases."""

CLOCK = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Time in a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
"""A second function tool, for naming one tool among several."""


def strict_weather(params: dict, strict: bool = True) -> dict:
    """Return the weather tool with ``strict`` and the given parameter schema."""
    return {"type": "function", "function": {"name": "get_weather", "strict": strict, "parameters": params}}


def build_groups(model: str) -> dict[str, dict[str, dict]]:
    """Return every case body by group and case name."""
    base = {"model": model, "max_tokens": 8}
    user = {"role": "user", "content": "Weather in Paris? One word."}
    call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}],
    }
    result = {"role": "tool", "tool_call_id": "call_1", "content": "18C sunny"}
    give_x = {"role": "user", "content": "Give x=5."}
    use_tool = {"role": "user", "content": "Weather in Paris? Use the tool."}
    say_hi = {"role": "user", "content": "Say hi. One word."}

    def tool_message_case(tool_message: dict) -> dict:
        return {"model": model, "max_tokens": 5, "tools": [WEATHER], "messages": [user, call, tool_message]}

    return {
        "tool_message": {
            "plain_tool_message": tool_message_case(result),
            "tool_message_with_name": tool_message_case({**result, "name": "get_weather"}),
            "tool_message_with_unknown_key": tool_message_case({**result, "bogus_key": "x"}),
            "tool_message_with_name_and_list_content": tool_message_case(
                {**result, "name": "get_weather", "content": [{"type": "text", "text": "18C sunny"}]},
            ),
        },
        "request": {
            "top_level_unknown_param": {**base, "messages": [user], "bogus_param": 1},
            "unknown_key_in_tool_function": {
                **base,
                "messages": [user],
                "tools": [{**WEATHER, "function": {**WEATHER["function"], "bogus": 1}}],
            },
            "unknown_key_in_tool_call_history": {
                **base,
                "messages": [user, {**call, "tool_calls": [{**call["tool_calls"][0], "index": 0, "bogus": 1}]}, result],
            },
            "unknown_key_in_content_part": {
                **base,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi. One word.", "bogus": 1}]}],
            },
            "name_on_user_message": {**base, "messages": [{**user, "name": "alice"}]},
            "tool_choice_named_not_offered": {
                **base,
                "messages": [user],
                "tools": [WEATHER],
                "tool_choice": {"type": "function", "function": {"name": "get_time"}},
            },
            "tool_choice_required_without_tools": {**base, "messages": [user], "tool_choice": "required"},
            "tool_choice_none_without_tools": {**base, "messages": [user], "tool_choice": "none"},
            "parallel_tool_calls_without_tools": {**base, "messages": [user], "parallel_tool_calls": False},
            "tool_choice_named_finish_reason": {
                **base,
                "messages": [user],
                "tools": [WEATHER, CLOCK],
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            },
            "tool_choice_required_finish_reason": {**base, "messages": [user], "tools": [WEATHER], "tool_choice": "required"},
            "tool_result_unknown_id": {**base, "messages": [user, call, {**result, "tool_call_id": "call_nope"}]},
            "unanswered_call_then_user": {**base, "messages": [user, call, {"role": "user", "content": "Never mind. One word."}]},
            "duplicate_tool_results": {**base, "messages": [user, call, result, result]},
            "duplicate_call_ids_in_history": {
                **base,
                "messages": [
                    user,
                    {
                        **call,
                        "tool_calls": [
                            call["tool_calls"][0],
                            {**call["tool_calls"][0], "function": {"name": "get_weather", "arguments": '{"city": "Rome"}'}},
                        ],
                    },
                    result,
                    {**result, "content": "20C"},
                ],
            },
            "mixed_assistant_text_and_calls": {**base, "messages": [user, {**call, "content": "Let me check."}, result]},
            "tool_result_without_tools_param": {**base, "messages": [user, call, result]},
            "json_schema_strict_with_minimum": {
                **base,
                "messages": [give_x],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "n",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"x": {"type": "number", "minimum": 2}},
                            "required": ["x"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
            "json_schema_strict_missing_additional_props": {
                **base,
                "messages": [give_x],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "n",
                        "strict": True,
                        "schema": {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]},
                    },
                },
            },
            "json_schema_nonstrict_with_minimum": {
                **base,
                "messages": [give_x],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "n",
                        "schema": {"type": "object", "properties": {"x": {"type": "number", "minimum": 2}}, "required": ["x"]},
                    },
                },
            },
            "json_schema_without_schema_key": {
                **base,
                "messages": [give_x],
                "response_format": {"type": "json_schema", "json_schema": {"name": "n"}},
            },
            "tools_with_json_schema": {
                **base,
                "max_tokens": 30,
                "messages": [use_tool],
                "tools": [WEATHER],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "a",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
            "json_object_without_json_in_prompt": {
                **base,
                "messages": [{"role": "user", "content": "Say hi."}],
                "response_format": {"type": "json_object"},
            },
            "temperature_2_5": {**base, "messages": [user], "temperature": 2.5},
            "five_stop_sequences": {**base, "messages": [user], "stop": ["a", "b", "c", "d", "e"]},
            "n_zero": {**base, "messages": [user], "n": 0},
            "max_tokens_zero": {**base, "messages": [user], "max_tokens": 0},
            "empty_messages": {**base, "messages": []},
            "developer_role": {**base, "messages": [{"role": "developer", "content": "Answer in one word."}, user]},
            "seed_and_fingerprint": {**base, "messages": [user], "seed": 7},
            "stream_tool_calls": {
                **base,
                "max_tokens": 40,
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "Call get_weather for Paris and for Rome."}],
                "tools": [WEATHER],
            },
        },
        "tool_control": {
            "unknown_key_in_tool_function": {
                "model": model,
                "max_tokens": 40,
                "messages": [use_tool],
                "tools": [{**WEATHER, "function": {**WEATHER["function"], "bogus": 1}}],
            },
            "unknown_key_in_tools_item": {"model": model, "max_tokens": 40, "messages": [use_tool], "tools": [{**WEATHER, "bogus": 1}]},
            "tool_choice_required_finish_reason": {
                "model": model,
                "max_tokens": 40,
                "messages": [use_tool],
                "tools": [WEATHER],
                "tool_choice": "required",
            },
            "tool_choice_auto_finish_reason": {"model": model, "max_tokens": 40, "messages": [use_tool], "tools": [WEATHER]},
            "json_schema_strict_minimum_enforced": {
                "model": model,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Give x=0 exactly."}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "n",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"x": {"type": "number", "minimum": 2}},
                            "required": ["x"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
            "json_schema_strict_optional_property": {
                "model": model,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Give x=1."}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "n",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                            "required": ["x"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
            "tool_choice_none_with_tools": {
                "model": model,
                "max_tokens": 12,
                "messages": [use_tool],
                "tools": [WEATHER],
                "tool_choice": "none",
            },
            "stream_text_then_tool": {
                "model": model,
                "max_tokens": 60,
                "stream": True,
                "messages": [{"role": "user", "content": "First write the word 'Checking.' then call get_weather for Paris."}],
                "tools": [WEATHER],
            },
        },
        "nested_keys": {
            "tool_calls_on_user_message": {
                "model": model,
                "max_tokens": 5,
                "messages": [
                    {**say_hi, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}
                ],
            },
            "tool_call_id_on_user_message": {"model": model, "max_tokens": 5, "messages": [{**say_hi, "tool_call_id": "call_1"}]},
            "unknown_key_in_response_format": {
                "model": model,
                "max_tokens": 12,
                "messages": [{"role": "user", "content": 'Give json {"x":1}.'}],
                "response_format": {"type": "json_object", "bogus": 1},
            },
            "unknown_key_in_json_schema_object": {
                "model": model,
                "max_tokens": 12,
                "messages": [{"role": "user", "content": "Give x=1."}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "n",
                        "bogus": 1,
                        "schema": {
                            "type": "object",
                            "properties": {"x": {"type": "number"}},
                            "required": ["x"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
            "unknown_key_in_stream_options": {
                "model": model,
                "max_tokens": 5,
                "stream": True,
                "stream_options": {"include_usage": True, "bogus": 1},
                "messages": [say_hi],
            },
            "unknown_key_in_tool_choice_object": {
                "model": model,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Weather in Paris?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
                    },
                ],
                "tool_choice": {"type": "function", "bogus": 1, "function": {"name": "get_weather"}},
            },
        },
        "strict_tools": {
            "strict_tool_missing_additional_props": {
                "model": model,
                "max_tokens": 20,
                "messages": [use_tool],
                "tools": [strict_weather({"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})],
            },
            "strict_tool_optional_property": {
                "model": model,
                "max_tokens": 20,
                "messages": [use_tool],
                "tools": [
                    strict_weather(
                        {
                            "type": "object",
                            "properties": {"city": {"type": "string"}, "unit": {"type": "string"}},
                            "required": ["city"],
                            "additionalProperties": False,
                        },
                    ),
                ],
            },
            "strict_tool_valid": {
                "model": model,
                "max_tokens": 20,
                "messages": [use_tool],
                "tools": [
                    strict_weather(
                        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False},
                    ),
                ],
            },
            "nonstrict_tool_optional_property": {
                "model": model,
                "max_tokens": 20,
                "messages": [use_tool],
                "tools": [
                    strict_weather(
                        {"type": "object", "properties": {"city": {"type": "string"}, "unit": {"type": "string"}}, "required": ["city"]},
                        strict=False,
                    ),
                ],
            },
            "trailing_unanswered_call": {
                "model": model,
                "max_tokens": 5,
                "messages": [
                    use_tool,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
                    },
                ],
                "tools": [strict_weather({"type": "object", "properties": {}, "additionalProperties": False}, strict=False)],
            },
            "duplicate_ids_in_one_assistant_message": {
                "model": model,
                "max_tokens": 5,
                "messages": [
                    use_tool,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
                            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "a"},
                    {"role": "tool", "tool_call_id": "call_1", "content": "b"},
                ],
            },
        },
    }


def post(url: str, headers: dict, body: dict) -> tuple[int, object]:
    """Send one case and return its status and the parsed body, or the list of SSE lines for a served stream."""
    for _ in range(RETRY_ATTEMPTS):
        if body.get("stream"):
            with httpx.stream("POST", url, headers=headers, json=body, timeout=60) as r:
                if r.status_code == 429:
                    time.sleep(RETRY_SECONDS)
                    continue
                if r.status_code == 200:
                    return r.status_code, list(r.iter_lines())
                r.read()
        else:
            r = httpx.post(url, headers=headers, json=body, timeout=60)
            if r.status_code == 429 and "credit" in r.text:
                time.sleep(RETRY_SECONDS)
                continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:500]}
    return 429, {"error": "credit gate after retries"}


def stream_shapes(lines: list[str]) -> tuple[list[dict], list[dict]]:
    """Return the parsed events of a stream and a compact shape of each."""
    events = [json.loads(line[6:]) for line in lines if line.startswith("data: ") and line != "data: [DONE]"]
    shapes = []
    for ev in events:
        if not ev.get("choices"):
            shapes.append({"usage_only": bool(ev.get("usage"))})
            continue
        ch = ev["choices"][0]
        d = ch.get("delta", {})
        calls = []
        for tc in d.get("tool_calls") or []:
            shape = {k: v for k, v in tc.items() if k in ("index", "id", "type")}
            if "function" in tc:
                shape["fn"] = {k: v[:12] if isinstance(v, str) else v for k, v in tc["function"].items()}
            calls.append(shape)
        shapes.append(
            {
                "role": d.get("role"),
                "content": d.get("content"),
                "tool_calls": calls,
                "finish_reason": ch.get("finish_reason"),
                "usage": bool(ev.get("usage")),
            },
        )
    return events, shapes


def brief(status: int, payload: dict) -> dict:
    """Return the fields of a non-streamed reply worth one line."""
    if status == 200:
        choice = payload["choices"][0]
        msg = choice["message"]
        return {
            "finish_reason": choice["finish_reason"],
            "content": (msg.get("content") or "")[:60],
            "tool_calls": [(tc["function"]["name"], tc["function"]["arguments"][:40]) for tc in msg.get("tool_calls") or []],
            "fingerprint": bool(payload.get("system_fingerprint")),
        }
    err = payload.get("error", payload) if isinstance(payload, dict) else {"message": payload}
    if not isinstance(err, dict):
        err = {"message": err}
    return {k: (str(err.get(k))[:140] if err.get(k) is not None else None) for k in ("message", "type", "param", "code")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("groups", nargs="*", help="Case groups to run (default: all). See --list.")
    parser.add_argument("--case", action="append", default=[], help="Run this case only, as group.case; repeatable.")
    parser.add_argument("--list", action="store_true", help="Print every group and case, then exit.")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"), help="OpenAI API key (env OPENAI_API_KEY).")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        help="API base URL (env OPENAI_BASE_URL, default https://api.openai.com/v1).",
    )
    parser.add_argument("--model", default="gpt-4.1-nano", help="Model every case requests (default gpt-4.1-nano).")
    parser.add_argument("--out", default="openai_shape_probe_results.json", help="File the raw responses are written to.")
    args = parser.parse_args()

    groups = build_groups(args.model)
    if args.list:
        for group, cases in groups.items():
            print(group)
            for name in cases:
                print(f"  {group}.{name}")
        return 0
    unknown = [g for g in args.groups if g not in groups]
    if unknown:
        parser.error(f"unknown group(s) {unknown}; choose from {sorted(groups)}")
    if not args.api_key:
        parser.error("an API key is required: pass --api-key or set OPENAI_API_KEY")

    selected: list[tuple[str, str, dict]] = []
    for group, cases in groups.items():
        for name, body in cases.items():
            wanted = (not args.groups and not args.case) or group in args.groups or f"{group}.{name}" in args.case
            if wanted:
                selected.append((group, name, body))
    if not selected:
        parser.error("no case matches; see --list")

    url = f"{args.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json"}
    results: dict[str, dict] = {}
    for group, name, body in selected:
        label = f"{group}.{name}"
        status, payload = post(url, headers, body)
        if isinstance(payload, list):
            events, shapes = stream_shapes(payload)
            results[label] = {"status": status, "events": events}
            print(f"{label:60s} HTTP {status} {len(events)} events")
            for shape in shapes:
                print("   ", json.dumps(shape))
            continue
        results[label] = {"status": status, "response": payload}
        print(f"{label:60s} HTTP {status} {json.dumps(brief(status, payload))}")

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=1)
    print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
