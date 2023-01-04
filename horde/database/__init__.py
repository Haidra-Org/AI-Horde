from horde.threads import PrimaryTimedFunction
from horde.database.classes import Quorum
from horde.database.threads import get_quorum, store_prioritized_wp_queue, check_waiting_prompts, assign_monthly_kudos, store_worker_list, store_available_models, store_totals, prune_stats, store_patreon_members, increment_extra_priority, check_interrogations
from horde.horde_redis import horde_r

# Threads
quorum = Quorum(1, get_quorum)
wp_list_cacher = PrimaryTimedFunction(1, store_prioritized_wp_queue, quorum=quorum)
worker_cacher = PrimaryTimedFunction(25, store_worker_list, quorum=quorum)
model_cacher = PrimaryTimedFunction(5, store_available_models, quorum=quorum)
wp_cleaner = PrimaryTimedFunction(60, check_waiting_prompts, quorum=quorum)
interrogations_cleaner = PrimaryTimedFunction(60, check_interrogations, quorum=quorum)
monthly_kudos = PrimaryTimedFunction(86400, assign_monthly_kudos, quorum=quorum)
totals_store = PrimaryTimedFunction(60, store_totals, quorum=quorum)
prune_stats = PrimaryTimedFunction(60, prune_stats, quorum=quorum)
patreon_cacher = PrimaryTimedFunction(3600, store_patreon_members, quorum=quorum)
priority_increaser = PrimaryTimedFunction(10, increment_extra_priority, quorum=quorum)
store_totals()