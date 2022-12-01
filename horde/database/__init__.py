from horde.database.threads import store_prioritized_wp_queue, check_waiting_prompts, assign_monthly_kudos, store_worker_list
from horde.database.classes import PrimaryTimedFunction

# Threads
wp_list_cacher = PrimaryTimedFunction(1, store_prioritized_wp_queue)
worker_cacher = PrimaryTimedFunction(25, store_worker_list)
wp_cleaner = PrimaryTimedFunction(60, check_waiting_prompts)
monthly_kudos = PrimaryTimedFunction(86400, assign_monthly_kudos)