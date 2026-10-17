# SPDX-FileCopyrightText: 2025 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from environs import Env

env = Env()
env.read_env()  # read .env file, if it exists


class Config:
    # WIP - Not enabled yet
    horde_title: str = env.str("HORDE_TITLE", default="AI Horde")
    chat_admission_enabled: bool = env.bool("HORDE_CHAT_ADMISSION_ENABLED", default=True)
    """Whether ``POST /v2/generate/chat/async`` accepts new chat requests. Turning it off refuses only new
    requests: status, cancel, pop and submit keep serving chat requests already queued, so a deployment can stop
    admitting chat work and drain what is in flight."""
