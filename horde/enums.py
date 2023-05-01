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

class UserRoleTypes(enum.Enum):
    MODERATOR = 0
    TRUSTED = 1
    FLAGGED = 3
    CUSTOMIZER = 4

