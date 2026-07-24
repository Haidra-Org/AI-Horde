# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import enum


class KudosEntryType(enum.StrEnum):
    """Classifies a kudos ledger posting by the business event that produced it.

    The entry type does not imply direction: each posting carries a signed
    ``amount`` and the applier folds it into exactly one balance. The type is
    the audit/reporting axis and the phase-2 extension point.

    Members document which postings the producing event emits:

    * ``GENERATION`` -- a generation or interrogation settlement: worker-balance
      credit, worker-owner credit (half to escrow while untrusted), and
      requester debit (each a separate posting).
    * ``UPTIME_REWARD`` -- a worker uptime crossing: worker-balance credit and
      owner credit (escrow while untrusted, unless the worker type bypasses it).
    * ``EVALUATION_PROMOTION`` -- a trust promotion delta pair: escrow debit and
      spendable-balance credit for the amount read at promotion time.
    * ``TRANSFER`` -- a user-to-user gift: source debit and destination credit.
    * ``ADMIN_ADJUSTMENT`` -- an administrator balance delta.
    * ``AWARD`` -- an award or recurring monthly-kudos credit.
    * ``STYLE_REWARD`` -- the fixed style-owner credit on a styled generation.
    * ``STAT_RECORD`` -- a per-user records movement (request/fulfilment counts
      and scaled thing totals); denominated in ``count`` or ``things``, not kudos.
    * ``STAT_CONTRIBUTION`` -- a worker aggregate movement (contributions things,
      fulfilment count); denominated in ``things`` or ``count``, not kudos.
    * ``FLOOR_ADJUSTMENT`` -- explicit supply created when the compatibility
      minimum-balance rule forgives the part of a debit below an account floor.
    * ``STAT_ACTIVITY`` -- an asynchronous ``users.last_active`` touch that
      keeps settlement transactions from locking requester/worker-owner rows.
    """

    GENERATION = "GENERATION"
    UPTIME_REWARD = "UPTIME_REWARD"
    EVALUATION_PROMOTION = "EVALUATION_PROMOTION"
    TRANSFER = "TRANSFER"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"
    AWARD = "AWARD"
    STYLE_REWARD = "STYLE_REWARD"
    STAT_RECORD = "STAT_RECORD"
    STAT_CONTRIBUTION = "STAT_CONTRIBUTION"
    STAT_ACTIVITY = "STAT_ACTIVITY"
    FLOOR_ADJUSTMENT = "FLOOR_ADJUSTMENT"
    RECONCILIATION = "RECONCILIATION"


class KudosLedgerMode(enum.StrEnum):
    """Runtime cutover mode for kudos mutations."""

    SHADOW = "shadow"
    LEDGER = "ledger"


class KudosUnit(enum.StrEnum):
    """Units accepted by the kudos projection event schemas."""

    KUDOS = "kudos"
    THINGS = "things"
    COUNT = "count"


class KudosStatRecord(enum.StrEnum):
    """Reserved record discriminators interpreted by the stats projector."""

    USER_KUDOS = "user_kudos"
    WORKER_KUDOS = "worker_kudos"
    LAST_ACTIVE = "last_active"


class KudosAggregate(enum.StrEnum):
    """Worker and team aggregate names interpreted by the stats projector."""

    CONTRIBUTIONS = "contributions"
    FULFILMENTS = "fulfilments"


class KudosAuditDetail(enum.StrEnum):
    """Stable keys stored in kudos audit metadata."""

    REASON = "reason"
    RESERVATION_ID = "reservation_id"
    SNAPSHOT_ID = "snapshot_id"
    TOUCH_LAST_ACTIVE = "touch_last_active"


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
    DELETED = 9


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
