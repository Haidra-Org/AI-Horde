# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os

from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.stable.genstats import record_image_statistic
from horde.flask import db
from horde.image import convert_b64_to_pil, convert_pil_to_b64
from horde.logger import logger
from horde.model_reference import model_reference
from horde.r2 import (
    check_shared_image,
    download_procgen_image,
    generate_procgen_download_url,
    upload_generated_image,
    upload_prompt,
    upload_shared_generated_image,
    upload_shared_metadata,
)


class ImageProcessingGeneration(ProcessingGeneration):
    __mapper_args__ = {
        "polymorphic_identity": "image",
    }
    wp = db.relationship("ImageWaitingPrompt", back_populates="processing_gens")
    worker = db.relationship("ImageWorker", back_populates="processing_gens")

    def get_details(self):
        """Returns a dictionary with details about this processing generation"""
        generation = self.generation
        if generation == "R2":
            if not self.wp.r2:
                img = download_procgen_image(self.id, self.wp.shared)
                if img is None:
                    generation = "N/A"
                else:
                    generation = convert_pil_to_b64(img)
            else:
                generation = generate_procgen_download_url(str(self.id), self.wp.shared)
        ret_dict = {
            "img": generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "id": self.id,
            "censored": self.censored,
            "gen_metadata": self.gen_metadata if self.gen_metadata is not None else [],
        }
        return ret_dict

    def get_gen_kudos(self):
        # We have pre-calculated them as they don't change per worker
        if model_reference.get_model_baseline(self.model) in ["stable_diffusion_xl"]:
            if self.wp.params.get("workflow") == "qr_code":
                return self.wp.kudos * 4
            return self.wp.kudos * 2
        if model_reference.get_model_baseline(self.model) in ["stable_cascade"]:
            # Stable Cascade 2pass has almost a double cost as it generates extra at a low generation
            if self.wp.params.get("hires_fix", False):
                return self.wp.kudos * 7
            return self.wp.kudos * 4
        return self.wp.kudos

    def log_aborted_generation(self):
        record_image_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} of wp {str(self.wp_id)} "
            f"({self.wp.width}x{self.wp.height}x{self.wp.params['steps']}@{self.wp.params['sampler_name']})"
            f" from by worker: {self.worker.name} ({self.worker.id})",
        )

    def set_generation(self, generation, things_per_sec, **kwargs):
        if kwargs.get("censored", False):
            self.censored = True
        state = kwargs.get("state", "ok")
        if state in ["censored", "csam"]:
            self.censored = True
            db.session.commit()
            if state == "csam":
                prompt_dict = {
                    "prompt": self.wp.prompt,
                    "user": self.wp.user.get_unique_alias(),
                    "type": "clip",
                }
                upload_prompt(prompt_dict)
        elif state == "faulted":
            self.wp.n += 1
            self.abort()
        if self.is_completed():
            return 0
        # We return -1 to know to send a different error
        if self.is_faulted():
            return -1
        if generation != "R2":
            logger.warning(
                f"Worker {self.worker.name} ({self.worker.id}) with bridge agent {self.worker.bridge_agent} returned a b64. Converting...",
            )
            if self.wp.shared:
                upload_method = upload_shared_generated_image
            else:
                upload_method = upload_generated_image
            filename = f"{self.id}.webp"
            image = convert_b64_to_pil(generation)
            if not image:
                logger.error("Could not convert b64 image from the worker to PIL to upload!")
            else:
                upload_method(image, filename)
                # This signifies to send the download URL
                generation = "R2"
        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        record_image_statistic(self)
        if self.wp.shared and not self.fake and generation == "R2":
            self.upload_generation_metadata()
        if state == "csam":
            self.wp.user.record_problem_job(
                procgen=self,
                ipaddr=self.wp.ipaddr,
                worker=self.worker,
                prompt=self.wp.prompt,
            )
        return kudos

    def upload_generation_metadata(self):
        if not check_shared_image(f"{self.id}.webp"):
            logger.warning(f"Avoiding json metadata upload because {self.id}.webp doesn't seem to exist.")
            return
        metadict = self.wp.get_share_metadata()
        metadict["seed"] = self.seed
        metadict["model"] = self.model
        metadict["censored"] = self.censored
        filename = f"{self.id}.json"
        json_object = json.dumps(metadict, indent=4)
        # Writing to sample.json
        with open(filename, "w") as f:
            f.write(json_object)
        upload_shared_metadata(filename)
        os.remove(filename)
