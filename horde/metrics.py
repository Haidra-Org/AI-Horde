# SPDX-FileCopyrightText: 2022 AI Horde developers
#
# SPDX-License-Identifier: AGPL-3.0-only

class WaitressMetrics:
    task_dispatcher = None

    def setup(self, td):
        self.task_dispatcher = td

    @property
    def queue(self):
        return len(self.task_dispatcher.queue)

    @property
    def threads(self):
        return len(self.task_dispatcher.threads)

    @property
    def active_count(self):
        # -1 to ignore the /metrics task
        return self.task_dispatcher.active_count - 1


waitress_metrics = WaitressMetrics()
