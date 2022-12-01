from horde.database.functions import store_prioritized_wp_queue
from horde.database.classes import PrimaryTimedFunction

# Threads
wp_list_cacher = PrimaryTimedFunction(1, store_prioritized_wp_queue)