# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Drive one chat request through the local native Horde: create users, submit, poll status, print the outcome.

``--mode bootstrap`` creates ``chat_worker_user`` and ``chat_requester`` through ``/api/v2/dev/test-user`` (the Horde
must run with ``HORDE_TEST_APIKEYS=1``, as ``start_horde.sh`` sets it). It prints each user without its key on stderr
and the two keys as ``export`` lines on stdout, so ``eval "$(python native_roundtrip.py --mode bootstrap)"`` sets
``HORDE_API_KEY`` (the requester) and ``HORDE_WORKER_API_KEY`` (the worker) for the other tools.

``--mode request --model <name>`` submits a chat request to the native route with the requester key and polls the
status until it is done or faulted.
"""

import argparse
import json
import os
import shlex
import sys
import time
import uuid

import requests

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("--horde-url", default=os.environ.get("HORDE_URL", "http://127.0.0.1:7101"), help="Horde root (env HORDE_URL).")
p.add_argument("--model", help="The model to request; required by --mode request.")
p.add_argument("--mode", choices=["bootstrap", "request"], required=True)
p.add_argument("--max-tokens", type=int, default=64)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY"), help="Requester API key for --mode request (env HORDE_API_KEY).")
a = p.parse_args()
if a.mode == "request" and a.model is None:
    p.error("--mode request requires --model")
if a.mode == "request" and not a.api_key:
    p.error("--mode request requires --api-key or HORDE_API_KEY")

if a.mode == "bootstrap":
    exports = {"chat_worker_user": "HORDE_WORKER_API_KEY", "chat_requester": "HORDE_API_KEY"}
    for name, variable in exports.items():
        r = requests.post(
            f"{a.horde_url}/api/v2/dev/test-user",
            json={"username": name, "oauth_id": f"local-{name}", "kudos": 100000, "trusted": True},
            timeout=10,
        )
        r.raise_for_status()
        print(name, json.dumps({k: v for k, v in r.json().items() if k != "api_key"}), file=sys.stderr)
        print(f"export {variable}={shlex.quote(r.json()['api_key'])}")
    sys.exit(0)

body = {
    "messages": [
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": "Name the capital of France and one fact about it."},
    ],
    "model": a.model,
    "max_tokens": a.max_tokens,
    "temperature": 0.7,
    "seed": 7,
    "horde": {"operation_key": str(uuid.uuid4())},
}
h = {"apikey": a.api_key, "Client-Agent": "native_roundtrip:1:local"}
r = requests.post(f"{a.horde_url}/api/v2/generate/chat/async", json=body, headers=h, timeout=30)
print("async", r.status_code, r.text[:400])
r.raise_for_status()
rid = r.json()["id"]
t0 = time.time()
while time.time() - t0 < 300:
    s = requests.get(f"{a.horde_url}/api/v2/generate/chat/status/{rid}", headers=h, timeout=30)
    js = s.json()
    print(
        f"{time.time() - t0:6.1f}s status={s.status_code} done={js.get('done')} processing={js.get('processing')} "
        f"waiting={js.get('waiting')} is_possible={js.get('is_possible')} faulted={js.get('faulted')}",
    )
    if js.get("done") or js.get("faulted"):
        print(json.dumps(js, indent=2)[:3000])
        break
    time.sleep(3)
