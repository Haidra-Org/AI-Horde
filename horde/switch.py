# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

class Switch:
    active = False

    def activate(self):
        self.active = True

    def disable(self):
        self.active = False

    def toggle(self, value):
        self.active = value
