import os
import requests

from horde.logger import logger
from horde.argparser import args
from horde.redis_ctrl import is_redis_up, get_ipaddr_db, get_ipaddr_suspicion_db, get_ipaddr_timeout_db
from datetime import timedelta
from horde.consts import WHITELISTED_SERVICE_IPS

ip_r = None
logger.init("IP Address Cache", status="Connecting")
if is_redis_up():
	ip_r = get_ipaddr_db()
	logger.init_ok("IP Address Cache", status="Connected")
else:
	logger.init_err("IP Address Cache", status="Failed")
ip_s_r = None
logger.init("IP Suspicion Cache", status="Connecting")
if is_redis_up():
	ip_s_r = get_ipaddr_suspicion_db()
	logger.init_ok("IP Suspicion Cache", status="Connected")
else:
	logger.init_err("IP Suspicion Cache", status="Failed")
ip_t_r = None
logger.init("IP Timeout Cache", status="Connecting")
if is_redis_up():
	ip_t_r = get_ipaddr_timeout_db()
	logger.init_ok("IP Timeout Cache", status="Connected")
else:
	logger.init_err("IP Timeout Cache", status="Failed")

test_timeout = 0


class CounterMeasures:
	@staticmethod
	def set_safe(ipaddr, is_safe):
		'''Stores the safety of the IP in redis temporarily'''
		ip_r.setex(ipaddr, timedelta(hours=48), int(is_safe))
		return is_safe

	@staticmethod
	def get_safe(ipaddr):
		is_safe = ip_r.get(ipaddr)
		if is_safe is None:
			return is_safe
		return bool(is_safe)

	@staticmethod
	def is_ip_safe(ipaddr):
		'''Returns False if the IP is not false
		Else return true
		This function is a bit obscured with env vars to prevent defeat
		'''
		if args.allow_all_ips or os.getenv("IP_CHECKER", "") == "":
			return True
		# If we don't have the cache up, it's always OK
		if not ip_r:
			return True
		safety_threshold=0.93
		timeout=2.00
		is_safe = CounterMeasures.get_safe(ipaddr)
		if is_safe is None:
			try:
				result = requests.get(os.getenv("IP_CHECKER").format(ipaddr = ipaddr), timeout=timeout)
			except Exception as err:
				logger.error(f"Exception when requesting info from checker")
				return None
			if not result.ok:
				if result.status_code == 429:
					# If we exceeded the amount of requests we can do to the IP checker, we ask the client to try again later.
					return None
				else:
					probability = float(result.content)
				if probability == int(os.getenv("IP_CHECKER_LC")):
					is_safe = CounterMeasures.set_safe(ipaddr,True)
				else:
					is_safe = CounterMeasures.set_safe(ipaddr,True) # True until I can improve my load
					logger.error(f"An error occured while validating IP. Return Code: {result.text}")
			else:
				probability = float(result.content)
				is_safe = CounterMeasures.set_safe(ipaddr, probability < safety_threshold)
			logger.debug(f"IP {ipaddr} has a probability of {probability}. Safe = {is_safe}")
		return is_safe

	@staticmethod
	def report_suspicion(ipaddr):
		'''Increases the suspicion of an IP in redis temporarily'''
		if not ip_s_r:
			global test_timeout
			test_timeout = test_timeout + test_timeout + 1
			timeout = test_timeout * 3
			logger.debug(f"Redis not available, so setting test_timeout to {test_timeout}")
			CounterMeasures.set_timeout(ipaddr,timeout)
			return test_timeout
		current_suspicion = ip_s_r.get(ipaddr)
		if current_suspicion is None:
			current_suspicion = 0
		current_suspicion = int(current_suspicion)
		ip_s_r.setex(ipaddr, timedelta(hours=24), current_suspicion + 1)
		# Fibonacci FTW!
		timeout = (current_suspicion + current_suspicion + 1) * 3
		if ipaddr in WHITELISTED_SERVICE_IPS and timeout > 300:
			timeout = 300
		CounterMeasures.set_timeout(ipaddr, timeout)
		return timeout

	@staticmethod
	def retrieve_suspicion(ipaddr):
		'''Checks the current suspicion of an IP address'''
		if not ip_s_r:
			return 0
		current_suspicion = ip_s_r.get(ipaddr)
		if current_suspicion is None:
			current_suspicion = 0
		return int(current_suspicion)

	@staticmethod
	def set_timeout(ipaddr, minutes):
		'''Puts the ip address into timeout'''
		if not ip_t_r:
			return
		ip_t_r.setex(ipaddr, timedelta(minutes=minutes), int(True))

	@staticmethod
	def retrieve_timeout(ipaddr):
		'''Checks if an IP address is still in timeout'''
		if not ip_t_r:
			return test_timeout*3*60
		has_timeout = ip_t_r.get(ipaddr)
		if not bool(has_timeout):
			return 0
		ttl = ip_t_r.ttl(ipaddr)
		return int(ttl)

	@staticmethod
	def delete_timeout(ipaddr):
		'''Deletes an IP address in timeout'''
		if not ip_t_r:
			return
		ip_t_r.delete(ipaddr)
		ip_s_r.delete(ipaddr)
