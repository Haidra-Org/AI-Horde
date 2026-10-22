# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Forwards OpenAI-style requests to an upstream front and records each exchange, for seeing what a client sends.

Usage: python -I logging_proxy.py <log dir> [--listen ADDR] [--port N] [--upstream URL]

Listens on 127.0.0.1:8790 by default and forwards every path to the upstream (``--upstream`` or ``CHAT_PROXY_UPSTREAM``,
default the local front at http://127.0.0.1:7002), streaming the response back unchanged. Each exchange is written to
<log dir>/<n>-<method>-<path>.json: the request headers (Authorization masked) and JSON body, the response status,
headers, and the body or the raw stream text. A one-line summary per exchange goes to stdout.
"""

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

LOG_DIR = Path()
UPSTREAM = ""
HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
counter = 0
counter_lock = threading.Lock()
client = httpx.Client(timeout=httpx.Timeout(connect=30, read=900, write=60, pool=30))


def summarise(body):
    if not isinstance(body, dict):
        return ""
    tools = body.get("tools") or []
    messages = body.get("messages") or []
    chars = len(json.dumps(messages))
    return (
        f"model={body.get('model')} stream={body.get('stream')} max_tokens={body.get('max_tokens')} "
        f"max_completion_tokens={body.get('max_completion_tokens')} messages={len(messages)} message_chars={chars} "
        f"tools={len(tools)} tool_chars={len(json.dumps(tools))} keys={sorted(body)}"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self):
        global counter
        with counter_lock:
            counter += 1
            n = counter
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        record = {
            "n": n,
            "time": time.strftime("%H:%M:%S"),
            "method": self.command,
            "path": self.path,
            "request_headers": {
                k: ("<masked>" if k.lower() in ("authorization", "api-key", "x-api-key") else v) for k, v in headers.items()
            },
            "request_body": body,
        }
        t0 = time.time()
        chunks = []
        try:
            req = client.build_request(self.command, UPSTREAM + self.path, headers=headers, content=raw)
            resp = client.send(req, stream=True)
            record["status"] = resp.status_code
            record["response_headers"] = dict(resp.headers)
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in HOP_HEADERS and k.lower() != "content-encoding":
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in resp.iter_raw():
                if not chunk:
                    continue
                chunks.append(chunk)
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            resp.close()
        except Exception as ex:  # noqa: BLE001
            record["exception"] = repr(ex)
            try:
                self.send_error(502, repr(ex))
            except Exception:  # noqa: BLE001
                pass
        text = b"".join(chunks).decode(errors="replace")
        try:
            record["response_body"] = json.loads(text)
        except ValueError:
            record["response_body"] = text
        record["seconds"] = round(time.time() - t0, 1)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        name = f"{n:03d}-{self.command}-{self.path.strip('/').replace('/', '_')[:60]}.json"
        (LOG_DIR / name).write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
        tail = text[-300:].replace("\n", " ") if record.get("status", 0) >= 400 or "exception" in record else ""
        print(
            f"#{n} {record['time']} {self.command} {self.path} -> {record.get('status')} in {record['seconds']}s "
            f"{summarise(body)} {record.get('exception', '')} {tail}",
            flush=True,
        )

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log_dir", type=Path, help="Directory each exchange is written to.")
    parser.add_argument("--listen", default="127.0.0.1", help="Address to bind (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8790, help="Port to bind (default 8790).")
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CHAT_PROXY_UPSTREAM", "http://127.0.0.1:7002"),
        help="Front root every path is forwarded to (env CHAT_PROXY_UPSTREAM, default http://127.0.0.1:7002).",
    )
    args = parser.parse_args()
    LOG_DIR = args.log_dir
    UPSTREAM = args.upstream.rstrip("/")
    print(f"logging proxy on http://{args.listen}:{args.port} -> {UPSTREAM}, logs in {LOG_DIR}", flush=True)
    ThreadingHTTPServer((args.listen, args.port), Handler).serve_forever()
