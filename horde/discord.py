import os

import requests
from loguru import logger


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
