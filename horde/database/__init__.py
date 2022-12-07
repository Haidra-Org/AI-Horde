from horde.database.classes import PrimaryTimedFunction, Quorum
from horde.database.threads import get_quorum, store_prioritized_wp_queue, check_waiting_prompts, assign_monthly_kudos, store_worker_list, store_available_models, store_totals, prune_stats

# Threads
quorum = Quorum(1, get_quorum)
wp_list_cacher = PrimaryTimedFunction(1, store_prioritized_wp_queue, quorum=quorum)
worker_cacher = PrimaryTimedFunction(25, store_worker_list, quorum=quorum)
model_cacher = PrimaryTimedFunction(2, store_available_models, quorum=quorum)
wp_cleaner = PrimaryTimedFunction(300, check_waiting_prompts, quorum=quorum)
monthly_kudos = PrimaryTimedFunction(86400, assign_monthly_kudos, quorum=quorum)
store_totals = PrimaryTimedFunction(60, store_totals, quorum=quorum)
prune_stats = PrimaryTimedFunction(60, prune_stats, quorum=quorum)