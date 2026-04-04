# SPDX-FileCopyrightText: 2026 Tazlin
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate N test API keys by registering users against a local AI Horde instance.

DEPRECATED: ``locustfile.py`` now auto-registers requestor & worker users at
``test_start`` via ``--bootstrap-requestors`` / ``--bootstrap-workers``. This
script is still useful if you want the raw keys on disk for other tooling
(``tests/integration``, ad-hoc curl), but it is no longer required to run the
locust stress suite.

Hits the /register web form endpoint directly. Captcha is skipped automatically
when the server has no RECAPTCHA_SECRET_KEY set (the default for local dev).

Usage:
    python tests/stress/gen_api_keys.py                          # 10 keys, default host
    python tests/stress/gen_api_keys.py -n 20 --host http://localhost:7001
    python tests/stress/gen_api_keys.py -n 5 --role worker --out worker_keys.txt

The generated keys are printed to stdout (one per line) and optionally written
to a file via --out.  The output format is directly usable as the value for
HORDE_REQUESTOR_API_KEYS or HORDE_WORKER_API_KEYS (comma-separated).
"""
from __future__ import annotations

import argparse
import sys
import uuid

import requests


def _register_user(session: requests.Session, base_url: str, username: str) -> str | None:
    """POST the registration form and return the raw API key, or None on failure."""
    resp = session.post(
        f"{base_url}/register",
        data={"username": username},
        allow_redirects=False,
    )
    if resp.status_code == 200:
        # The template renders:  <code class="ah-api-key">KEY</code>
        text = resp.text
        marker = 'class="ah-api-key">'
        idx = text.find(marker)
        if idx == -1:
            return None
        start = idx + len(marker)
        end = text.find("<", start)
        if end == -1:
            return None
        return text[start:end].strip() or None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate test API keys for a local AI Horde instance")
    parser.add_argument("-n", "--count", type=int, default=10, help="Number of API keys to create (default: 10)")
    parser.add_argument("--host", default="http://localhost:7001", help="Base URL of the Horde instance")
    parser.add_argument(
        "--role",
        choices=["requestor", "worker", "both"],
        default="both",
        help="Label prefix for generated usernames (default: both)",
    )
    parser.add_argument("--out", type=str, default=None, help="Write keys to this file (one per line)")
    parser.add_argument("--csv", action="store_true", help="Print a single comma-separated line instead of one-per-line")
    args = parser.parse_args(argv)

    roles = ["requestor", "worker"] if args.role == "both" else [args.role]
    keys: dict[str, list[str]] = {r: [] for r in roles}
    per_role = args.count // len(roles)
    remainder = args.count % len(roles)

    session = requests.Session()

    # Quick connectivity check — try registering a canary user to verify captcha isn't enforced
    try:
        session.get(f"{args.host}/register", timeout=5)
    except requests.ConnectionError:
        print(f"ERROR: Could not connect to {args.host}", file=sys.stderr)
        return 1

    canary_name = f"stress_canary_{uuid.uuid4().hex[:8]}"
    canary_key = _register_user(session, args.host, canary_name)
    if canary_key is None:
        print(
            "ERROR: Could not register a test user. The server likely has "
            "reCAPTCHA enabled (RECAPTCHA_SECRET_KEY is set). This script "
            "only works against a local instance without it.",
            file=sys.stderr,
        )
        return 1

    # Count the canary toward the first role's quota
    first_role = roles[0]
    keys[first_role].append(canary_key)

    total = 1
    for i, role in enumerate(roles):
        n = per_role + (1 if i < remainder else 0)
        # The canary already consumed one slot from the first role
        if role == first_role:
            n -= 1
        for _ in range(n):
            tag = uuid.uuid4().hex[:8]
            username = f"stress_{role}_{tag}"
            api_key = _register_user(session, args.host, username)
            if api_key:
                keys[role].append(api_key)
                total += 1
            else:
                print(f"WARNING: Failed to register user '{username}'", file=sys.stderr)

    # Output
    if args.csv:
        for role in roles:
            if keys[role]:
                print(f"# {role}")
                print(",".join(keys[role]))
    else:
        for role in roles:
            if keys[role]:
                print(f"# {role}")
                for k in keys[role]:
                    print(k)

    if args.out:
        with open(args.out, "w") as f:
            for role in roles:
                for k in keys[role]:
                    f.write(f"{k}\n")
        print(f"\nWrote {total} keys to {args.out}", file=sys.stderr)

    # Print env-var-ready lines to stderr for convenience
    if total:
        print("\n# Paste into your shell or .env:", file=sys.stderr)
        for role in roles:
            if keys[role]:
                var = "HORDE_REQUESTOR_API_KEYS" if role == "requestor" else "HORDE_WORKER_API_KEYS"
                print(f'export {var}="{",".join(keys[role])}"', file=sys.stderr)

    print(f"\nGenerated {total}/{args.count} keys", file=sys.stderr)
    return 0 if total == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
