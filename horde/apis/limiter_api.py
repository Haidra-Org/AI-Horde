from flask import request
from horde.consts import WHITELISTED_SERVICE_IPS
from horde.utils import hash_api_key

# Used to for the flask limiter, to limit requests per url paths
def get_request_path():
    # logger.info(dir(request))
    return f"{request.remote_addr}@{request.method}@{request.path}"


def get_request_90min_limit_per_ip():
    if request.remote_addr in WHITELISTED_SERVICE_IPS:
        return "300/minute"
    return "90/minute"


def get_request_90hour_limit_per_ip():
    if request.remote_addr in WHITELISTED_SERVICE_IPS:
        return "600/hour"
    return "90/hour"


def get_request_2sec_limit_per_ip():
    if request.remote_addr in WHITELISTED_SERVICE_IPS:
        return "10/second"
    return "2/second"


def get_request_api_key():
    apikey = hash_api_key(request.headers.get("apikey", "0000000000"))
    return f"{apikey}@{request.method}@{request.path}"


def get_request_limit_per_apikey():
    apikey = request.headers.get("apikey", "0000000000")
    if apikey == "0000000000":
        return "60/second"
    return "2/second"
