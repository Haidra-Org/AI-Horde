# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pop chat jobs from a running Horde as a fake worker and answer each with a scripted reply (no GPU).

Usage: fake_chat_worker_driver.py [--horde-url URL] [--api-key KEY] model log.jsonl [--twrf] [--name NAME] [--seconds N]

The worker key comes from ``--api-key`` or ``HORDE_WORKER_API_KEY``; it is a separate variable from the requester's
``HORDE_API_KEY`` because both are usually set in the same shell.

Declares every chat feature and guarantee (``tools_with_response_format`` only with ``--twrf``) and
``tools_max_temperature: 1.0``. The reply is chosen from the job payload: a ``[reply:<tag>]`` marker in the last user
message selects a scripted reply; otherwise a well-behaved default (tool call to the offered or named function, prose
after a tool result, a schema-conformant object, JSON for json_object). A refused submit is followed by a faulted
submit, as koboldcpp does. Every pop, payload, submit body and submit response is appended to the log.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# The fake worker's body builders live in the test tree; the repository root is three levels above this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.integration._fake_chat_worker import (  # noqa: E402
    build_chat_submit_body,
    build_faulted_chat_submit_body,
    build_pop_body,
)

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("model", help="Model the worker serves, such as koboldcpp/fake-chat.")
p.add_argument("log", help="JSON lines file every pop, payload and submit is appended to.")
p.add_argument("--api-key", default=os.environ.get("HORDE_WORKER_API_KEY"), help="Worker API key (env HORDE_WORKER_API_KEY).")
p.add_argument("--twrf", action="store_true")
p.add_argument("--name", default="fake-chat-worker")
p.add_argument("--seconds", type=int, default=3600)
p.add_argument("--horde-url", default=os.environ.get("HORDE_URL", "http://127.0.0.1:7101"), help="Horde root (env HORDE_URL).")
a = p.parse_args()
if not a.api_key:
    p.error("a worker key is required: pass --api-key or set HORDE_WORKER_API_KEY")

H = {"apikey": a.api_key, "Client-Agent": "fake_chat_worker_driver:1:local"}
FEATURES = ["seed", "presence_penalty", "frequency_penalty", "tools", "json_object", "json_schema"]
if a.twrf:
    FEATURES.append("tools_with_response_format")
log = open(a.log, "a", encoding="utf-8")


def call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def text_of(msg):
    c = msg.get("content")
    if isinstance(c, list):
        return " ".join(part.get("text", "") for part in c if isinstance(part, dict))
    return c or ""


def sample(schema):
    t = schema.get("type")
    if t == "object":
        return {k: sample(v) for k, v in (schema.get("properties") or {}).items()}
    if t in ("number", "integer"):
        return max(5, schema.get("minimum", 5))
    if t == "string":
        return "Sunny"
    if t == "boolean":
        return True
    if t == "array":
        return []
    return None


def reply(payload):
    """Return (content, tool_calls) for a popped chat payload."""
    msgs = payload.get("messages") or []
    last_user = next((text_of(m) for m in reversed(msgs) if m.get("role") == "user"), "")
    tag = (re.search(r"\[reply:([a-z_]+)\]", last_user) or [None, None])[1]
    tools = payload.get("tools") or []
    names = [t["function"]["name"] for t in tools]
    first = names[0] if names else "get_weather"
    if tag == "two_calls":
        return None, [call(first, {"city": "Paris"}, "call_a"), call(first, {"city": "Rome"}, "call_b")]
    if tag == "undeclared":
        return None, [call("not_offered", {"city": "Paris"}, "call_a")]
    if tag == "dup_ids":
        return None, [call(first, {"city": "Paris"}, "call_a"), call(first, {"city": "Rome"}, "call_a")]
    if tag == "prose":
        return "Sunny.", None
    if tag == "nonconformant":
        return '{"x": 0}', None
    if tag == "text_and_call":
        return "Checking.", [call(first, {"city": "Paris"}, "call_a")]
    if "x=0 exactly" in last_user:
        return '{"x": 0}', None
    if "First write the word 'Checking.'" in last_user:
        return "Checking.", [call(first, {"city": "Paris"}, "call_a")]
    if "for Paris and for Rome" in last_user and tools:
        return None, [call(first, {"city": "Paris"}, "call_a"), call(first, {"city": "Rome"}, "call_b")]
    choice = payload.get("tool_choice")
    last_role = msgs[-1].get("role") if msgs else None
    if tools and choice != "none" and last_role == "user":
        if isinstance(choice, dict):
            return None, [call(choice["function"]["name"], {"city": "Paris"}, "call_a")]
        return None, [call(first, {"city": "Paris"}, "call_a")]
    rf = payload.get("response_format") or {}
    if rf.get("type") == "json_schema":
        return json.dumps(sample(rf.get("json_schema", {}).get("schema", {}))), None
    if rf.get("type") == "json_object":
        return '{"x": 1}', None
    return "Sunny", None


deadline = time.time() + a.seconds
base_name = a.name
generation = 0
while time.time() < deadline:
    body = build_pop_body(
        name=a.name,
        models=[a.model],
        bridge_agent=H["Client-Agent"],
        chat_jobs=True,
        prompt_jobs=False,
        chat_features=FEATURES,
        tools_max_temperature=1.0,
    )
    try:
        r = requests.post(f"{a.horde_url}/api/v2/generate/text/pop", json=body, headers=H, timeout=30)
    except requests.RequestException as e:
        print("pop error", e, flush=True)
        time.sleep(2)
        continue
    if r.status_code == 403 and "WorkerMaintenance" in r.text:
        # Six refused-then-faulted submits per faulting case put a worker into maintenance; continue under a new name.
        generation += 1
        a.name = f"{base_name}-{generation}"
        print("maintenance; continuing as", a.name, flush=True)
        continue
    if r.status_code != 200:
        print("pop", r.status_code, r.text[:300], flush=True)
        time.sleep(2)
        continue
    job = r.json()
    if not job.get("id"):
        time.sleep(0.5)
        continue
    payload = job.get("payload") or {}
    content, tool_calls = reply(payload)
    sub = build_chat_submit_body(
        generation_id=job["id"],
        content=content,
        model_id=a.model,
        finish_reason="tool_calls" if tool_calls else "stop",
        prompt_tokens=20,
        completion_tokens=5,
        tool_calls=tool_calls,
    )
    s = requests.post(f"{a.horde_url}/api/v2/generate/text/submit", json=sub, headers=H, timeout=30)
    entry = {
        "t": time.time(),
        "id": job["id"],
        "payload": payload,
        "submit": sub,
        "submit_status": s.status_code,
        "submit_response": s.text[:600],
    }
    if s.status_code >= 400:
        f = requests.post(
            f"{a.horde_url}/api/v2/generate/text/submit",
            json=build_faulted_chat_submit_body(generation_id=job["id"], model_id=a.model),
            headers=H,
            timeout=30,
        )
        entry["fault_status"] = f.status_code
        entry["fault_response"] = f.text[:300]
    log.write(json.dumps(entry) + "\n")
    log.flush()
    print(job["id"], s.status_code, s.text[:160], entry.get("fault_status"), flush=True)
