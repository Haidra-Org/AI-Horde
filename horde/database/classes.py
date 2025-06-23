# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid
from datetime import datetime

from loguru import logger

from horde.threads import PrimaryTimedFunction
from horde.vars import horde_instance_id


class FakeWPRow:
    def __init__(self, json_row):
        self.id = uuid.UUID(json_row["id"])
        self.things = json_row["things"]
        self.n = json_row["n"]
        self.extra_priority = json_row["extra_priority"]
        self.created = datetime.strptime(json_row["created"], "%Y-%m-%d %H:%M:%S")


class Quorum(PrimaryTimedFunction):
    quorum = None

    def call_function(self):
        self.quorum = self.function(*self.args, **self.kwargs)

    def is_primary(self):
        return self.quorum == horde_instance_id


class CachedPasskeys(PrimaryTimedFunction):
    passkeys = []

    def call_function(self):
        self.passkeys = self.function(*self.args, **self.kwargs)

    def is_passkey_known(self, passkey):
        logger.debug(f"Checking if passkey {passkey} is known in {self.passkeys}")
        return passkey in self.passkeys
