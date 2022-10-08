import requests, os
from . import logger, args
from .redis_ctrl import is_redis_up, get_ipaddr_db

r = None
if is_redis_up():
	r = get_ipaddr_db()

# Returns False if the IP is not false
# Else return true
# This function is a bit obscured with env vars to prevent defeat
def is_ip_safe(ipaddr):
	if args.allow_all_ips:
		return(True)
	# If we don't have the cache up, it's always OK
	if not r:
		return(True)
	safety_threshold=0.99
	timeout=2.00
	result = requests.get(os.getenv("IP_CHECKER").format(ipaddr = ipaddr), timeout=timeout)
	probability = float(result.content)
	if not result.ok:
		if probability == int(os.getenv("IP_CHECKER_LC")):
			is_safe = True
		else:
			is_safe = False
			logger.error(f"An error occured while validating IP. Return Code: {result.text}")
	else:
		is_safe = probability < safety_threshold
	logger.debug(f"IP {ipaddr} has a probability of {probability}. Safe = {is_safe}")
	return(is_safe)
