from horde.logger import logger
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.r2 import generate_procgen_download_url


class ProcessingGenerationExtended(ProcessingGeneration):

    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        generation = self.generation
        if generation == "R2":
            generation = generate_procgen_download_url(str(self.id))
        ret_dict = {
            "img": generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
        }
        return ret_dict

    def get_gen_kudos(self):
        # We have pre-calculated them as they don't change per worker
        return self.wp.kudos

    def log_aborted_generation(self):
        logger.info(
            f"Aborted Stale Generation {self.id} "
            f"({self.wp.width}x{self.wp.height}x{self.wp.params['steps']}@{self.wp.params['sampler_name']})"
            f" from by worker: {self.worker.name} ({self.worker.id})"
        )

