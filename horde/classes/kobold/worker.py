# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from datetime import timedelta

from sqlalchemy.dialects.postgresql import UUID

from horde import exceptions as e
from horde import horde_redis as hr
from horde.bridge_reference import (
    is_backed_validated,
)
from horde.classes.base.worker import Worker
from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.model_reference import model_reference
from horde.utils import sanitize_string

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731


class TextWorkerSoftprompts(db.Model):
    __tablename__ = "text_worker_softprompts"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("TextWorker", back_populates="softprompts")
    softprompt = db.Column(db.String(255))
    wtype = "text"


class TextWorker(Worker):
    __mapper_args__ = {
        "polymorphic_identity": "text_worker",
    }
    # TODO: Switch to max_power
    max_length = db.Column(db.Integer, default=80, nullable=False)
    max_context_length = db.Column(db.Integer, default=1024, nullable=False)

    softprompts = db.relationship("TextWorkerSoftprompts", back_populates="worker", cascade="all, delete-orphan")
    wtype = "text"

    def check_in(self, max_length, max_context_length, softprompts, **kwargs):
        super().check_in(**kwargs)
        self.max_length = max_length
        self.max_context_length = max_context_length
        self.set_softprompts(softprompts)
        paused_string = ""
        if self.paused:
            paused_string = "(Paused) "
        logger.trace(
            f"{paused_string}Text Worker {self.name} checked-in, offering models {self.models} "
            f"at {self.max_length} max tokens and {self.max_context_length} max content length.",
        )

    def refresh_softprompt_cache(self):
        softprompts_list = [s.softprompt for s in self.softprompts]
        try:
            hr.horde_r_setex(
                f"worker_{self.id}_softprompts_cache",
                timedelta(seconds=600),
                json.dumps(softprompts_list),
            )
        except Exception as e:
            logger.warning(f"Error when trying to set softprompts cache: {e}. Retrieving from DB.")
        return softprompts_list

    def get_softprompt_names(self):
        if hr.horde_r is None:
            return [s.softprompt for s in self.softprompts]
        softprompts_cache = hr.horde_r_get(f"worker_{self.id}_softprompts_cache")
        if not softprompts_cache:
            return self.refresh_softprompt_cache()
        try:
            softprompts_ret = json.loads(softprompts_cache)
        except TypeError:
            logger.error("Softprompts cache could not be loaded: {softprompts_cache}")
            return self.refresh_softprompt_cache()
        if softprompts_ret is None:
            return self.refresh_softprompt_cache()
        return softprompts_ret

    def set_softprompts(self, softprompts):
        softprompts = [sanitize_string(softprompt_name[0:100]) for softprompt_name in softprompts]
        del softprompts[200:]
        softprompts = set(softprompts)
        existing_softprompts_names = set(self.get_softprompt_names())
        if existing_softprompts_names == softprompts:
            return
        logger.debug(
            [
                existing_softprompts_names,
                softprompts,
                existing_softprompts_names == softprompts,
            ],
        )
        db.session.query(TextWorkerSoftprompts).filter_by(worker_id=self.id).delete()
        db.session.commit()
        for softprompt_name in softprompts:
            softprompt = TextWorkerSoftprompts(worker_id=self.id, softprompt=softprompt_name)
            db.session.add(softprompt)
        db.session.commit()
        self.refresh_softprompt_cache()

    def calculate_uptime_reward(self):
        model = self.get_model_names()[0]
        # The base amount of kudos one gets is based on the max context length they've loaded
        base_kudos = 25 + (15 * min(self.max_context_length, 16384) / 1024)
        if not model_reference.is_known_text_model(model):
            return base_kudos * 0.5
        # We consider the 7B models the baseline here
        param_multiplier = model_reference.get_text_model_multiplier(model) / 7
        if param_multiplier < 0.25:
            param_multiplier = 0.25
        # Unvalidated backends get less kudos
        if not is_backed_validated(self.worker.bridge_agent):
            base_kudos *= 0.3
        # The uptime is based on both how much context they provide, as well as how many parameters they're serving
        return round(base_kudos * param_multiplier, 2)

    def can_generate(self, waiting_prompt):
        can_generate = super().can_generate(waiting_prompt)
        if not can_generate[0]:
            return [can_generate[0], can_generate[1]]
        if self.max_context_length < waiting_prompt.max_context_length:
            return [False, "max_context_length"]
        if self.max_length < waiting_prompt.max_length:
            return [False, "max_length"]
        if waiting_prompt.validated_backends and not is_backed_validated(self.bridge_agent):
            return [False, "bridge_version"]
        matching_softprompt = True
        if waiting_prompt.softprompt:
            matching_softprompt = False
            # If a None softprompts has been provided, we always match, since we can always remove the softprompt
            if waiting_prompt.softprompt == "":
                matching_softprompt = True
            if waiting_prompt.softprompt in self.get_softprompt_names():
                matching_softprompt = True
        if not matching_softprompt:
            return [False, "matching_softprompt"]
        return [True, None]

    def get_details(self, is_privileged=False):
        ret_dict = super().get_details(is_privileged)
        ret_dict["max_length"] = self.max_length
        ret_dict["max_context_length"] = self.max_context_length
        return ret_dict

    def parse_models(self, unchecked_models):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        del unchecked_models[200:]
        models = set()
        for model in unchecked_models:
            # # We allow custom models from trusted users
            # if model in model_reference.text_model_names or self.user.trusted:
            usermodel = model.split("::")
            if len(usermodel) == 2:
                user_alias = usermodel[1]
                if self.user.get_unique_alias() != user_alias:
                    raise e.BadRequest(f"This model can only be hosted by {user_alias}")
            models.add(model)
        if len(models) == 0:
            raise e.BadRequest("Unfortunately we cannot accept workers serving unrecognised models at this time")
        return models
