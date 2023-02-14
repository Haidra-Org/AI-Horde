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

