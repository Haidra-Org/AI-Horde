import math

from horde.logger import logger
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.kobold.genstats import record_text_statistic
from horde.flask import db
from horde.model_reference import model_reference


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
            # Trusted users with an unknown model gain 1 per token requested, as we don't know their parameters amount
            return self.get_things_count() * 0.12 * context_multiplier
        # This is the approximate reward for generating with a 2.7 model at 4bit
        kudos = self.get_things_count() * model_reference.get_text_model_multiplier(self.model) / 84
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
        quick_token_count = len(generation)/4
        if self.wp.things > quick_token_count:
            return quick_token_count
        return self.wp.things
