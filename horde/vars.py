from horde.argparser import args

thing_names = {
    "stable": "megapixelsteps",
    "kobold": "tokens",
}

raw_thing_names = {
    "stable": "pixelsteps",
    "kobold": "tokens",
}

# The division that converts raw thing to thing
thing_divisors = {
    "stable": 1000000,
    "kobold": 1,
}

google_verification_strings = {
    "stable": "pmLKyCEPKM5csKT9mW1ZbGLu2TX_wD0S5FCxWlmg_iI",
    "kobold": "5imNnbyz39-i9j6dbAeS0o0ZRIfzpznY9FBa_kMZns0",
}

suspicion_thresholds = {
    "stable": 20,
    "kobold": 100,
}
icon = {
    "stable": 1000000,
    "kobold": 1,
}

thing_name = thing_names["stable"]
raw_thing_name = raw_thing_names["stable"]
text_thing_name = thing_names["kobold"]
thing_divisor = thing_divisors["stable"]
text_thing_divisor = thing_divisors["kobold"]
things_per_sec_suspicion_threshold = suspicion_thresholds["stable"]
google_verification_string = google_verification_strings["stable"]
img_url = f"https://raw.githubusercontent.com/db0/Stable-Horde/main/img_stable/"
horde_title = "AI Horde"
horde_url = "https://stablehorde.net"
