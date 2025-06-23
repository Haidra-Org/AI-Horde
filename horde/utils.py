# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import json
import os
import random
import secrets
import uuid
from datetime import datetime

import bleach
import dateutil.relativedelta
import regex as re
from better_profanity import profanity
from profanity_check import predict

from horde import exceptions as e
from horde.flask import SQLITE_MODE

profanity.load_censor_words()

random.seed(random.SystemRandom().randint(0, 2**32 - 1))


def is_profane(text):
    if profanity.contains_profanity(text):
        return True
    if predict([text]) == [1]:
        return True
    return False


def count_digits(number):
    digits = 1
    while number > 10:
        number = number / 10
        digits += 1
    return digits


class ConvertAmount:
    def __init__(self, amount, decimals=1):
        self.digits = count_digits(amount)
        self.decimals = decimals
        if self.digits < 4:
            self.amount = round(amount, self.decimals)
            self.prefix = ""
            self.char = ""
        elif self.digits < 7:
            self.amount = round(amount / 1000, self.decimals)
            self.prefix = "kilo"
            self.char = "K"
        elif self.digits < 10:
            self.amount = round(amount / 1_000_000, self.decimals)
            self.prefix = "mega"
            self.char = "M"
        elif self.digits < 13:
            self.amount = round(amount / 1_000_000_000, self.decimals)
            self.prefix = "giga"
            self.char = "G"
        elif self.digits < 16:
            self.amount = round(amount / 1_000_000_000_000, self.decimals)
            self.prefix = "tera"
            self.char = "T"
        else:
            self.amount = round(amount / 1_000_000_000_000_000, self.decimals)
            self.prefix = "peta"
            self.char = "P"


def get_db_uuid():
    if SQLITE_MODE:
        return str(uuid.uuid4())
    return uuid.uuid4()


def generate_client_id():
    return secrets.token_urlsafe(16)


def sanitize_string(text):
    return bleach.clean(text).lstrip().rstrip()


def generate_api_key():
    """Generates a random API key."""
    return secrets.token_urlsafe(16)


def hash_api_key(unhashed_api_key):
    salt = os.getenv("secret_key", "s0m3s3cr3t")  # Note default here, just so it can run without env file #noqa SIM112
    return hashlib.sha256(salt.encode() + unhashed_api_key.encode()).hexdigest()


def hash_dictionary(dictionary):
    # Convert the dictionary to a JSON string
    json_string = json.dumps(dictionary, sort_keys=True)
    # Create a hash object
    hash_object = hashlib.sha256(json_string.encode())
    # Get the hexadecimal representation of the hash
    return hash_object.hexdigest()


def get_message_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(hours=+12)


def get_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+20)


def get_extra_slow_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+60)


def get_interrogation_form_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+3)


def get_random_seed(start_point=0):
    """Generated a random seed, using a random number unique per node"""
    return random.randint(start_point, 2**32 - 1)


def count_parentheses(s):
    open_p = False
    count = 0
    for c in s:
        if c == "(":
            open_p = True
        elif c == ")" and open_p:
            open_p = False
            count += 1
    return count


def validate_regex(regex_string):
    try:
        re.compile(regex_string, re.IGNORECASE)
    except Exception:
        return False
    return True


def does_extra_text_reference_exist(extra_texts, reference):
    for et in extra_texts:
        if et["reference"] == reference:
            return True
    return False


def ensure_clean(string, key):
    if is_profane(string):
        raise e.BadRequest(f"{key} contains profanity")
    return sanitize_string(string)
