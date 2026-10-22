# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A minimal coding agent on the OpenAI SDK, pointed at the Horde's OpenAI front.

It copies a small project with failing tests into a fresh workspace, then lets the model read files, write files and
run the tests through tool calls until the tests pass or the turn budget runs out. Every request and tool result is
written to a Markdown transcript.

The workspace copy and ``transcript.md`` are written under ``--out``; the workspace is deleted and recreated on every run.
``run_tests`` executes model-written code with this interpreter, confined only by the workspace path check.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openai import OpenAI

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument("--model", default="koboldcpp/Qwen3.8-27B")
p.add_argument(
    "--base-url", default=os.environ.get("CHAT_FRONT_URL", "http://127.0.0.1:7002/v1"), help="Front base URL (env CHAT_FRONT_URL)."
)
p.add_argument("--api-key", default=os.environ.get("HORDE_API_KEY", "0000000000"), help="API key (env HORDE_API_KEY, default anonymous).")
p.add_argument("--out", type=Path, default=Path("coding_demo_run"), help="Directory the workspace and transcript are written to.")
a = p.parse_args()

SEED = Path(__file__).resolve().parent / "seed_project"
WORK = a.out / "workspace"
TRANSCRIPT = a.out / "transcript.md"
MODEL = a.model
MAX_TURNS = 14

client = OpenAI(base_url=a.base_url, api_key=a.api_key, timeout=600, max_retries=0)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files in the project.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a project file and return its text.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to the project root"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Replace a project file with new full contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the project root"},
                    "content": {"type": "string", "description": "The complete new file contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the project's test suite with pytest and return the output.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

SYSTEM = (
    "You are a careful software engineer working in a small Python project through tools. "
    "Investigate before editing: run the tests, read the failing code, then make the smallest correct fix. "
    "Never edit the tests. When writing a file, send its complete contents. "
    "When the tests pass, reply with a short summary of the bug and the fix, and call no more tools."
)
TASK = "The tests in this project are failing after a recent change to the order pricing code. Please fix it."


def resolve(path: str) -> Path:
    target = (WORK / path).resolve()
    if WORK.resolve() not in target.parents:
        raise ValueError("path outside the project")
    return target


def run_tool(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            return "\n".join(sorted(str(p.relative_to(WORK)).replace("\\", "/") for p in WORK.rglob("*.py")))
        if name == "read_file":
            return resolve(args["path"]).read_text(encoding="utf-8")
        if name == "write_file":
            target = resolve(args["path"])
            if target.name.startswith("test_"):
                return "Refused: tests are read-only."
            target.write_text(args["content"], encoding="utf-8")
            return f"Wrote {len(args['content'])} characters to {args['path']}."
        if name == "run_tests":
            proc = subprocess.run(
                [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-rN", "--tb=short"],
                cwd=WORK,
                capture_output=True,
                text=True,
                timeout=60,
            )
            return (proc.stdout + proc.stderr)[-2500:]
        return f"Unknown tool {name}"
    except Exception as ex:  # noqa: BLE001
        return f"Error: {ex}"


def main() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.copytree(SEED, WORK)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    log = [
        f"# Coding agent demo\n\nModel `{MODEL}` through `{a.base_url}`, OpenAI Python SDK.\n",
        f"**System:** {SYSTEM}\n",
        f"**User:** {TASK}\n",
    ]
    totals = {"prompt": 0, "completion": 0, "seconds": 0.0}
    for turn in range(1, MAX_TURNS + 1):
        t0 = time.time()
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, temperature=0.2, max_tokens=512)
        elapsed = time.time() - t0
        choice = resp.choices[0]
        msg = choice.message
        totals["prompt"] += resp.usage.prompt_tokens
        totals["completion"] += resp.usage.completion_tokens
        totals["seconds"] += elapsed
        horde = (resp.model_extra or {}).get("x_horde", {})
        log.append(
            f"## Turn {turn} ({elapsed:.1f} s, {resp.usage.prompt_tokens} prompt / {resp.usage.completion_tokens} completion tokens, "
            f"finish `{choice.finish_reason}`, worker `{horde.get('worker_name')}`)\n"
        )
        print(
            f"turn {turn}: {elapsed:.1f}s finish={choice.finish_reason} calls={[c.function.name for c in msg.tool_calls or []]}", flush=True
        )
        assistant = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant["tool_calls"] = [c.model_dump(include={"id", "type", "function"}) for c in msg.tool_calls]
        messages.append(assistant)
        if msg.content:
            log.append(f"**Assistant:**\n\n{msg.content.strip()}\n")
        if not msg.tool_calls:
            break
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            result = run_tool(call.function.name, args)
            shown_args = {k: (v if k != "content" else f"<{len(v)} characters>") for k, v in args.items()}
            log.append(f"**Tool call** `{call.function.name}({json.dumps(shown_args)})`\n")
            if call.function.name == "write_file":
                log.append(f"```python\n{args.get('content', '')}\n```\n")
            log.append(f"<details><summary>Result</summary>\n\n```\n{result}\n```\n</details>\n")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    final = run_tool("run_tests", {})
    log.append(f"## Final test run (outside the agent)\n\n```\n{final}\n```\n")
    log.append(
        f"Totals: {totals['prompt']} prompt and {totals['completion']} completion tokens over {turn} turns, "
        f"{totals['seconds']:.0f} s waiting on the Horde.\n"
    )
    TRANSCRIPT.write_text("\n".join(log), encoding="utf-8")
    print(final.strip().splitlines()[-1] if final.strip() else final)
    print(f"transcript: {TRANSCRIPT}")


if __name__ == "__main__":
    main()
