import time
import uuid
import json
import threading
from datetime import datetime, timedelta
from horde.argparser import args

from horde.logger import logger
from horde.vars import thing_name,thing_divisor

class FakeWPRow:
    def __init__(self,json_row):
        self.id = uuid.UUID(json_row["id"])
        self.things = json_row["things"]
        self.n = json_row["n"]
        self.extra_priority = json_row["extra_priority"]
        self.created = datetime.strptime(json_row["created"],"%Y-%m-%d %H:%M:%S")

class PrimaryTimedFunction:
    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.cancel = False
        self.args = args if args is not None else []
        self.kwargs = kwargs if kwargs is not None else {}
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
            if not args.primary:
                time.sleep(self.interval)
                continue
            try:
                self.function(*self.args, **self.kwargs)
            except Exception as e:
                logger.error(f"Exception caught in PrimaryTimer for method {self.function.__name__}(). Avoiding! {e}")
            time.sleep(self.interval)
    
    def stop(self):
        self.cancel = True
        logger.init_ok(f"PrimaryTimedFunction for {self.function.__name__}()", status="Stopped")
