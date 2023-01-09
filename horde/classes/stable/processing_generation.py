import threading
import requests
import os
import json

from horde.logger import logger
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.r2 import generate_procgen_download_url, upload_shared_metadata
from horde.flask import db


class ProcessingGenerationExtended(ProcessingGeneration):
    censored = db.Column(db.Boolean, default=False, nullable=False)

    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        generation = self.generation
        if generation == "R2":
            generation = generate_procgen_download_url(str(self.id), self.wp.shared)
        ret_dict = {
            "img": generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "id": self.id,
            "censored": self.censored,
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


    def set_generation(self, generation, things_per_sec, **kwargs):
        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        self.censored = kwargs.get("censored", False)
        db.session.commit()
        if self.wp.shared and not self.fake:
            self.upload_generation_metadata()
        # if not self.wp.r2: 
            # Should I put code here to convert b64 to PIL and upload or nevermind?
        return(kudos)
        
    def upload_generation_metadata(self):
        metadict = self.wp.get_share_metadata()
        metadict['seed'] = self.seed
        metadict['model'] = self.model
        metadict['censored'] = self.censored
        filename = f"{self.id}.json"
        json_object = json.dumps(metadict, indent=4)
        # Writing to sample.json
        with open(filename, "w") as f:
            f.write(json_object)
        upload_shared_metadata(filename)
        os.remove(filename)


        