import argparse

from horde.switch import Switch

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('-i', '--insecure', action="store_true", help="If set, will use http instead of https (useful for testing)")
arg_parser.add_argument('-v', '--verbosity', action='count', default=0, help="The default logging level is ERROR or higher. This value increases the amount of logging seen in your screen")
arg_parser.add_argument('-q', '--quiet', action='count', default=0, help="The default logging level is ERROR or higher. This value decreases the amount of logging seen in your screen")
arg_parser.add_argument('-c', '--convert_flag', action='store', default=None, required=False, type=str, help="A special flag to convert from previous DB entries to newer and exit")
arg_parser.add_argument('-p', '--port', action='store', default=7001, required=False, type=int, help="Provide a different port to start with")
arg_parser.add_argument('--horde', action='store', default='stable', required=True, type=str, help="Which Horde to Start. This customizes endpoints and methods")
arg_parser.add_argument('--allow_all_ips', action="store_true", help="If set, will consider all IPs safe")
arg_parser.add_argument('--quorum', action="store_true", help="If set, will forcefully grab the quorum")
arg_parser.add_argument('--reload_all_caches', action="store_true", help="If set, will forcefully reload all caches at startup")
arg_parser.add_argument('--check_prompts', action="store_true", help="If set, will cleanup all prompts and exit")
args = arg_parser.parse_args()

maintenance = Switch()
invite_only = Switch()
raid = Switch()
