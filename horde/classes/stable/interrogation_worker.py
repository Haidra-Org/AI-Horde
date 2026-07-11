# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from typing import Any

from sqlalchemy import func

from horde.classes.base.kudos import emit_kudos_stat_event
from horde.classes.base.worker import (
    WorkerPerformance,
    WorkerTemplate,
    uuid_column_type,
)
from horde.database.kudos_legacy_projection import project_worker_fulfilment
from horde.database.kudos_reservations import available_kudos
from horde.enums import KudosAggregate, KudosEntryType, KudosUnit
from horde.flask import db
from horde.logger import logger

# from horde.suspicions import Suspicions


class WorkerInterrogationForm(db.Model):
    __tablename__ = "interrogation_worker_forms"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("InterrogationWorker", back_populates="forms")
    form = db.Column(db.String(30))


class WorkerAnnotationType(db.Model):
    __tablename__ = "interrogation_worker_annotation_types"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("workers.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker = db.relationship("InterrogationWorker", back_populates="annotation_types")
    annotation_type = db.Column(db.String(64))


class InterrogationWorker(WorkerTemplate):
    __mapper_args__ = {
        "polymorphic_identity": "interrogation_worker",
    }

    forms = db.relationship("WorkerInterrogationForm", back_populates="worker")
    annotation_types = db.relationship("WorkerAnnotationType", back_populates="worker")
    processing_forms = db.relationship("InterrogationForms", back_populates="worker")
    wtype = "interrogation"

    def check_in(self, max_tiles: int, **kwargs: Any) -> bool:
        if not super().check_in(**kwargs):
            return False
        self.max_power = max_tiles
        # If's OK to provide an empty list here as we don't actually modify this var
        # We only check it in can_generate
        self.set_forms(kwargs.get("forms"))
        # Advertised annotation types are display/moderation only; pop-time matching stays on the
        # live pop payload. A legacy alchemist sends no annotation_types, which clears cleanly.
        self.set_annotation_types(kwargs.get("annotation_types"))
        form_names = self.get_form_names()
        if len(form_names) == 0:
            self.set_forms(["caption"])
        paused_string = ""
        if self.paused:
            paused_string = "(Paused) "
        db.session.commit()
        logger.trace(
            f"{paused_string}Interrogation Worker {self.name} checked-in, offering forms: {form_names} @ {self.max_power} max tiles",
        )
        return True

    def calculate_uptime_reward(self):
        return 40

    def uptime_reward_bypasses_eval(self):
        # Alchemists earn few jobs, so their owners get the uptime reward on the
        # spendable balance directly rather than accruing it in trust escrow;
        # otherwise an untrusted owner could never make usable kudos.
        return True

    def can_interrogate(self, interrogation_form):
        if interrogation_form.interrogation.trusted_workers and not self.user.trusted:
            return False, "untrusted"
        # We do not give untrusted workers VPN generations, to avoid anything slipping by and spooking them.
        if not self.user.trusted:
            if not interrogation_form.interrogation.safe_ip and not interrogation_form.interrogation.user.trusted:
                return False, "untrusted"
        if self.require_upfront_kudos:
            user_actual_kudos = available_kudos(interrogation_form.interrogation.user)
            if (
                not interrogation_form.interrogation.user.trusted
                and interrogation_form.interrogation.user.get_unique_alias() not in self.prioritized_users
                and user_actual_kudos < interrogation_form.kudos + (2 if interrogation_form.interrogation.slow_workers else 1)
            ):
                return False, "kudos"
        return True, None

    @logger.catch(reraise=True)
    def record_interrogation(self, kudos: float, seconds_taken: float) -> None:
        """We record the servers newest interrogation contribution"""
        self.user.record_contributions(raw_things=0, kudos=kudos, contrib_type=self.wtype, commit=False)
        self.modify_kudos(kudos, "interrogated", commit=False, entry_type=KudosEntryType.GENERATION)
        emit_kudos_stat_event(
            KudosEntryType.STAT_CONTRIBUTION,
            1,
            worker_id=self.id,
            worker_user_id=self.user_id,
            unit=KudosUnit.COUNT,
            stat_action=KudosAggregate.FULFILMENTS,
        )
        # workers.fulfilments is applier-maintained from the STAT_CONTRIBUTION
        # posting above once cutover completes; shadow mode still owns the counter
        # inline. Interrogation forms carry no team attribution and no raw things,
        # so only the worker's own fulfilment count moves.
        project_worker_fulfilment(self, team_id=None, raw_things=0, kudos=kudos)
        performances = db.session.query(WorkerPerformance).filter_by(worker_id=self.id).order_by(WorkerPerformance.created.asc())
        if performances.count() >= 20:
            # Keep only the 20 most recent performance records
            keep_ids = (
                db.session.query(WorkerPerformance.id).filter_by(worker_id=self.id).order_by(WorkerPerformance.created.desc()).limit(20)
            )
            db.session.query(WorkerPerformance).filter_by(worker_id=self.id).filter(
                WorkerPerformance.id.not_in(keep_ids),
            ).delete(synchronize_session=False)
        new_performance = WorkerPerformance(worker_id=self.id, performance=seconds_taken)
        db.session.add(new_performance)
        # if things_per_sec / thing_divisor > things_per_sec_suspicion_threshold:
        #     self.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[round(things_per_sec / thing_divisor,2)])

    def get_form_names(self):
        form_names = (
            db.session.query(func.distinct(WorkerInterrogationForm.form).label("name"))
            .filter(WorkerInterrogationForm.worker_id == self.id)
            .all()
        )
        return [f.name for f in form_names]

    def set_forms(self, forms):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        existing_forms = db.session.query(WorkerInterrogationForm).filter_by(worker_id=self.id)
        existing_form_names = set([f.form for f in existing_forms.all()])
        if existing_form_names == forms:
            return
        existing_forms.delete()
        for form_name in forms:
            form = WorkerInterrogationForm(worker_id=self.id, form=form_name)
            db.session.add(form)
        db.session.commit()

    def get_annotation_type_names(self):
        annotation_type_names = (
            db.session.query(func.distinct(WorkerAnnotationType.annotation_type).label("name"))
            .filter(WorkerAnnotationType.worker_id == self.id)
            .all()
        )
        return [a.name for a in annotation_type_names]

    def set_annotation_types(self, annotation_types):
        # A legacy alchemist advertises no annotation types; treat absent as an empty set.
        if not annotation_types:
            annotation_types = []
        # We don't allow workers to claim they can serve more than 100 annotation types (to prevent abuse)
        annotation_types = list(annotation_types)[:100]
        existing_annotation_types = db.session.query(WorkerAnnotationType).filter_by(worker_id=self.id)
        existing_annotation_type_names = set([a.annotation_type for a in existing_annotation_types.all()])
        if existing_annotation_type_names == set(annotation_types):
            return
        existing_annotation_types.delete()
        for annotation_type in annotation_types:
            entry = WorkerAnnotationType(worker_id=self.id, annotation_type=annotation_type)
            db.session.add(entry)
        db.session.commit()

    def get_performance(self):
        performances = [p.performance for p in self.performance]
        if len(performances):
            ret_str = f"{round(sum(performances) / len(performances), 1)} seconds per form"
        else:
            ret_str = "No requests fulfilled yet"
        return ret_str

    def get_details(self, details_privilege=0):
        ret_dict = super().get_details(details_privilege)
        ret_dict["forms"] = self.get_form_names()
        ret_dict["annotation_types"] = self.get_annotation_type_names()
        return ret_dict
