# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from horde.database.classes import CachedPasskeys

_cached_passkeys: Optional["CachedPasskeys"] = None


def set_cached_passkeys(cached_passkeys: "CachedPasskeys"):
    """Sets the cached passkeys object. Called from start_background_threads() to initialize the global variable."""
    global _cached_passkeys
    _cached_passkeys = cached_passkeys


def get_cached_passkeys() -> "CachedPasskeys":
    """Returns the cached passkeys object. This is used in request_utils to check if a passkey is known."""
    if _cached_passkeys is None:
        raise Exception("CachedPasskeys not initialized yet")

    return _cached_passkeys


def start_background_threads():
    """Start all periodic background threads. Called from create_app() in non-test mode."""
    import horde.database.threads as threads
    from horde.argparser import args
    from horde.database.classes import CachedPasskeys, Quorum
    from horde.logger import logger
    from horde.threads import PrimaryTimedFunction

    quorum = Quorum(1, threads.get_quorum)
    PrimaryTimedFunction(1, threads.store_prioritized_wp_queue, quorum=quorum)
    PrimaryTimedFunction(30, threads.store_worker_list, quorum=quorum)
    PrimaryTimedFunction(10, threads.store_available_models, quorum=quorum)
    if not args.check_prompts:
        PrimaryTimedFunction(60, threads.check_waiting_prompts, quorum=quorum)
    PrimaryTimedFunction(60, threads.check_interrogations, quorum=quorum)
    PrimaryTimedFunction(3600, threads.assign_monthly_kudos, quorum=quorum)
    PrimaryTimedFunction(60, threads.store_totals, quorum=quorum)
    PrimaryTimedFunction(60, threads.prune_stats, quorum=quorum)
    PrimaryTimedFunction(10, threads.increment_extra_priority, quorum=quorum)
    PrimaryTimedFunction(10, threads.store_compiled_filter_regex, quorum=quorum)
    PrimaryTimedFunction(10, threads.store_compiled_filter_regex_replacements, quorum=quorum)
    PrimaryTimedFunction(300, threads.store_known_image_models, quorum=quorum)
    set_cached_passkeys(CachedPasskeys(5, threads.refresh_passkeys))

    if args.reload_all_caches:
        logger.info("store_prioritized_wp_queue()")
        threads.store_prioritized_wp_queue()
        logger.info("store_worker_list()")
        threads.store_worker_list()
        logger.info("store_totals()")
        threads.store_totals()
        logger.info("store_compiled_filter_regex()")
        threads.store_compiled_filter_regex()
        logger.info("store_compiled_filter_regex_replacements()")
        threads.store_compiled_filter_regex_replacements()
        logger.info("store_available_models()")
        threads.store_available_models()
        logger.info("store_known_image_models()")
        threads.store_known_image_models()
