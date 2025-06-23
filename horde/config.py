# SPDX-FileCopyrightText: 2025 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from environs import Env

env = Env()
env.read_env()  # read .env file, if it exists


class Config:
    # WIP - Not enabled yet
    horde_title: str = env.str("HORDE_TITLE", default="AI Horde")
