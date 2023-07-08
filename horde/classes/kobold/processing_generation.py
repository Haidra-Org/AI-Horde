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
        # We have pre-calculated them as they don't change per worker
        # If a worker serves an unknown model, they only get 1 kudos, unless they're trusted in which case they get 20
        if not model_reference.is_known_text_model(self.model):
            if not self.worker.user.trusted:
                return 1
            # Trusted users with an unknown model gain 1 per token requested, as we don't know their parameters amount
            return self.wp.max_length * 0.12
        return round(self.wp.max_length * model_reference.get_text_model_multiplier(self.model) / 21, 2)


    def log_aborted_generation(self):
        record_text_statistic(self)
        logger.info(
            f"Aborted Stale Generation {self.id} of wp {str(self.wp_id)} "
            f"(for {self.wp.max_length} tokens and {self.wp.max_context_length} content length) "
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