from . import args

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

# The division that converts raw thing to thing
icon = {
    "stable": 1000000,
    "kobold": 1,
}

thing_name = thing_names[args.horde]
raw_thing_name = raw_thing_names[args.horde]
thing_divisor = thing_divisors[args.horde]
google_verification_string = google_verification_strings[args.horde]
img_url = f"https://raw.githubusercontent.com/db0/Stable-Horde/main/img_{args.horde}/"
horde_title = args.horde.capitalize()
if args.horde == "kobold":
    horde_title = "KoboldAI"
