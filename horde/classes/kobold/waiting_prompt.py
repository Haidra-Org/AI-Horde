import random
from sqlalchemy.sql import expression
from horde.logger import logger
from horde import vars as hv
from horde.flask import db
from horde.classes.base.waiting_prompt import WaitingPrompt
from horde.r2 import generate_procgen_upload_url, download_source_image, download_source_mask
from horde.image import convert_pil_to_b64
from horde.bridge_reference import check_bridge_capability

class TextWaitingPrompt(WaitingPrompt):
    __mapper_args__ = {
        "polymorphic_identity": "text",
    }    
    max_length = db.Column(db.Integer, default=80, nullable=False, index=True, server_default=expression.literal(80))
    max_context_length = db.Column(db.Integer, default=1024, nullable=False, index=True, server_default=expression.literal(1024))
    softprompt = db.Column(db.String(255), default=None, nullable=True)
    processing_gens = db.relationship("TextProcessingGeneration", back_populates="wp", passive_deletes=True, cascade="all, delete-orphan")


    def extract_params(self, **kwargs):
        self.n = self.params.pop('n', 1)
        self.jobs = self.n 
        self.max_length = self.params.get("max_length", 80)
        self.max_context_length = self.params.get("max_context_length", 1024)
        # To avoid unnecessary calculations, we do it once here.
        self.things = self.max_length
        # The total amount of to pixelsteps requested.
        self.total_usage = round(self.max_length * self.n / hv.thing_divisors["text"],2)
        self.softprompt = kwargs.get("softprompt")
        self.prepare_job_payload(self.params)

    @logger.catch(reraise=True)
    def prepare_job_payload(self, initial_dict = None):
        '''Prepares the default job payload. This might be further adjusted per job in get_job_payload()'''
        if not initial_dict: initial_dict = {}
        self.gen_payload = initial_dict.copy()
        self.gen_payload["prompt"] = self.prompt
        self.gen_payload["n"] = 1
        db.session.commit()

    def activate(self, source_image = None, source_mask = None):
        # We separate the activation from __init__ as often we want to check if there's a valid worker for it
        # Before we add it to the queue
        super().activate()
        logger.info(f"New text2text prompt with ID {self.id} by {self.user.get_unique_alias()}: token:{self.max_length} * n:{self.n} == {self.total_usage} Total Tokens")

    def record_text_usage(self, raw_things, kudos):
        # This represents the cost of using the resources of the horde
        horde_tax = 1
        kudos += horde_tax
        super().record_text_usage(raw_things, kudos)

    def log_faulted_prompt(self):
        source_processing = 'txt2img'
        if self.source_image:
            source_processing = self.source_processing
        logger.warning(f"Faulting waiting {source_processing} prompt {self.id} with payload '{self.gen_payload}' due to too many faulted jobs")

    def get_status(self, **kwargs):
        ret_dict = super().get_status(**kwargs)
        return ret_dict

    def record_usage(self, raw_things, kudos, usage_type = "text"):
        '''I need to extend this to point it to record_text_usage()
        '''
        super().record_usage(raw_things, kudos, usage_type)

    def require_upfront_kudos(self, counted_totals):
        '''Returns True if this wp requires that the user already has the required kudos to fulfil it
        else returns False
        '''
        queue = counted_totals["queued_text_requests"]
        max_tokens = 511 - round(queue * 0.9)
        if not self.slow_workers:
            return(True,max_tokens) 
        if max_tokens < 256:
            max_tokens = 256
        if max_tokens > 512:
            max_tokens = 512
        if self.max_length > max_tokens:
            return (True,max_tokens)
        return (False,max_tokens)