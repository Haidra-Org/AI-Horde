
from enum import IntEnum

class Suspicions(IntEnum):
    WORKER_NAME_LONG = 0
    WORKER_NAME_EXTREME = 1
    WORKER_PROFANITY = 2
    UNSAFE_IP = 3
    EXTREME_MAX_PIXELS = 4
    UNREASONABLY_FAST = 5
    USERNAME_LONG = 6
    USERNAME_PROFANITY = 7
    CORRUPT_PROMPT = 8
    TOO_MANY_JOBS_ABORTED = 9

SUSPICION_LOGS = {
    Suspicions.WORKER_NAME_LONG: 'Worker Name too long',
    Suspicions.WORKER_NAME_EXTREME: 'Worker Name extremely long',
    Suspicions.WORKER_PROFANITY: 'Discovered profanity in worker name {}',
    Suspicions.UNSAFE_IP: 'Worker using unsafe IP',
    Suspicions.EXTREME_MAX_PIXELS: 'Worker claiming they can generate too many pixels',
    Suspicions.UNREASONABLY_FAST: 'Generation unreasonably fast ({})',
    Suspicions.USERNAME_LONG: 'Username too long',
    Suspicions.USERNAME_PROFANITY: 'Profanity in username',
    Suspicions.CORRUPT_PROMPT: 'Corrupt Prompt detected',
    Suspicions.TOO_MANY_JOBS_ABORTED: 'Too many jobs aborted in a short amount of time'
}
