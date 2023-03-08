import uuid
import bleach
import secrets
import hashlib
import os
import random
from datetime import datetime
import dateutil.relativedelta
from profanity_check  import predict
from better_profanity import profanity
from horde.logger import logger
from horde.flask import SQLITE_MODE

profanity.load_censor_words()

system_random_seed = random.SystemRandom()

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

    def __init__(self,amount,decimals = 1):
        self.digits = count_digits(amount)
        self.decimals = decimals
        if self.digits < 4:
            self.amount = amount
            self.prefix = ''
            self.char = ''
        elif self.digits < 7:
            self.amount = round(amount / 1000, self.decimals)
            self.prefix = 'kilo'
            self.char = 'K'
        elif self.digits < 10:
            self.amount = round(amount / 1000000, self.decimals)
            self.prefix = 'mega'
            self.char = 'M'
        elif self.digits < 13:
            self.amount = round(amount / 1000000000, self.decimals)
            self.prefix = 'giga'
            self.char = 'G'
        else:
            self.amount = round(amount / 1000000000000, self.decimals)
            self.prefix = 'tera'
            self.char = 'T'

def get_db_uuid():
    if SQLITE_MODE:
        return str(uuid.uuid4())
    else: 
        return uuid.uuid4()

def generate_client_id():
    return secrets.token_urlsafe(16)

def sanitize_string(text):
    santxt = bleach.clean(text).lstrip().rstrip()
    return santxt

def hash_api_key(unhashed_api_key):
    salt = os.getenv("secret_key", "s0m3s3cr3t") # Note default here, just so it can run without env file
    hashed_key = hashlib.sha256(salt.encode() + unhashed_api_key.encode()).hexdigest()
    # logger.warning([os.getenv("secret_key", "s0m3s3cr3t"), hashed_key,unhashed_api_key])
    return hashed_key

def get_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+20)

def get_interrogation_form_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+3)

def get_random_seed(start_point=0):
    '''Generated a random seed, using a random number unique per node'''
    return system_random_seed.randint(start_point, 2**32 - 1)

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
