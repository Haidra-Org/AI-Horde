# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from urllib.parse import urlencode, urlsplit

import requests
from loguru import logger


def moderation_event_url(event_id: int) -> str | None:
    """Return the frontpage review link for one prompt moderation event.

    Args:
        event_id: The `prompt_moderation_events` row an alert refers to.

    Returns:
        The encoded review URL, or None when `HORDE_MODERATION_FRONTPAGE_URL` is unset or not an absolute HTTP URL.
    """
    base_url = os.getenv("HORDE_MODERATION_FRONTPAGE_URL", "").rstrip("/")
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        logger.warning("HORDE_MODERATION_FRONTPAGE_URL must be an absolute HTTP URL without a query or fragment")
        return None
    return f"{base_url}/admin/review?{urlencode({'tab': 'prompts', 'event_id': event_id})}"


def send_webhook(webhook_url: str, message: str):
    data = {"content": message}
    try:
        req = requests.post(webhook_url, json=data, timeout=2)
        if not req.ok:
            logger.warning(f"Something went wrong when sending discord webhook: {req.status_code} - {req.text}")
            return
    except Exception as err:
        logger.warning(f"Exception when sending discord webhook: {err}")
        return


def send_pause_notification(message: str):
    webhook_url = os.getenv("DISCORD_PAUSED_NOTICE_WEBHOOK")
    if not webhook_url:
        logger.warning("Cannot send Pause notification. No DISCORD_PAUSED_NOTICE_WEBHOOK set")
        return
    send_webhook(webhook_url, message)


def send_problem_user_notification(message: str):
    webhook_url = os.getenv("DISCORD_PROBLEM_USER_WEBHOOK")
    if not webhook_url:
        logger.warning("Cannot send Pause notification. No DISCORD_PROBLEM_USER_WEBHOOK set")
        return
    send_webhook(webhook_url, message)
