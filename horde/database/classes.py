# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import hmac
import uuid
from datetime import datetime

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

    def is_primary(self) -> bool:
        return self.quorum == horde_instance_id


class CachedPasskeys(PrimaryTimedFunction):
    passkeys = {}

    def call_function(self):
        self.passkeys = self.function(*self.args, **self.kwargs)

    def is_passkey_known(self, passkey: str | None) -> bool:
        """Return whether ``passkey`` is the proxy passkey of an account.

        A request's ``Proxied-For`` header replaces its address only when this returns True, so the supplied value must
        match a cached passkey; that some account has a passkey is not enough.

        Args:
            passkey: The request's ``Proxy-Authorization`` header value, or None when it sent none.
        """
        return self.get_passkey_owner(passkey) is not None

    def get_passkey_owner(self, passkey: str | None) -> int | None:
        """Return the id of the account whose proxy passkey is ``passkey``, or None when no account's is.

        Each comparison takes the same time however much of the value matches, so response timing does not reveal a
        partly correct guess.

        Args:
            passkey: The request's ``Proxy-Authorization`` header value, or None when it sent none.
        """
        if not passkey:
            return None
        supplied_passkey = passkey.encode("utf-8")
        for user_id, known_passkey in self.passkeys.items():
            if hmac.compare_digest(supplied_passkey, known_passkey.encode("utf-8")):
                return user_id
        return None
