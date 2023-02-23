import time
import threading

from horde.logger import logger
from horde import horde_instance_id

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
        if self.function:
            logger.init_ok(f"PrimaryTimedFunction for {self.function.__name__}()", status="Started")

    def run(self):
        while True:
            try:
                # Everything starts the thread, but only the primary does something with it.
                # This allows me to change the primary node on-the-fly
                if self.cancel:
                    break
                if self.quorum_thread and self.quorum_thread.quorum != horde_instance_id:
                    time.sleep(self.interval)
                    continue
                self.call_function()
                time.sleep(self.interval)
            except Exception as e:
                logger.error(f"Exception caught in PrimaryTimer for method {self.function.__name__}(). Avoiding! {e}")
                time.sleep(10)

    # Putting this in its own method, so I can extend it
    def call_function(self):
        self.function(*self.args, **self.kwargs)

    def stop(self):
        self.cancel = True
        logger.init_ok(f"PrimaryTimedFunction for {self.function.__name__}()", status="Stopped")
