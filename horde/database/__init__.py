from horde.threads import PrimaryTimedFunction
from horde.database.classes import Quorum
import horde.database.threads as threads
from horde.horde_redis import horde_r

# Threads
quorum = Quorum(1, threads.get_quorum)
wp_list_cacher = PrimaryTimedFunction(1, threads.store_prioritized_wp_queue, quorum=quorum)
worker_cacher = PrimaryTimedFunction(25, threads.store_worker_list, quorum=quorum)
model_cacher = PrimaryTimedFunction(10, threads.store_available_models, quorum=quorum)
wp_cleaner = PrimaryTimedFunction(60, threads.check_waiting_prompts, quorum=quorum)
interrogations_cleaner = PrimaryTimedFunction(60, threads.check_interrogations, quorum=quorum)
monthly_kudos = PrimaryTimedFunction(40000, threads.assign_monthly_kudos, quorum=quorum)
totals_store = PrimaryTimedFunction(60, threads.store_totals, quorum=quorum)
prune_stats = PrimaryTimedFunction(60, threads.prune_stats, quorum=quorum)
patreon_cacher = PrimaryTimedFunction(3600, threads.store_patreon_members, quorum=quorum)
priority_increaser = PrimaryTimedFunction(10, threads.increment_extra_priority, quorum=quorum)
compiled_filter_cacher = PrimaryTimedFunction(10, threads.store_compiled_filter_regex, quorum=quorum)
