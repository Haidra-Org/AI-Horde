import threading
import requests
import os
import json

from horde.logger import logger
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.stable.genstats import record_image_statistic
from horde.r2 import generate_procgen_download_url, upload_shared_metadata, check_shared_image, upload_generated_image, upload_shared_generated_image
from horde.flask import db
from horde.image import convert_b64_to_pil


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
        record_image_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} "
            f"({self.wp.width}x{self.wp.height}x{self.wp.params['steps']}@{self.wp.params['sampler_name']})"
            f" from by worker: {self.worker.name} ({self.worker.id})"
        )


    def set_generation(self, generation, things_per_sec, **kwargs):
        if kwargs.get("censored", False):
            self.censored = True
        state = kwargs.get("state", 'ok')
        if state == 'censored':
            self.censored = True
            db.session.commit()
        elif state == "faulted":
            self.abort()
        if self.is_completed():
            return(0)
        # We return -1 to know to send a different error
        if self.is_faulted():
            return(-1)
        if self.wp.r2 and generation != "R2":
            logger.warning(f"Worker {self.worker.name} ({self.worker.id}) with bridge version {self.worker.bridge_version} uploaded an R2 request as b64. Converting...")
            if self.wp.shared:
                upload_method = upload_shared_generated_image
            else:
                upload_method = upload_generated_image
            filename = f"{self.id}.webp"
            image = convert_b64_to_pil(generation)
            if not image:
                logger.error("Could not convert b64 image from the worker to PIL to upload!")
            else:
                # FIXME: I would really like to avoid the unnecessary I/O here by uploading directly from RAM...
                image.save(filename)
                upload_method(filename)
                # This signifies to send the download URL
                generation = "R2"
                os.remove(filename)
        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        record_image_statistic(self)
        if self.wp.shared and not self.fake and generation == "R2":
            self.upload_generation_metadata()
        return(kudos)
        
    def upload_generation_metadata(self):
        if not check_shared_image(f"{self.id}.webp"):
            logger.warning(f"Avoiding json metadata upload because {self.id}.webp doesn't seem to exist.")
            return
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

    def adjust_user_kudos(self, kudos):
        if self.censored:
            return 0
        return kudos

        