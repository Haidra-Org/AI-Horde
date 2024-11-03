# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import enum


class State(enum.Enum):
    WAITING = 0
    PROCESSING = 1
    DONE = 2
    CANCELLED = 3
    FAULTED = 4
    PARTIAL = 5


class ImageGenState(enum.Enum):
    OK = 0
    CENSORED = 1
    CANCELLED = 3
    FAULTED = 4


class UserRecordTypes(enum.Enum):
    CONTRIBUTION = 0
    USAGE = 1
    FULFILLMENT = 3
    REQUEST = 4
    STYLE = 5


class UserRoleTypes(enum.Enum):
    MODERATOR = 0
    TRUSTED = 1
    FLAGGED = 3
    CUSTOMIZER = 4
    VPN = 5
    SPECIAL = 6
    SERVICE = 7
    EDUCATION = 8


class ReturnedEnum(enum.Enum):
    @property
    def code(self):
        return self.name

    @property
    def message(self):
        return self.value


class WarningMessage(ReturnedEnum):
    NoAvailableWorker = (
        "Warning: No available workers can fulfill this request. "
        "It will expire in 20 minutes unless a worker appears. "
        "Please confider reducing its size of the request or choosing a different model."
    )
    ClipSkipMismatch = "The clip skip specified for this generation does not match the requirements of one of the requested models."
    StepsTooFew = "The steps specified for this generation are too few for this model."
    StepsTooMany = "The steps specified for this generation are too many for this model."
    CfgScaleMismatch = "The cfg scale specified for this generation does not match the requirements of one of the requested models."
    CfgScaleTooSmall = "The cfg_scale specified for this generation is too small for this model."
    CfgScaleTooLarge = "The cfg_scale specified for this generation is too large for this model."
    SamplerMismatch = "The requested sampler does not match the requirements for one of the requested models."
    SchedulerMismatch = "The requested scheduler does not match the requirements for one of the requested models."
