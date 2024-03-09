import math

from sqlalchemy.sql import expression

from horde import vars as hv
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.flask import db
from horde.logger import logger
from horde.model_reference import model_reference


class TextWaitingPrompt(WaitingPrompt):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }
    max_length = db.Column(
        db.Integer,
        default=80,
        nullable=False,
        index=True,
        server_default=expression.literal(80),
    )
    max_context_length = db.Column(
        db.Integer,
        default=1024,
        nullable=False,
        index=True,
        server_default=expression.literal(1024),
    )
    softprompt = db.Column(db.String(255), default=None, nullable=True)
    processing_gens = db.relationship(
        "TextProcessingGeneration",
        back_populates="wp",
        passive_deletes=True,
        cascade="all, delete-orphan",
    )

    def extract_params(self, **kwargs):
        self.n = self.params.pop("n", 1)
        self.jobs = self.n
        self.max_length = self.params.get("max_length", 80)
        self.max_context_length = self.params.get("max_context_length", 1024)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.max_length
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.max_length * self.n / hv.thing_divisors["text"], 2)
        self.softprompt = kwargs.get("softprompt")
        self.prepare_job_payload(self.params)

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict=None):
        """Prepares the default job payload. This might be further adjusted per job in get_job_payload()"""
        if not initial_dict:
            initial_dict = {}
        self.gen_payload = initial_dict.copy()
        self.gen_payload["prompt"] = self.prompt
        self.gen_payload["n"] = 1
        db.session.commit()

    def activate(self, downgrade_wp_priority=False, source_image=None, source_mask=None):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate(downgrade_wp_priority)
        proxied_account = ""
        if self.proxied_account:
            proxied_account = f":{self.proxied_account}"
        logger.info(
            f"New text2text prompt with ID {self.id} by {self.user.get_unique_alias()}{proxied_account}: "
            f"max_length:{self.max_length} * n:{self.n} == {self.total_usage} Total Tokens",
        )

    def calculate_extra_kudos_burn(self, kudos):
        # This represents the cost of using the resources of the horde
        return kudos + 1

    def log_faulted_prompt(self):
        source_processing = "txt2img"
        if self.source_image:
            source_processing = self.source_processing
        logger.warning(
            f"Faulting waiting {source_processing} prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs",
        )

    def get_status(self, **kwargs):
        ret_dict = super().get_status(**kwargs)
        return ret_dict

    def record_usage(self, raw_things, kudos, usage_type="text", avoid_burn=False):
        """I need to extend this to point it to record_text_usage()"""
        super().record_usage(raw_things, kudos, usage_type)

    def require_upfront_kudos(self, counted_totals, total_threads):
        """Returns True if this wp requires that the user already has the required kudos to fulfil it
        else returns False
        """
        queue = counted_totals["queued_text_requests"]
        max_tokens = 512 + (total_threads * 5) - round(queue * 0.9)
        # logger.debug([queue,max_tokens])
        if not self.slow_workers:
            return (True, max_tokens)
        if max_tokens < 256:
            max_tokens = 256
        if max_tokens > 512:
            max_tokens = 512
        if self.max_length > max_tokens:
            return (True, max_tokens)
        return (False, max_tokens)

    def downgrade(self, max_tokens):
        """Ensures this WP requirements are not exceeding upfront kudos requirements"""
        self.slow_workers = True
        while self.max_length > max_tokens:
            self.max_length = max_tokens
            self.params["max_length"] = self.max_length
            self.gen_payload["max_length"] = self.max_length
            logger.info(f"Text WP {self.id} was downgraded to {self.max_length} tokens")
        db.session.commit()

    def calculate_kudos(self):
        # Slimmed down version of procgen.get_gen_kudos()
        # As we don't know the worker's trusted status.
        # It exists here in order to allow us to calculate dry_runs
        context_multiplier = 1.2 + (2.2 ** (math.log2(self.max_context_length / 1024)))
        # Prevent shenanigans
        if context_multiplier > 30:
            context_multiplier = 30
        if context_multiplier < 0.1:
            context_multiplier = 0.1
        if len(self.models) > 0:
            model_name = self.models[0].model
        else:
            # For empty model lists, we assume they're going to run into a 13B model
            return round(self.max_length * 13 * context_multiplier / 100, 2)
        if not model_reference.is_known_text_model(model_name):
            return self.wp.max_length * (2.7 / 100) * context_multiplier
        model_multiplier = model_reference.get_text_model_multiplier(model_name)
        parameter_bonus = (max(model_multiplier, 13) / 13) ** 0.20
        self.kudos = round(
            self.max_length * parameter_bonus * model_multiplier * context_multiplier / 100,
            2,
        )
        return self.kudos
