# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Free-tier daily quota.

The mission is "free AI for everyone, funded by paid usage." This is the code
that makes the free part real: every user gets a daily allowance of requests,
metered per UTC day in Redis. Accounts carrying an explicit ``quota_exempt``
policy flag are unmetered.

Design notes:
  - Meter ACCEPTED requests only: callers invoke this right before a request
    is actually queued, so a user never burns quota on a 503/validation error.
  - Fail OPEN: if Redis is unavailable we log and allow the request. A quota
    store outage must never take down inference for everyone.
  - The day key auto-expires, so there's no cleanup job and no unbounded
    key growth.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException

from ..redis_client import get_redis

logger = logging.getLogger("grid_api.quota")

# Requests/day for the free tier. Generous enough to learn and build on,
# bounded enough that one anonymous user can't drain scarce workers.
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "200"))

_QUOTA_PREFIX = "grid:quota:"


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # next midnight
    secs = 86400 - (now - tomorrow).seconds
    return max(secs, 1)


def is_paid(user: dict) -> bool:
    """True if account policy explicitly exempts the user from the daily cap."""
    return bool(user.get("quota_exempt"))


async def check_and_consume(user: dict) -> None:
    """Consume one unit of the user's daily quota; raise 429 if exhausted.

    Call this immediately before a request is queued. Paid/contributor users
    pass through untouched. On any Redis error we fail open (allow the request)
    so a quota-store outage doesn't break inference.
    """
    if is_paid(user):
        return

    user_id = user.get("id")
    if user_id is None:
        return  # can't meter without an id; don't block

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{_QUOTA_PREFIX}{user_id}:{day}"

    try:
        r = get_redis()
        count = await r.incr(key)
        if count == 1:
            # First request today — set the key to expire at day's end.
            await r.expire(key, _seconds_until_utc_midnight())
    except Exception as e:
        # Fail open: never block legitimate traffic on a quota-store outage.
        logger.warning(f"quota check failed open for user {user_id}: {e}")
        return

    if count > FREE_DAILY_LIMIT:
        reset_in = _seconds_until_utc_midnight()
        raise HTTPException(
            status_code=429,
            detail=(
                f"Free daily limit reached ({FREE_DAILY_LIMIT} requests/day). "
                f"Resets in {reset_in // 3600}h {(reset_in % 3600) // 60}m. "
                f"Upgrade or stake AIPG for unlimited access: https://aipowergrid.io"
            ),
            headers={"Retry-After": str(reset_in)},
        )


async def remaining(user: dict) -> int | None:
    """Requests left today, or None for paid/unmetered users. Best-effort."""
    if is_paid(user):
        return None
    user_id = user.get("id")
    if user_id is None:
        return None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{_QUOTA_PREFIX}{user_id}:{day}"
    try:
        r = get_redis()
        used = int(await r.get(key) or 0)
    except Exception:
        return None
    return max(FREE_DAILY_LIMIT - used, 0)
