import enum

class State(enum.Enum):
    WAITING = 0
    PROCESSING = 1
    DONE = 2
    CANCELLED = 3
    FAULTED = 4

