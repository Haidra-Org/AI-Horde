# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import math
import os
import time
from typing import TYPE_CHECKING

import logfire
from sqlalchemy.orm import Mapped, relationship

from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.stable.genstats import record_image_statistic
from horde.flask import db
from horde.image import convert_b64_to_pil, convert_pil_to_b64
from horde.logger import logger
from horde.metrics import (
    submit_genstats_record_duration,
    submit_server_upload_duration,
    submit_state_handling_duration,
)
from horde.model_reference import model_reference
from horde.r2 import (
    check_shared_image,
    download_procgen_image,
    generate_procgen_download_url,
    upload_generated_image,
    upload_shared_generated_image,
    upload_shared_metadata,
)

if TYPE_CHECKING:
    from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
    from horde.classes.stable.worker import ImageWorker

# Each requested LoRA may have to be fetched before the job can start. The lease assumes the largest
# permitted file arriving over a modest connection, since a cache miss on every LoRA is a legitimate
# case rather than a fault. Kept separate from the pixel-work term because download time does not
# scale with resolution or sampler.
LORA_MAX_SIZE_MB = 400
LORA_ASSUMED_DOWNLOAD_MBPS = 30
LORA_DOWNLOAD_SECONDS = LORA_MAX_SIZE_MB * 8 / LORA_ASSUMED_DOWNLOAD_MBPS


class ImageProcessingGeneration(ProcessingGeneration):
    __mapper_args__ = {
        "polymorphic_identity": "image",
    }
    wp: Mapped[ImageWaitingPrompt] = relationship("ImageWaitingPrompt", back_populates="processing_gens")
    worker: Mapped[ImageWorker] = relationship("ImageWorker", back_populates="processing_gens")

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
        if model_reference.get_model_baseline(self.model) in ["flux_1", "z_image_turbo"]:
            # Flux and Qwen is double the size of SDXL and much slower, so it gives double the rewards from it.
            return self.wp.kudos * 8
        if model_reference.get_model_baseline(self.model) in ["qwen_image"]:
            # Qwen is even larger than flux.
            return self.wp.kudos * 12
        return self.wp.kudos

    def log_aborted_generation(self):
        record_image_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} of wp {str(self.wp_id)} "
            f"({self.wp.width}x{self.wp.height}x{self.wp.params['steps']}@{self.wp.params['sampler_name']})"
            f" from by worker: {self.worker.name} ({self.worker.id})",
        )

    def set_generation(self, generation, things_per_sec, **kwargs):
        with logfire.span("horde.stable.set_generation", procgen_id=str(self.id), wp_id=str(self.wp_id)):
            return self._set_generation_inner(generation, things_per_sec, **kwargs)

    def _set_generation_inner(self, generation, things_per_sec, **kwargs):
        state_t0 = time.monotonic()
        state = kwargs.get("state", "ok")
        censored = False
        gen_metadata = kwargs.get("gen_metadata") if kwargs.get("gen_metadata") is not None else []
        for metadata in gen_metadata:
            if metadata.get("type") != "censorship":
                # this metadata isnt about censorship
                continue
            if metadata.get("value") == "csam":
                censored = "csam"
            else:
                censored = "nsfw"
        if censored is not False:
            self.censored = True
            db.session.commit()
            # Disabled prompt gathering for now
            # if censored == "csam":
            #     prompt_dict = {
            #         "prompt": self.wp.prompt,
            #         "user": self.wp.user.get_unique_alias(),
            #         "type": "clip",
            #     }
            #     upload_prompt(prompt_dict)
        elif state == "faulted":
            if self.wp.count_finished_jobs() < self.wp.jobs:
                self.wp.n += 1
            self.abort()
        submit_state_handling_duration.record(time.monotonic() - state_t0, {"horde.gentype": "image"})
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
                upload_t0 = time.monotonic()
                upload_method(image, filename)
                submit_server_upload_duration.record(
                    time.monotonic() - upload_t0,
                    {"horde.gentype": "image", "horde.upload": "image"},
                )
                # This signifies to send the download URL
                generation = "R2"
        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        genstats_t0 = time.monotonic()
        record_image_statistic(self)
        submit_genstats_record_duration.record(time.monotonic() - genstats_t0, {"horde.gentype": "image"})
        if self.wp.shared and not self.fake and generation == "R2":
            metadata_t0 = time.monotonic()
            self.upload_generation_metadata()
            submit_server_upload_duration.record(
                time.monotonic() - metadata_t0,
                {"horde.gentype": "image", "horde.upload": "metadata"},
            )
        if censored == "csam":
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

    def set_job_ttl(self):
        """Persist the completion deadline offered to the worker for this assignment.

        This is a lease from pop to submission, not a prediction of uninterrupted inference time. A
        worker may pop ahead so that model and LoRA I/O overlaps its current generation; the lease must
        therefore cover some local queue residence as well as this job's inference. At 512 square the
        scalable term allows one work unit every two seconds, or 0.131072 megapixel-work units per
        second. That is deliberately more conservative than the 0.5-MPS normal-speed worker threshold:
        it gives such a worker about 3.8 times its isolated compute time, or about 1.9 times the combined
        compute time when one equally expensive job is already ahead of it.

        The 150-second floor protects short jobs, where model and asset preparation dominate and the
        proportional allowance would be least useful. Workload and worker multipliers retain extra
        room for known slow paths. They intentionally compound the whole lease, including its fixed
        allowance; changing that ordering is a policy change rather than an algebraic cleanup.

        Requested LoRAs add a fixed download allowance per file on top of the floored lease. Up to five
        LoRAs of the maximum permitted size can exceed the floor several times over on an uncached
        worker, so no proportional or fixed term covers them; the addition is applied after the floor
        so it never disappears into it.
        """
        ttl_multiplier = (self.wp.width * self.wp.height) / (512 * 512)
        sampler_work = self.wp.get_estimated_sampler_work().work_units.value
        ttl = 30 + (sampler_work * 2 * ttl_multiplier)

        # Sampler work does not represent the additional conditioned model path, so ControlNet keeps
        # its separate allowance rather than pretending those costs are sampler trajectory.
        if self.wp.gen_payload.get("control_type"):
            ttl *= 2

        # Pixel-work is deliberately model-neutral. Larger architectures retain a separate allowance,
        # applied only when this procgen was actually assigned that model; using every requested model
        # would over-budget ordinary assignments from a multi-model request.
        if model_reference.get_model_baseline(self.model) in ["flux_1", "qwen_image", "z_image_turbo"]:
            ttl *= 3

        ttl = max(ttl, 150)
        ttl += len(self.wp.params.get("loras", [])) * LORA_DOWNLOAD_SECONDS
        # The extra-slow opt-in describes the worker, not the payload. Applying it after the floor keeps
        # short leases viable on hardware whose model and asset preparation is itself unusually slow.
        if self.worker.extra_slow_worker is True:
            ttl *= 3

        # The wire contract and database column are whole seconds. Round upward so fractional pixel
        # ratios can never shorten the promised completion window.
        self.job_ttl = math.ceil(ttl)
        db.session.commit()
