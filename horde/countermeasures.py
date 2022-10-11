import requests, os
from . import logger, args
from .redis_ctrl import is_redis_up, get_ipaddr_db
from datetime import timedelta

r = None
logger.init("IP Address Cache", status="Connecting")
if is_redis_up():
	r = get_ipaddr_db()
	logger.init_ok("IP Address Cache", status="Connected")
else:
	logger.init_err("IP Address Cache", status="Failed")



def set_safe(ipaddr, is_safe):
	'''Stores the safety of the IP in redis temporarily'''
	r.setex(ipaddr, timedelta(hours=6), int(is_safe))
	return(is_safe)

def get_safe(ipaddr):
	is_safe = r.get(ipaddr)
	if is_safe == None:
		return(is_safe)
	return(bool(is_safe))

def is_ip_safe(ipaddr):
	'''Returns False if the IP is not false
	Else return true
	This function is a bit obscured with env vars to prevent defeat
	'''
	if args.allow_all_ips:
		return(True)
	# If we don't have the cache up, it's always OK
	if not r:
		return(True)
	safety_threshold=0.99
	timeout=2.00
	is_safe = get_safe(ipaddr)
	if is_safe == None:
		result = requests.get(os.getenv("IP_CHECKER").format(ipaddr = ipaddr), timeout=timeout)
		if not result.ok:
			if result.status_code == 429:
				# If we exceeded the amount of requests we can do to the IP checker, we ask the client to try again later.
				return(None)
			else:
				probability = float(result.content)
			if probability == int(os.getenv("IP_CHECKER_LC")):
				is_safe = set_safe(ipaddr,True)
			else:
				is_safe = set_safe(ipaddr,False)
				logger.error(f"An error occured while validating IP. Return Code: {result.text}")
		else:
			probability = float(result.content)
			is_safe = set_safe(ipaddr, probability < safety_threshold)
		logger.debug(f"IP {ipaddr} has a probability of {probability}. Safe = {is_safe}")
	return(is_safe)