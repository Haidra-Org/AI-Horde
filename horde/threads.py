import time
import threading
import requests

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



class ModelReference(PrimaryTimedFunction):
    quorum = None
    reference = None
    stable_diffusion_names = set()
    controlnet_models = set()
    nsfw_models = set()

    def call_function(self):
        '''Retrieves to nataili model reference and stores in it a var'''
        # If it's running in SQLITE_MODE, it means it's a test and we never want to grab the quorum
        # We don't want to report on any random model name a client might request
        try:
            self.reference = requests.get("https://raw.githubusercontent.com/Sygil-Dev/nataili-model-reference/main/db.json", timeout=2).json()
            # logger.debug(self.reference)
            self.stable_diffusion_names = set()
            for model in self.reference:
                if self.reference[model].get("baseline") in {"stable diffusion 1","stable diffusion 2"}:
                    self.stable_diffusion_names.add(model)
                    if self.reference[model].get("nsfw"):
                        self.nsfw_models.add(model)
                    if self.reference[model].get("type") == "controlnet":
                        self.controlnet_models.add(model)

        except Exception:
            logger.error(f"Error when downloading known models list: {e}")

    def get_model_names(self):
        return set(reference.keys())


model_reference = ModelReference(3600, None)