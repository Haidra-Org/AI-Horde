import horde.database.threads as threads
from horde.argparser import args
from horde.database.classes import Quorum
from horde.logger import logger
from horde.threads import PrimaryTimedFunction

# Threads
quorum = Quorum(1, threads.get_quorum)
wp_list_cacher = PrimaryTimedFunction(1, threads.store_prioritized_wp_queue, quorum=quorum)
worker_cacher = PrimaryTimedFunction(30, threads.store_worker_list, quorum=quorum)
model_cacher = PrimaryTimedFunction(10, threads.store_available_models, quorum=quorum)
if not args.check_prompts:
    wp_cleaner = PrimaryTimedFunction(60, threads.check_waiting_prompts, quorum=quorum)
interrogations_cleaner = PrimaryTimedFunction(60, threads.check_interrogations, quorum=quorum)
patreon_cacher = PrimaryTimedFunction(3600, threads.store_patreon_members, quorum=quorum)
monthly_kudos = PrimaryTimedFunction(3600, threads.assign_monthly_kudos, quorum=quorum)
totals_store = PrimaryTimedFunction(60, threads.store_totals, quorum=quorum)
prune_stats = PrimaryTimedFunction(60, threads.prune_stats, quorum=quorum)
priority_increaser = PrimaryTimedFunction(10, threads.increment_extra_priority, quorum=quorum)
compiled_filter_cacher = PrimaryTimedFunction(10, threads.store_compiled_filter_regex, quorum=quorum)
regex_replacements_cacher = PrimaryTimedFunction(10, threads.store_compiled_filter_regex_replacements, quorum=quorum)
known_image_models_cacher = PrimaryTimedFunction(300, threads.store_known_image_models, quorum=quorum)

if args.reload_all_caches:
    logger.info("store_prioritized_wp_queue()")
    threads.store_prioritized_wp_queue()
    logger.info("store_worker_list()")
    threads.store_worker_list()
    logger.info("store_totals()")
    threads.store_totals()
    logger.info("store_patreon_members()")
    threads.store_patreon_members()
    logger.info("store_compiled_filter_regex()")
    threads.store_compiled_filter_regex()
    logger.info("store_compiled_filter_regex_replacements()")
    threads.store_compiled_filter_regex_replacements()
    logger.info("store_available_models()")
    threads.store_available_models()
    logger.info("store_known_image_models()")
    threads.store_known_image_models()


if args.check_prompts:
    threads.check_waiting_prompts()
    import sys

    sys.exit()

if args.new_patreons:
    threads.store_patreon_members()
    threads.assign_monthly_kudos()
    import sys

    sys.exit()

# # Test

# logger.info("store_compiled_filter_regex_replacements()")
# threads.increment_extra_priority()
# import sys
# sys.exit()
