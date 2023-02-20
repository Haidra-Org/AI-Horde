from sqlalchemy import func
from datetime import datetime
from horde.logger import logger
from horde.flask import db
from horde.classes.base.worker import WorkerTemplate, WorkerPerformance, uuid_column_type
from horde.suspicions import Suspicions


class WorkerInterrogationForm(db.Model):
    __tablename__ = "interrogation_worker_forms"
    id = db.Column(db.Integer, primary_key=True)
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id", ondelete="CASCADE"), nullable=False)
    worker = db.relationship(f"InterrogationWorker", back_populates="forms")
    form = db.Column(db.String(30))
    wtype = "interrogation"


class InterrogationWorker(WorkerTemplate):
    __mapper_args__ = {
        "polymorphic_identity": "interrogation_worker",
    }

    forms = db.relationship("WorkerInterrogationForm", back_populates="worker")
    processing_forms = db.relationship("InterrogationForms", back_populates="worker")
    wtype = "interrogation"

    def check_in(self, **kwargs):
        super().check_in(**kwargs)
        # If's OK to provide an empty list here as we don't actually modify this var
        # We only check it in can_generate
        self.set_forms(kwargs.get("forms"))
        form_names = self.get_form_names()
        if len(form_names) == 0:
            self.set_forms(['caption'])
        paused_string = ''
        if self.paused:
            paused_string = '(Paused) '
        db.session.commit()
        logger.trace(f"{paused_string}Interrogation Worker {self.name} checked-in, offering forms: {form_names}")

    def calculate_uptime_reward(self):
        return 15

    def can_interrogate(self, interrogation_form):
        if interrogation_form.interrogation.trusted_workers and not self.user.trusted:
            return False, 'untrusted'
        # We do not give untrusted workers VPN generations, to avoid anything slipping by and spooking them.
        if not self.user.trusted:
            if not interrogation_form.interrogation.safe_ip and not interrogation_form.interrogation.user.trusted:
                return False, 'untrusted'
        if self.require_upfront_kudos:
            user_actual_kudos = interrogation_form.interrogation.user.kudos
            # We don't want to take into account minimum kudos
            if user_actual_kudos > 0:
                user_actual_kudos -= interrogation_form.interrogation.user.get_min_kudos()
            if (
                not interrogation_form.interrogation.user.trusted
                and interrogation_form.interrogation.user.get_unique_alias() not in self.prioritized_users
                and user_actual_kudos < interrogation_form.kudos + 1 # All forms take +1 kudos than they give to the worker
            ):
                return False, 'kudos'
        return True, None

    @logger.catch(reraise=True)
    def record_interrogation(self, kudos, seconds_taken):
        '''We record the servers newest interrogation contribution
        '''
        self.user.record_contributions(raw_things = 0, kudos = kudos, contrib_type = self.wtype)
        self.modify_kudos(kudos,'interrogated')
        self.fulfilments += 1
        # TODO: Switch to use desc() and offset to ensure we don't have performances left over
        performances = db.session.query(WorkerPerformance).filter_by(worker_id=self.id).order_by(WorkerPerformance.created.asc())
        if performances.count() >= 20:
            db.session.delete(performances.first())
        new_performance = WorkerPerformance(worker_id=self.id, performance=seconds_taken)
        db.session.add(new_performance)
        db.session.commit()
        # if things_per_sec / thing_divisor > things_per_sec_suspicion_threshold:
        #     self.report_suspicion(reason = Suspicions.UNREASONABLY_FAST, formats=[round(things_per_sec / thing_divisor,2)])


    def get_form_names(self):
        form_names = db.session.query(func.distinct(WorkerInterrogationForm.form).label('name')).filter(WorkerInterrogationForm.worker_id == self.id).all()
        return [f.name for f in form_names]


    def set_forms(self, forms):
        # We don't allow more workers to claim they can server more than 100 models atm (to prevent abuse)
        existing_forms = db.session.query(WorkerInterrogationForm).filter_by(worker_id=self.id)
        existing_form_names = set([f.form for f in existing_forms.all()])
        if existing_form_names == forms:
            return
        existing_forms.delete()
        for form_name in forms:
            form = WorkerInterrogationForm(worker_id=self.id,form=form_name)
            db.session.add(form)
        db.session.commit()


    def get_performance(self):
        performances = [p.performance for p in self.performance]
        if len(performances):
            ret_str = f'{round(sum(performances) / len(performances),1)} seconds per form'
        else:
            ret_str = f'No requests fulfilled yet'
        return(ret_str)
