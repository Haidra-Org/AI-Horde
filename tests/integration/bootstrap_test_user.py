# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bootstrap a local/CI integration test user and emit its API key.

This script calls the test-only endpoint at /api/v2/dev/test-user. That endpoint
is doubly gated and will refuse to mint a key unless both hold:

* The server has ``HORDE_TEST_APIKEYS`` enabled (1/true/yes/on); otherwise it
  returns 404 and is indistinguishable from absent. Production never sets it.
* The request reaches the server over the loopback interface, checked against
  the genuine socket peer (X-Forwarded-For cannot spoof it); otherwise 403.

So run this from the same host as the server, against its direct address (the
default ``http://localhost:7001``), not through a remote proxy.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create or rotate a test user API key")
    parser.add_argument("--horde-url", default="http://localhost:7001", help="Base Horde URL")
    parser.add_argument("--username", default="test_user", help="Test username")
    parser.add_argument("--oauth-id", default="ci_test_user", help="Stable test oauth_id")
    parser.add_argument("--kudos", type=int, default=10000, help="Initial kudos for test user")
    parser.add_argument("--github-env", help="Path to GITHUB_ENV file to export AI_HORDE_DEV_APIKEY")
    return parser.parse_args()


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _bootstrap_test_user(base_url: str, username: str, oauth_id: str, kudos: int) -> str:
    payload = {
        "username": username,
        "oauth_id": oauth_id,
        "moderator": True,
        "trusted": True,
        "kudos": kudos,
    }
    request = urllib.request.Request(
        f"{_normalize_base_url(base_url)}/api/v2/dev/test-user",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        error_body = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Bootstrap request failed with status {err.code}: {error_body}",
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Bootstrap request failed: {err}") from err

    try:
        data = json.loads(body)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Bootstrap endpoint returned non-JSON response: {body}") from err

    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise RuntimeError(f"Bootstrap response is missing api_key: {data}")
    return api_key


def _append_to_github_env(path: str, api_key: str) -> None:
    with open(path, "a", encoding="utf-8") as env_file:
        env_file.write(f"AI_HORDE_DEV_APIKEY={api_key}\n")


def main() -> int:
    args = _parse_args()
    try:
        api_key = _bootstrap_test_user(args.horde_url, args.username, args.oauth_id, args.kudos)
    except RuntimeError as err:
        print(str(err), file=sys.stderr)
        return 1

    if args.github_env:
        _append_to_github_env(args.github_env, api_key)
    else:
        print(api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
