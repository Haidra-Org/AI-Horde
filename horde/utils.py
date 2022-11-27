import uuid
from profanity_check  import predict
from better_profanity import profanity
profanity.load_censor_words()

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
    return str(uuid.uuid4())