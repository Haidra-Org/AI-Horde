import os
from uuid import uuid4

thing_names = {
    "image": "megapixelsteps",
    "text": "tokens",
}

raw_thing_names = {
    "image": "pixelsteps",
    "text": "tokens",
}

# The division that converts raw thing to thing
thing_divisors = {
    "image": 1000000,
    "text": 1,
    "interrogation": 1,
}

suspicion_thresholds = {
    "image": 20,
    "text": 150,
}
icon = {
    "image": 1000000,
    "text": 1,
}

thing_name = thing_names["image"]
raw_thing_name = raw_thing_names["image"]
text_thing_name = thing_names["text"]
thing_divisor = thing_divisors["image"]
text_thing_divisor = thing_divisors["text"]
things_per_sec_suspicion_threshold = suspicion_thresholds["image"]
google_verification_string = os.getenv("GOOGLE_VERIFICATION_STRING", "pmLKyCEPKM5csKT9mW1ZbGLu2TX_wD0S5FCxWlmg_iI")
img_url = os.getenv("HORDE_LOGO", "https://raw.githubusercontent.com/db0/Stable-Horde/main/img_stable/0.jpg")
horde_title = os.getenv("HORDE_TITLE", "AI Horde")
horde_noun = os.getenv("HORDE_noun", "horde")
horde_url = os.getenv("HORDE_URL", "https://aihorde.net")
horde_contact_email = os.getenv("HORDE_EMAIL", "aihorde@dbzer0.com")
horde_instance_id = str(uuid4())
