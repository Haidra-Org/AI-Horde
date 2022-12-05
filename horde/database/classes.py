import time
import uuid
import json
import threading
from datetime import datetime, timedelta
from horde.argparser import args

from horde.logger import logger
from horde.vars import thing_name,thing_divisor
from horde.horde_redis import horde_r
from horde import horde_instance_id

class FakeWPRow:
    def __init__(self,json_row):
        self.id = uuid.UUID(json_row["id"])
        self.things = json_row["things"]
        self.n = json_row["n"]
        self.extra_priority = json_row["extra_priority"]
        self.created = datetime.strptime(json_row["created"],"%Y-%m-%d %H:%M:%S")

class PrimaryTimedFunction:
    def __init__(self, interval, function, args=None, kwargs=None, quorum=None):
        self.interval = interval
        self.function = function
        self.cancel = False
        self.args = args if args is not None else []
        self.kwargs = kwargs if kwargs is not None else {}
        self.quorum_thread = quorum
        self.thread = threading.Thread(target=self.run, args=())
        self.thread.daemon = True
        self.thread.start()
        logger.init_ok(f"PrimaryTimedFunction for {self.function.__name__}()", status="Started")

    def run(self):
        while True:
            # Everything starts the thread, but only the primary does something with it.
            # This allows me to change the primary node on-the-fly
            if self.cancel:
                break
            if self.quorum_thread:
                logger.debug(self.quorum_thread.quorum)
            if self.quorum_thread and self.quorum_thread.quorum != horde_instance_id:
                time.sleep(self.interval)
                continue
            try:
                self.call_function()
            except Exception as e:
                logger.error(f"Exception caught in PrimaryTimer for method {self.function.__name__}(). Avoiding! {e}")
            time.sleep(self.interval)

    # Putting this in its own method, so I can extend it
    def call_function(self):
        self.function(*self.args, **self.kwargs)

    def stop(self):
        self.cancel = True
        logger.init_ok(f"PrimaryTimedFunction for {self.function.__name__}()", status="Stopped")

class Quorum(PrimaryTimedFunction):
    quorum = None

    def call_function(self):
        self.quorum = self.function(*self.args, **self.kwargs)

    def is_primary(self):
        return self.quorum == horde_instance_id