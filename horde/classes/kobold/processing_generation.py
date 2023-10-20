import math

from horde.logger import logger
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.kobold.genstats import record_text_statistic
from horde.flask import db
from horde.model_reference import model_reference
from horde import vars as hv
from horde.suspicions import Suspicions

class TextProcessingGeneration(ProcessingGeneration):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }    
    wp = db.relationship("TextWaitingPrompt", back_populates="processing_gens")
    worker = db.relationship("TextWorker", back_populates="processing_gens")

    def get_details(self):
        '''Returns a dictionary with details about this processing generation'''
        ret_dict = {
            "text": self.generation,
            "seed": self.seed,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "id": self.id,
        }
        return ret_dict

    def get_gen_kudos(self):
        # This formula creates an exponential increase on the kudos consumption, based on the context requested
        # 1024 context is considered the base.
        # The reason is that higher context has exponential VRAM requirements
        context_multiplier = 2.5 ** (math.log2(self.wp.max_context_length / 1024))
        # Prevent shenanigans
        if context_multiplier > 30:
            context_multiplier = 30
        if context_multiplier < 0.1:
            context_multiplier = 0.1
        # If a worker serves an unknown model, they only get 1 kudos, unless they're trusted in which case they get 20
        if not model_reference.is_known_text_model(self.model):
            if not self.worker.user.trusted:
                return context_multiplier
            # Trusted users with an unknown model are considered as running a 2.7B model
            return self.get_things_count() * 0.027 * context_multiplier
        # This is the approximate reward for generating with a 2.7 model at 4bit
        kudos = self.get_things_count() * model_reference.get_text_model_multiplier(self.model) / 100
        return round(kudos * context_multiplier, 2)


    def log_aborted_generation(self):
        record_text_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} of wp {str(self.wp_id)} "
            f"(for {self.get_things_count()} tokens and {self.wp.max_context_length} content length) "
            f" from by worker: {self.worker.name} ({self.worker.id})"
        )


    def set_generation(self, generation, things_per_sec, **kwargs):
        # We don't check the state in the super() function as image gen sets it early here
        # as well, so it can abort before doing R2 operations
        state = kwargs.get("state", 'ok')
        if state == "faulted":
            self.wp.n += 1
            self.abort()
        elif state == "censored":
            self.censored = True
            db.session.commit()
        kudos = super().set_generation(generation, things_per_sec, **kwargs)
        record_text_statistic(self)
        return(kudos)
    
    def get_things_count(self, generation = None):
        if generation is None:
            if self.generation is None:
                return 0
            generation = self.generation
        quick_token_count = math.ceil(len(generation)/4)
        if quick_token_count < 20:
            quick_token_count = 20
        if self.wp.things > quick_token_count:
            # logger.debug([self.wp.things,quick_token_count])
            return quick_token_count
        return self.wp.things

    def record(self, things_per_sec, kudos):
        # Extended function to try and catch workers using unreasonable
        # speeds at higher params
        # This only affects untrusted workers running known models
        super().record(things_per_sec, kudos)
        if not model_reference.is_known_text_model(self.model):
            return
        if self.worker.user.trusted:
            return
        param_multiplier = model_reference.get_text_model_multiplier(self.model)
        unreasonable_speed = hv.suspicion_thresholds['text']
        max_speed_per_multiplier = {
            70: 12,
            40: 22,
            20: 35,
            13: 50,
            7: 70,
        }
        for params_count in max_speed_per_multiplier:
            if param_multiplier >= params_count:
                unreasonable_speed = max_speed_per_multiplier[params_count]
                break
        if things_per_sec > unreasonable_speed:
            self.worker.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[things_per_sec])