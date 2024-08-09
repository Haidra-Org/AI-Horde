# SPDX-FileCopyrightText: 2022 AI Horde developers
#
# SPDX-License-Identifier: AGPL-3.0-only

class Switch:
    active = False

    def activate(self):
        self.active = True

    def disable(self):
        self.active = False

    def toggle(self, value):
        self.active = value
