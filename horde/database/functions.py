# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import logfire
from sqlalchemy import Boolean, and_, case, func, not_, or_
from sqlalchemy.orm import contains_eager, joinedload, noload, selectinload

import horde.classes.base.stats as stats
from horde import vars as hv
from horde.bridge_reference import (
    check_bridge_capability,
    get_supported_samplers,
)
from horde.classes.base.detection import Filter
from horde.classes.base.kudos import KudosLedger, kudos_event
from horde.classes.base.processing_generation import ProcessingGeneration
from horde.classes.base.style import Style, StyleCollection, StyleModel, StyleTag
from horde.classes.base.user import KudosTransferLog, User, UserRecords, UserSharedKey
from horde.classes.base.waiting_prompt import WaitingPrompt, WPAllowedWorkers, WPModels
from horde.classes.base.worker import Worker, WorkerMessage, WorkerModel, WorkerPerformance
from horde.classes.kobold.processing_generation import TextProcessingGeneration
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker
from horde.classes.stable.interrogation import Interrogation, InterrogationForms
from horde.classes.stable.interrogation_worker import InterrogationWorker
from horde.classes.stable.processing_generation import ImageProcessingGeneration
from horde.classes.stable.waiting_prompt import ImageWaitingPrompt
from horde.classes.stable.worker import ImageWorker
from horde.consts import (
    EXTENDED_SCHEDULERS,
    FLOW_SHIFT_PARAM,
    LEGACY_IMAGE_CONTROL_TYPES,
    SIGMA_GENERATOR_SCHEDULERS,
    SOLVER_KNOB_PARAMS,
)
from horde.database.classes import FakeWPRow
from horde.database.kudos_legacy_projection import consume_user_reservation
from horde.database.kudos_reservations import reserve_kudos
from horde.enums import KudosAuditDetail, KudosEntryType, State
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.metrics import kudos_transfers_idempotent_replays, pop_query_duration
from horde.model_reference import model_reference
from horde.utils import hash_api_key, validate_regex

ALLOW_ANONYMOUS = True
type KudosTransferResult = list[int | float | str | bool | None]
WORKER_CLASS_MAP = {
    "image": ImageWorker,
    "text": TextWorker,
    "interrogation": InterrogationWorker,
}
WP_CLASS_MAP = {
    "image": ImageWaitingPrompt,
    "text": TextWaitingPrompt,
}


@dataclass(frozen=True)
class RequestWorkerAvailability:
    """Represents current worker capacity that passes the same gates as job dispatch."""

    worker_count: int
    thread_count: int
    has_inflight_generation: bool = False

    @property
    def is_possible(self) -> bool:
        """Return whether capacity or an in-flight generation can serve the request."""

        return self.has_inflight_generation or self.worker_count > 0


def get_anon():
    # The anonymous account is identified by oauth_id "anon" (id 0). Looking it
    # up by api_key("anon") never matched - anon is seeded with the api_key
    # hash of "0000000000" - so this silently returned None, disabling guards
    # such as the kudos-transfer-to-anon block. Query the canonical identifier.
    return db.session.query(User).filter_by(oauth_id="anon").first()


# TODO: Switch this to take this node out of operation instead?
# Or maybe just delete this
def shutdown(seconds):
    if seconds > 0:
        logger.critical(f"Initiating shutdown in {seconds} seconds")
        time.sleep(seconds)


def get_top_contributor():
    top_contributor = None
    top_contributor = (
        db.session.query(User)
        .join(UserRecords)
        .filter(
            UserRecords.record_type == "CONTRIBUTION",
            UserRecords.record == "image",
        )
        .order_by(UserRecords.value.desc())
        .first()
    )
    return top_contributor


def get_top_worker():
    top_worker = None
    top_worker = db.session.query(ImageWorker).order_by(ImageWorker.contributions.desc()).first()
    return top_worker


def get_active_workers(worker_type=None):
    active_workers = []
    if worker_type is None or worker_type == "image":
        active_workers += db.session.query(ImageWorker).filter(ImageWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)).all()
    if worker_type is None or worker_type == "text":
        active_workers += db.session.query(TextWorker).filter(TextWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300)).all()
    if worker_type is None or worker_type == "interrogation":
        active_workers += (
            db.session.query(InterrogationWorker)
            .filter(InterrogationWorker.last_check_in > datetime.utcnow() - timedelta(seconds=300))
            .all()
        )
    return active_workers


def count_active_workers(worker_class="image"):
    worker_cache = hr.horde_r_get_json(f"count_active_workers_{worker_class}")
    if worker_cache:
        return tuple(worker_cache)
    WorkerClass = ImageWorker
    if worker_class == "interrogation":
        WorkerClass = InterrogationWorker
    if worker_class == "text":
        WorkerClass = TextWorker
    active_workers = db.session.query(WorkerClass).filter(WorkerClass.last_check_in > datetime.utcnow() - timedelta(seconds=300)).count()
    active_workers_threads = (
        db.session.query(func.sum(WorkerClass.threads).label("threads"))
        .filter(WorkerClass.last_check_in > datetime.utcnow() - timedelta(seconds=300))
        .first()
    )
    # logger.debug([worker_class,active_workers,active_workers_threads.threads])
    if active_workers and active_workers_threads.threads:
        hr.horde_r_setex_json(
            f"count_active_workers_{worker_class}",
            timedelta(seconds=300),
            [active_workers, active_workers_threads.threads],
        )
        return active_workers, active_workers_threads.threads
    return 0, 0


def count_workers_on_ip(ip_addr):
    return db.session.query(ImageWorker).filter_by(ipaddr=ip_addr).count()


def count_workers_in_ipaddr(ipaddr):
    return count_workers_on_ip(ipaddr)


def get_total_usage():
    totals = {
        hv.thing_names["image"]: 0,
        hv.thing_names["text"]: 0,
        "image_fulfilments": 0,
        "text_fulfilments": 0,
    }
    result = db.session.query(
        func.sum(ImageWorker.contributions).label("contributions"),
        func.sum(ImageWorker.fulfilments).label("fulfilments"),
    ).first()
    if result:
        totals[hv.thing_names["image"]] = result.contributions if result.contributions else 0
        totals["image_fulfilments"] = result.fulfilments if result.fulfilments else 0
    result = db.session.query(
        func.sum(TextWorker.contributions).label("contributions"),
        func.sum(TextWorker.fulfilments).label("fulfilments"),
    ).first()
    if result:
        totals[hv.thing_names["text"]] = result.contributions if result.contributions else 0
        totals["text_fulfilments"] = result.fulfilments if result.fulfilments else 0
    form_result = result = db.session.query(func.sum(InterrogationWorker.fulfilments).label("forms")).first()
    if form_result:
        totals["forms"] = result.forms if result.forms else 0
    return totals


# PostgreSQL ``integer`` (int4) upper bound. A user id larger than this reaches
# the driver and raises NumericValueOutOfRange (a 500 that also aborts the
# transaction), so out-of-range ids from path params are treated as "not found".
_PG_INT4_MAX = 2147483647


def _coerce_user_id(value):
    """Return ``value`` as an int4-representable user id, or None if it cannot be
    (unparseable, negative, or out of range). Guards id-based user lookups
    against hostile path params before they reach an integer column."""
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    if not (0 <= coerced <= _PG_INT4_MAX):
        return None
    return coerced


def find_user_by_oauth_id(oauth_id):
    if oauth_id == "anon" and not ALLOW_ANONYMOUS:
        return None
    return db.session.query(User).filter_by(oauth_id=oauth_id).first()


def find_user_by_username(username):
    ulist = username.split("#")
    user_id = _coerce_user_id(ulist[-1])
    if user_id is None:
        return None
    if user_id == 0 and not ALLOW_ANONYMOUS:
        return None
    # This approach handles someone cheekily putting # in their username
    return db.session.query(User).filter_by(id=user_id).filter(User.oauth_id != "<wiped>").first()


def find_user_by_id(user_id):
    user_id = _coerce_user_id(user_id)
    if user_id is None:
        return None
    if user_id == 0 and not ALLOW_ANONYMOUS:
        return None
    return db.session.query(User).filter_by(id=user_id).filter(User.oauth_id != "<wiped>").first()


def find_user_by_contact(contact):
    # Counting the same query separately doubles the work for a lookup that only
    # needs to know whether more than one row matched, so fetch two rows instead.
    matched_users = db.session.query(User).filter_by(contact=contact).filter(User.oauth_id != "<wiped>").limit(2).all()
    if len(matched_users) == 0:
        return None
    selected_user = matched_users[0]
    if len(matched_users) > 1:
        logger.warning(f"Multiple users found with the same contact {contact}! Returning first found {selected_user.id}")
    return selected_user


def find_user_by_api_key(api_key):
    if api_key == 0000000000 and not ALLOW_ANONYMOUS:
        return None
    user = db.session.query(User).filter_by(api_key=hash_api_key(api_key)).filter(User.oauth_id != "<wiped>").first()
    return user


def find_user_by_sharedkey(shared_key):
    try:
        sharedkey_uuid = uuid.UUID(shared_key)
    except ValueError:
        return None
    if SQLITE_MODE:
        sharedkey_uuid = str(sharedkey_uuid)
    user = db.session.query(User).join(UserSharedKey).filter(UserSharedKey.id == shared_key).first()
    return user


def find_sharedkey(shared_key):
    try:
        sharedkey_uuid = uuid.UUID(shared_key)
    except ValueError:
        return None
    if SQLITE_MODE:
        sharedkey_uuid = str(sharedkey_uuid)
    sharedkey = db.session.query(UserSharedKey).filter(UserSharedKey.id == shared_key).first()
    return sharedkey


def find_worker_by_name(worker_name, worker_class=ImageWorker):
    worker = db.session.query(worker_class).filter_by(name=worker_name).first()
    return worker


def find_worker_id_by_name(worker_name):
    for worker_class in [ImageWorker, TextWorker, InterrogationWorker]:
        worker_id = db.session.query(worker_class.id).filter_by(name=worker_name).first()
        if worker_id:
            return worker_id


def worker_name_exists(worker_name):
    for worker_class in [ImageWorker, TextWorker, InterrogationWorker]:
        worker = db.session.query(worker_class).filter_by(name=worker_name).count()
        if worker:
            return True
    return False


def find_worker_by_id(worker_id):
    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError:
        logger.debug(f"Non-UUID worker_id sent: '{worker_id}'.")
        return None
    if SQLITE_MODE:
        worker_uuid = str(worker_uuid)
    worker = db.session.query(ImageWorker).filter_by(id=worker_uuid).first()
    if not worker:
        worker = db.session.query(TextWorker).filter_by(id=worker_uuid).first()
    if not worker:
        worker = db.session.query(InterrogationWorker).filter_by(id=worker_uuid).first()
    return worker


def worker_exists(worker_id):
    try:
        worker_uuid = uuid.UUID(worker_id)
    except ValueError:
        logger.debug(f"Non-UUID worker_id sent: '{worker_id}'.")
        return None
    if SQLITE_MODE:
        worker_uuid = str(worker_uuid)
    wc = db.session.query(ImageWorker).filter_by(id=worker_uuid).count()
    if not wc:
        wc = db.session.query(TextWorker).filter_by(id=worker_uuid).count()
    if not wc:
        wc = db.session.query(InterrogationWorker).filter_by(id=worker_uuid).count()
    return wc


def workers_exist(worker_ids):
    """Given a list of worker_id strings, return the set of IDs that do NOT exist."""
    valid_uuids = {}
    invalid_ids = set()
    for wid in worker_ids:
        try:
            valid_uuids[wid] = uuid.UUID(wid)
        except ValueError:
            invalid_ids.add(wid)
    if not valid_uuids:
        return invalid_ids
    uuid_values = list(valid_uuids.values())
    if SQLITE_MODE:
        uuid_values = [str(u) for u in uuid_values]
    # Single query across all worker types using the polymorphic base
    from horde.classes.base.worker import Worker

    found_ids = {row[0] for row in db.session.query(Worker.id).filter(Worker.id.in_(uuid_values)).all()}
    if SQLITE_MODE:
        found_id_strs = {str(fid) for fid in found_ids}
        missing = {wid for wid, uid in valid_uuids.items() if str(uid) not in found_id_strs}
    else:
        missing = {wid for wid, uid in valid_uuids.items() if uid not in found_ids}
    return missing | invalid_ids


MODEL_ETA_NO_CAPACITY = 10000
"""The eta reported when a model's queue has no way to clear: no workers, or no known speed at all."""


def compute_model_eta(
    things_queued: float,
    jobs_queued: int,
    worker_count: int,
    model_avg_perf: float,
    global_avg_perf: float,
) -> int:
    """Return the estimated seconds for one model's queue to clear.

    Pure arithmetic on values the caller already holds, so it does no DB or Redis work.

    A model's threads only parallelize across the jobs actually queued for it, so the
    thread count is clamped to the queued job count. Without that clamp a model with far
    more idle threads than demand reports an eta far below the time a single job takes.
    This mirrors the request-level clamp in ``WaitingPrompt.get_status``.

    A model that has served too little to have its own recorded average would otherwise
    report the no-capacity sentinel while workers are sitting ready for it, so the
    horde-wide average for that model's type stands in. Both rates are raw things per
    second per thread, so they are directly interchangeable here.
    """
    if worker_count <= 0:
        return MODEL_ETA_NO_CAPACITY
    if things_queued <= 0 or jobs_queued <= 0:
        return 0
    perf = model_avg_perf if model_avg_perf > 0 else global_avg_perf
    if perf <= 0:
        return MODEL_ETA_NO_CAPACITY
    return int(things_queued / (min(worker_count, jobs_queued) * perf))


def get_available_models(filter_model_name: str = None):
    models_dict = {}
    available_worker_models = None

    if filter_model_name is not None:
        # Decode the filter_model_name from URL encoding
        # e.g., `aphrodite%2FNeverSleep%2FNoromaid-13b-v0.3` will become `aphrodite/NeverSleep/Noromaid-13b-v0.3`.
        filter_model_name = urllib.parse.unquote(filter_model_name)

    for model_type, worker_class, wp_class, procgen_class in [
        ("image", ImageWorker, ImageWaitingPrompt, ImageProcessingGeneration),
        ("text", TextWorker, TextWaitingPrompt, TextProcessingGeneration),
    ]:
        # To avoid abuse, when looking for filtered model names, we are searching only in known models and specials
        if (
            filter_model_name
            and filter_model_name not in model_reference.stable_diffusion_names
            and filter_model_name not in model_reference.testing_models
            and filter_model_name not in model_reference.text_model_names
            and "horde_special" not in filter_model_name
            and filter_model_name != "SDXL_beta::stability.ai#6901"
        ):
            continue
        # If we're doing a filter, and we've already found the model type, we don't want to look in other worker versions
        if filter_model_name and available_worker_models and len(available_worker_models) > 0:
            continue
        available_worker_models = (
            db.session.query(
                WorkerModel.model,
                func.sum(worker_class.threads).label("total_threads"),
                # worker_class.id.label('worker_id') # TODO: make the query return a list or workers serving this model?
            )
            .join(
                worker_class,
            )
            .filter(
                worker_class.last_check_in > datetime.utcnow() - timedelta(seconds=300),
                worker_class.maintenance == False,  # noqa E712
            )
        )
        if filter_model_name:
            available_worker_models = available_worker_models.filter(WorkerModel.model == filter_model_name)
        available_worker_models = available_worker_models.group_by(WorkerModel.model).all()
        # logger.debug(available_worker_models)
        for model_row in available_worker_models:
            model_name = model_row.model
            # We don't want to publicly display special models
            if not filter_model_name and "horde_special" in model_name:
                continue
            models_dict[model_name] = {}
            models_dict[model_name]["name"] = model_name
            models_dict[model_name]["count"] = model_row.total_threads
            models_dict[model_name]["type"] = model_type

            models_dict[model_name]["queued"] = 0
            models_dict[model_name]["jobs"] = 0
            models_dict[model_name]["eta"] = 0
            models_dict[model_name]["performance"] = stats.get_model_avg(model_name)
            models_dict[model_name]["workers"] = []

        known_models = [filter_model_name] if filter_model_name else list(model_reference.stable_diffusion_names)
        ophan_models = (
            db.session.query(
                WPModels.model,
            )
            .join(
                wp_class,
            )
            .filter(
                WPModels.model.not_in(list(models_dict.keys())),
                WPModels.model.in_(known_models),
                wp_class.n > 0,
            )
            .group_by(WPModels.model)
            .all()
        )
        for model_row in ophan_models:
            model_name = model_row.model
            models_dict[model_name] = {}
            models_dict[model_name]["name"] = model_name
            models_dict[model_name]["count"] = 0
            models_dict[model_name]["queued"] = 0
            models_dict[model_name]["jobs"] = 0
            models_dict[model_name]["type"] = model_type
            models_dict[model_name]["eta"] = 0
            models_dict[model_name]["performance"] = stats.get_model_avg(model_name)
            models_dict[model_name]["workers"] = []
        if filter_model_name:
            things_per_model, jobs_per_model = count_things_for_specific_model(
                wp_class,
                procgen_class,
                filter_model_name,
            )
        else:
            things_per_model, jobs_per_model = count_things_per_model(wp_class)
        # Fetched once per model type rather than per model: it is the same value for every
        # model of that type and each call can reach Redis.
        global_avg_perf = get_request_avg(model_type)
        # If we request a lite_dict, we only want worker count per model and a dict format
        for model_name in things_per_model:
            # This shouldn't happen, but I'm checking anyway
            if model_name not in models_dict:
                # logger.debug(f"Tried to match non-existent wp model {model_name} to worker models. Skipping.")
                continue
            models_dict[model_name]["queued"] = things_per_model[model_name]
            models_dict[model_name]["jobs"] = jobs_per_model[model_name]
            models_dict[model_name]["eta"] = compute_model_eta(
                things_queued=things_per_model[model_name],
                jobs_queued=jobs_per_model[model_name],
                worker_count=models_dict[model_name]["count"],
                model_avg_perf=models_dict[model_name]["performance"],
                global_avg_perf=global_avg_perf,
            )
    return list(models_dict.values())


def retrieve_available_models(model_type=None, min_count=None, max_count=None, model_state="known"):
    """Retrieves model details from Redis cache, or from DB if cache is unavailable"""
    if hr.horde_r is None:
        return get_available_models()
    model_cache = hr.horde_r_get("models_cache")
    try:
        models_ret = json.loads(model_cache)
    except TypeError:
        logger.error(f"Model cache could not be loaded: {model_cache}")
        return []
    if models_ret is None:
        models_ret = get_available_models()
    if model_type is not None:
        models_ret = [md for md in models_ret if md.get("type", "image") == model_type]
    if min_count is not None:
        models_ret = [md for md in models_ret if md["count"] >= min_count]
    if max_count is not None:
        models_ret = [md for md in models_ret if md["count"] <= max_count]

    def check_model_state(model_name):
        if model_type is None:
            return True
        model_check = model_reference.is_known_image_model
        if model_type == "text":
            model_check = model_reference.is_known_text_model
        if model_state == "known" and model_check(model_name):
            return True
        if model_state == "custom" and not model_check(model_name):
            return True
        if model_state == "all":
            return True
        return False

    models_ret = [md for md in models_ret if check_model_state(md["name"])]

    return models_ret


def transfer_kudos(
    source_user: User,
    dest_user: User,
    amount: float,
    idempotency_key: str | None = None,
) -> KudosTransferResult:
    reverse_transfer = hr.horde_r_get(f"kudos_transfer_{dest_user.id}-{source_user.id}")
    if reverse_transfer:
        return [
            0,
            "This user transferred kudos to you very recently. Please wait at least 1 minute.",
            "TooFastKudosTransfers",
        ]
    if source_user.is_suspicious():
        return [
            0,
            "Something went wrong when sending kudos. Please contact the mods.",
            "FaultWhenKudosSending",
        ]
    if source_user.flagged:
        return [
            0,
            "The target account has been flagged for suspicious activity and tranferring kudos to them is blocked.",
            "SourceAccountFlagged",
        ]
    if source_user.education:
        return [
            0,
            "Education accounts cannot transfer kudos away",
            "EducationCannotSendKudos",
        ]
    if dest_user.is_suspicious():
        return [
            0,
            "Something went wrong when receiving kudos. Please contact the mods.",
            "FaultWhenKudosReceiving",
        ]
    if dest_user.flagged:
        return [0, "Your account has been flagged for suspicious activity. Please contact the mods.", "TargetAccountFlagged"]
    if dest_user.deleted:
        return [0, "This destination account has been scheduled for deletion and is disabled", "DeletedUser"]
    if source_user.deleted:
        return [0, "This source account has been scheduled for deletion and is disabled", "DeletedUser"]
    if amount < 0:
        return [0, "Nice try...", "NegativeKudosTransfer"]
    if amount == 0:
        return [0, "Transfer amount must be positive.", "InvalidKudosTransferAmount"]
    transfer_type = "gifted"
    if dest_user.education:
        transfer_type = "donated"
    # The payer hold, audit row, debit, and credit are one transaction.  The
    # reservation serializes only this payer; it is consumed inline when the
    # projection owns writes and at fold time when the applier does.
    scoped_retry_key = None if idempotency_key is None else f"transfer:{source_user.id}:{idempotency_key}"
    with kudos_event(idempotency_key=scoped_retry_key) as event:
        prior_postings = db.session.query(KudosLedger).filter(KudosLedger.event_id == event.event_id).all()
        if prior_postings:
            same_request = any(row.user_id == source_user.id and row.amount == -amount for row in prior_postings) and any(
                row.user_id == dest_user.id and row.amount == amount for row in prior_postings
            )
            if not same_request:
                return [0, "Idempotency key was already used with different transfer parameters.", "IdempotencyKeyConflict"]
            kudos_transfers_idempotent_replays.add(1)
            return [amount, "OK", None, True]
        reservation = reserve_kudos(
            source_user,
            amount,
            business_id=f"transfer:{source_user.id}:{event.event_id}",
            event_id=event.event_id,
        )
        if reservation is None:
            db.session.rollback()
            return [0, "Not enough kudos.", "KudosTransferNotEnough"]
        # The payer lock acquired by reserve_kudos closes the concurrent retry
        # race between the optimistic check above and the first commit.
        prior_postings = db.session.query(KudosLedger).filter(KudosLedger.event_id == event.event_id).all()
        if prior_postings:
            db.session.rollback()
            same_request = any(row.user_id == source_user.id and row.amount == -amount for row in prior_postings) and any(
                row.user_id == dest_user.id and row.amount == amount for row in prior_postings
            )
            if not same_request:
                return [0, "Idempotency key was already used with different transfer parameters.", "IdempotencyKeyConflict"]
            kudos_transfers_idempotent_replays.add(1)
            return [amount, "OK", None, True]
        transfer_log = KudosTransferLog(
            source_id=source_user.id,
            dest_id=dest_user.id,
            kudos=amount,
        )
        db.session.add(transfer_log)
        source_user.modify_kudos(
            -amount,
            transfer_type,
            commit=False,
            entry_type=KudosEntryType.TRANSFER,
            detail={KudosAuditDetail.RESERVATION_ID: reservation.business_id},
        )
        dest_user.modify_kudos(amount, "received", commit=False, entry_type=KudosEntryType.TRANSFER)
        # When the debit above was materialized inline its ledger row is born
        # applied, so the applier's fold-time consumption never sees this hold;
        # consume it here in that case (no-op while projection is async).
        consume_user_reservation(reservation.business_id, amount)
        db.session.commit()
    hr.horde_r_setex(f"kudos_transfer_{source_user.id}-{dest_user.id}", timedelta(seconds=60), 1)
    logger.info(f"{source_user.get_unique_alias()} transfered {amount} kudos to {dest_user.get_unique_alias()}")
    return [amount, "OK", None, False]


def transfer_kudos_to_username(
    source_user: User,
    dest_username: str,
    amount: float,
    idempotency_key: str | None = None,
) -> KudosTransferResult:
    dest_user = find_user_by_username(dest_username)
    shared_key = None
    if not dest_user:
        shared_key = find_sharedkey(dest_username)
        if not shared_key:
            return [0, "Invalid target username.", "InvalidTargetUsername"]
        if shared_key.is_expired():
            return [0, "This shared key has expired", "SharedKeyExpired"]
        dest_user = shared_key.user
    if dest_user == get_anon():
        return [0, "Tried to burn kudos via sending to Anonymous. Assuming PEBKAC and aborting.", "KudosTransferToAnon"]
    if dest_user == source_user:
        return [0, "Cannot send kudos to yourself, ya monkey!", "KudosTransferToSelf"]
    kudos = transfer_kudos(source_user, dest_user, amount, idempotency_key=idempotency_key)
    replayed = len(kudos) > 3 and kudos[3]
    if kudos[0] > 0 and not replayed and shared_key is not None and shared_key.kudos != -1:
        shared_key.kudos += kudos[0]
        db.session.commit()
    return kudos


def transfer_kudos_from_apikey_to_username(
    source_api_key: str,
    dest_username: str,
    amount: float,
    idempotency_key: str | None = None,
) -> KudosTransferResult:
    source_user = find_user_by_api_key(source_api_key)
    if not source_user:
        return [0, "Invalid API Key.", "InvalidAPIKey"]
    if source_user == get_anon():
        return [0, "You cannot transfer Kudos from Anonymous, smart-ass.", "KudosTransferFromAnon"]
    kudos = transfer_kudos_to_username(source_user, dest_username, amount, idempotency_key=idempotency_key)
    return kudos


# Should be overriden
def convert_things_to_kudos(things, **kwargs):
    # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
    kudos = round(things, 2)
    return kudos


def count_waiting_requests(user, models=None, request_type="image"):
    with logfire.span("horde.db.count_waiting_requests", request_type=request_type, model_count=len(models) if models else 0):
        return _count_waiting_requests(user, models, request_type)


def _count_waiting_requests(user, models=None, request_type="image"):
    wp_class = ImageWaitingPrompt
    if request_type == "text":
        wp_class = TextWaitingPrompt

    if not models:
        models = []
    if len(models):
        known_model_query = (
            db.session.query(func.sum(wp_class.n))
            .select_from(
                WPModels,
            )
            .join(wp_class, WPModels.wp_id == wp_class.id)
            .filter(
                WPModels.model.in_(models),
                wp_class.user_id == user.id,
                wp_class.faulted == False,  # noqa E712
                wp_class.active == True,  # noqa E712
                wp_class.n >= 1,
            )
            .scalar()
        )
        if known_model_query is None:
            return 0
        logger.debug(known_model_query)
        return known_model_query
    else:
        unknown_model_query = (
            db.session.query(func.sum(wp_class.n))
            .filter(
                wp_class.user_id == user.id,
                wp_class.faulted == False,  # noqa E712
                wp_class.n >= 1,
            )
            .scalar()
        )
        if unknown_model_query is None:
            return 0
        return unknown_model_query


def count_waiting_interrogations(user):
    found_i_forms = (
        db.session.query(InterrogationForms.state, Interrogation.user_id)
        .join(Interrogation)
        .filter(
            Interrogation.user_id == user.id,
            or_(
                InterrogationForms.state == State.WAITING,
                InterrogationForms.state == State.PROCESSING,
            ),
        )
    )
    return found_i_forms.count()

    # for wp in db.session.query(ImageWaitingPrompt).all():  # TODO this can likely be improved
    #     model_names = wp.get_model_names()
    #     #logger.warning(datetime.utcnow())
    #     if wp.user == user and not wp.is_completed():
    #         #logger.warning(datetime.utcnow())
    #         # If we pass a list of models, we want to count only the WP for these particular models.
    #         if len(models) > 0:
    #             matching_model = False
    #             for model in models:
    #                 if model in model_names:
    #                     #logger.warning(datetime.utcnow())
    #                     matching_model = True
    #                     break
    #             if not matching_model:
    #                 continue
    #         count += wp.n
    # #logger.warning(datetime.utcnow())
    # return(count)


def count_totals():
    queued_images = f"queued_{hv.thing_names['image']}"
    queued_text = f"queued_{hv.thing_names['text']}"
    queued_forms = "queued_forms"
    ret_dict = {
        "queued_requests": 0,
        "queued_text_requests": 0,
        queued_images: 0,
        queued_text: 0,
    }
    all_image_wp_counts = (
        db.session.query(
            ImageWaitingPrompt.id,
            (func.sum(ImageWaitingPrompt.n) + func.count(ImageProcessingGeneration.wp_id)).label("total_count"),
            func.sum(ImageWaitingPrompt.things).label("total_things"),
        )
        .outerjoin(
            ImageProcessingGeneration,
            and_(
                ImageWaitingPrompt.id == ImageProcessingGeneration.wp_id,
                ImageProcessingGeneration.generation == None,  # noqa E712
            ),
        )
        .filter(
            ImageWaitingPrompt.n > 0,
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.active == True,  # noqa E712
        )
        .group_by(ImageWaitingPrompt.id)
        .subquery("all_image_wp_counts")
    )
    total_image_sum = (
        db.session.query(
            func.sum(all_image_wp_counts.c.total_count).label("total_count_sum"),
            func.sum(all_image_wp_counts.c.total_things).label("total_things_sum"),
        )
        .select_from(all_image_wp_counts)
        .one()
    )
    ret_dict["queued_requests"] = int(total_image_sum.total_count_sum) if total_image_sum.total_count_sum is not None else 0
    ret_dict[queued_images] = (
        round(int(total_image_sum.total_things_sum) / hv.thing_divisors["image"], 2) if total_image_sum.total_things_sum is not None else 0
    )
    all_text_wp_counts = (
        db.session.query(
            TextWaitingPrompt.id,
            (func.sum(TextWaitingPrompt.n) + func.count(TextProcessingGeneration.wp_id)).label("total_count"),
            func.sum(TextWaitingPrompt.things).label("total_things"),
        )
        .outerjoin(
            TextProcessingGeneration,
            and_(
                TextWaitingPrompt.id == TextProcessingGeneration.wp_id,
                TextProcessingGeneration.generation == None,  # noqa E712
            ),
        )
        .filter(
            TextWaitingPrompt.n > 0,
            TextWaitingPrompt.faulted == False,  # noqa E712
            TextWaitingPrompt.active == True,  # noqa E712
        )
        .group_by(TextWaitingPrompt.id)
        .subquery("all_text_wp_counts")
    )
    total_text_sum = (
        db.session.query(
            func.sum(all_text_wp_counts.c.total_count).label("total_count_sum"),
            func.sum(all_text_wp_counts.c.total_things).label("total_things_sum"),
        )
        .select_from(all_text_wp_counts)
        .one()
    )
    ret_dict["queued_text_requests"] = int(total_text_sum.total_count_sum) if total_text_sum.total_count_sum is not None else 0
    ret_dict[queued_text] = (
        int(total_text_sum.total_things_sum) / hv.thing_divisors["text"] if total_text_sum.total_things_sum is not None else 0
    )
    ret_dict[queued_forms] = (
        db.session.query(
            InterrogationForms.state,
        )
        .filter(
            or_(
                InterrogationForms.state == State.WAITING,
                InterrogationForms.state == State.PROCESSING,
            ),
        )
        .count()
    )
    # logger.debug(ret_dict)
    return ret_dict


def retrieve_totals(ignore_cache=False):
    """Retrieves horde totals from Redis cache"""
    if ignore_cache or hr.horde_r is None:
        return count_totals()
    totals_ret = hr.horde_r_get("totals_cache")
    if totals_ret is None:
        return {
            "queued_requests": 0,
            "queued_text_requests": 0,
            f"queued_{hv.thing_names['image']}": 0,
            f"queued_{hv.thing_names['text']}": 0,
            "queued_forms": 0,
        }
    return json.loads(totals_ret)


def get_organized_wps_by_model(wp_class):
    org = {}
    # TODO: Offload the sorting to the DB through join() + SELECT statements
    all_wps = (
        db.session.query(wp_class)
        .filter(
            wp_class.active == True,  # noqa E712
            wp_class.faulted == False,  # noqa E712
            wp_class.n >= 1,
        )
        .all()
    )  # TODO this can likely be improved
    for wp in all_wps:
        # Each wp we have will be placed on the list for each of it allowed models (in case it's selected multiple)
        # This will inflate the overall expected times, but it shouldn't be by much.
        # I don't see a way to do this calculation more accurately though
        for model in wp.get_model_names():
            if "horde_special" in model:
                continue
            if model not in org:
                org[model] = []
            org[model].append(wp)
    return org


def count_things_per_model(wp_class):
    things_per_model = {}
    jobs_per_model = {}
    org = get_organized_wps_by_model(wp_class)
    for model in org:
        for wp in org[model]:
            current_wp_queue = wp.n + wp.count_processing_gens()["processing"]
            if current_wp_queue > 0:
                things_per_model[model] = things_per_model.get(model, 0) + wp.things
                jobs_per_model[model] = jobs_per_model.get(model, 0) + current_wp_queue
        things_per_model[model] = round(things_per_model.get(model, 0), 2)
    return things_per_model, jobs_per_model


def count_things_for_specific_model(wp_class, procgen_class, model_name):
    things = {model_name: 0}
    jobs = {model_name: 0}
    all_wps_query = (
        db.session.query(
            wp_class.id.label("wp_id"),
            wp_class.n,
            wp_class.things,
            procgen_class.id.label("procgen_id"),
        )
        .join(
            WPModels,
        )
        .outerjoin(
            procgen_class,
        )
        .filter(
            wp_class.active == True,  # noqa E712
            wp_class.faulted == False,  # noqa E712
            wp_class.n >= 0,
            WPModels.model == model_name,
            or_(
                procgen_class.id == None,  # noqa E712
                and_(
                    procgen_class.generation == None,  # noqa E712
                    procgen_class.cancelled == False,  # noqa E712
                    procgen_class.faulted == False,  # noqa E712
                ),
            ),
        )
    )
    all_wps = all_wps_query.all()
    seen_wps = set()
    for wp in all_wps:
        current_wp_queue = 0
        if wp.wp_id not in seen_wps:
            current_wp_queue = wp.n
            seen_wps.add(wp.wp_id)
        if wp.procgen_id:
            current_wp_queue += 1
        things[model_name] += wp.things * current_wp_queue
        jobs[model_name] += current_wp_queue
    things[model_name] = round(things[model_name], 2)
    return things, jobs


@logger.catch(reraise=True)
def get_sorted_wp_filtered_to_worker(worker, models_list=None, blacklist=None, priority_user_ids=None, page=0):
    import time as _time

    t0 = _time.monotonic()
    # This is just the top 3 - Adjusted method to send ImageWorker object. Filters to add.
    # TODO: Filter by ImageWorker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.ImageWorker.gan_generate())
    PER_PAGE = 10  # how many requests we're picking up to filter further
    # The model constraint is a semi-join: joining wp_models returns one row per
    # matching model, and the page LIMIT below counts joined rows, so a WP
    # naming several of the worker's models would consume several page slots as
    # duplicates of itself.
    wp_serves_model = (
        db.session.query(WPModels.id).filter(WPModels.wp_id == ImageWaitingPrompt.id, WPModels.model.in_(models_list)).exists()
    )
    wp_names_any_model = db.session.query(WPModels.id).filter(WPModels.wp_id == ImageWaitingPrompt.id).exists()
    # Worker targeting is evaluated per WP, never per targeting row: joining
    # wp_allowed_workers admits a blacklisted worker whenever the blacklist
    # names anyone else, because the other rows satisfy a row-level
    # ``worker_id != x`` predicate.
    wp_targets_this_worker = (
        db.session.query(WPAllowedWorkers.id)
        .filter(WPAllowedWorkers.wp_id == ImageWaitingPrompt.id, WPAllowedWorkers.worker_id == worker.id)
        .exists()
    )
    wp_has_worker_targets = db.session.query(WPAllowedWorkers.id).filter(WPAllowedWorkers.wp_id == ImageWaitingPrompt.id).exists()
    final_wp_list = (
        db.session.query(ImageWaitingPrompt)
        .options(noload(ImageWaitingPrompt.processing_gens))
        .filter(
            ImageWaitingPrompt.n > 0,
            ImageWaitingPrompt.active == True,  # noqa E712
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.expiry > datetime.utcnow(),
            ImageWaitingPrompt.width * ImageWaitingPrompt.height <= worker.max_pixels,
            or_(
                wp_serves_model,
                and_(
                    ~wp_names_any_model,
                    not any("horde_special" in mname for mname in models_list),
                    "SDXL_beta::stability.ai#6901" not in models_list,
                ),
            ),
            or_(
                ImageWaitingPrompt.source_image == None,  # noqa E712
                worker.allow_img2img == True,  # noqa E712
            ),
            or_(
                ImageWaitingPrompt.source_processing.not_in(["inpainting", "outpainting"]),
                worker.allow_painting == True,  # noqa E712
            ),
            or_(
                ImageWaitingPrompt.extra_source_images == None,  # noqa E712
                check_bridge_capability("extra_source_images", worker.bridge_agent),
            ),
            or_(
                ImageWaitingPrompt.safe_ip == True,  # noqa E712
                worker.allow_unsafe_ipaddr == True,  # noqa E712
            ),
            or_(
                ImageWaitingPrompt.nsfw == False,  # noqa E712
                worker.nsfw == True,  # noqa E712
            ),
            or_(
                check_bridge_capability("r2", worker.bridge_agent),
                ImageWaitingPrompt.r2 == False,  # noqa E712
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("loras")),
                and_(
                    worker.allow_lora == True,  # noqa E712
                    check_bridge_capability("lora", worker.bridge_agent),
                ),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("tis")),
                check_bridge_capability("textual_inversion", worker.bridge_agent),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("post-processing")),
                and_(
                    worker.allow_post_processing == True,  # noqa E712
                    check_bridge_capability("post-processing", worker.bridge_agent),
                ),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("control_type")),
                and_(
                    worker.allow_controlnet == True,  # noqa E712
                    check_bridge_capability("controlnet", worker.bridge_agent),
                    or_(
                        ImageWaitingPrompt.params["control_type"].astext.in_(LEGACY_IMAGE_CONTROL_TYPES),
                        and_(
                            check_bridge_capability("extended_controlnet", worker.bridge_agent),
                            worker.allow_extended_controlnet == True,  # noqa E712
                        ),
                    ),
                ),
            ),
            # A schedule the legacy karras flag cannot express only reaches a bridge that reads the field;
            # anything older would silently sample on a different schedule than the one requested.
            or_(
                ImageWaitingPrompt.params["scheduler"].astext.notin_(EXTENDED_SCHEDULERS),
                ImageWaitingPrompt.params["scheduler"].is_(None),
                check_bridge_capability("scheduler", worker.bridge_agent),
            ),
            or_(
                ImageWaitingPrompt.params["scheduler"].astext.notin_(SIGMA_GENERATOR_SCHEDULERS),
                ImageWaitingPrompt.params["scheduler"].is_(None),
                check_bridge_capability("sigma_generators", worker.bridge_agent),
            ),
            or_(
                and_(*[ImageWaitingPrompt.params[field].astext.is_(None) for field in SOLVER_KNOB_PARAMS]),
                check_bridge_capability("solver_options", worker.bridge_agent),
            ),
            or_(
                ImageWaitingPrompt.params[FLOW_SHIFT_PARAM].astext.is_(None),
                check_bridge_capability("flow_shift", worker.bridge_agent),
            ),
            or_(
                worker.speed >= 500000,  # 0.5 MPS/s
                ImageWaitingPrompt.slow_workers == True,  # noqa E712
            ),
            or_(
                worker.extra_slow_worker is False,
                and_(
                    worker.extra_slow_worker is True,
                    ImageWaitingPrompt.extra_slow_workers.is_(True),
                ),
            ),
            or_(
                not_(ImageWaitingPrompt.params.has_key("transparent")),
                ImageWaitingPrompt.params["transparent"].astext.cast(Boolean).is_(False),
                and_(
                    check_bridge_capability("layer_diffuse", worker.bridge_agent),
                    worker.allow_sdxl_controlnet == True,  # noqa E712
                ),
            ),
        )
    )
    # logger.debug(final_wp_list)
    if priority_user_ids:
        final_wp_list = final_wp_list.filter(ImageWaitingPrompt.user_id.in_(priority_user_ids))
        final_wp_list = final_wp_list.filter(
            # Workers in maintenance can still pick up their owner or their friends
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id.in_(priority_user_ids),
            ),
            or_(
                ~wp_has_worker_targets,
                and_(
                    ImageWaitingPrompt.worker_blacklist.is_(False),
                    wp_targets_this_worker,
                ),
                and_(
                    ImageWaitingPrompt.worker_blacklist.is_(True),
                    ~wp_targets_this_worker,
                ),
            ),
        )
    else:
        final_wp_list = final_wp_list.filter(
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id == worker.user_id,
            ),
        )
        # If HORDE_REQUIRE_MATCHED_TARGETING is set to 1, we disable using WPAllowedWorkers
        # Targeted requests will only be picked up in the condition above as it will include the
        # filter to ensure the worker also has that user as a priority
        if os.getenv("HORDE_REQUIRE_MATCHED_TARGETING", "0") == "1":
            final_wp_list = final_wp_list.filter(
                or_(
                    ~wp_has_worker_targets,
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(True),
                        ~wp_targets_this_worker,
                    ),
                ),
            )
        else:
            final_wp_list = final_wp_list.filter(
                or_(
                    ~wp_has_worker_targets,
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(False),
                        wp_targets_this_worker,
                    ),
                    and_(
                        ImageWaitingPrompt.worker_blacklist.is_(True),
                        ~wp_targets_this_worker,
                    ),
                ),
            )

    # logger.debug(final_wp_list)
    final_wp_list = (
        final_wp_list.order_by(ImageWaitingPrompt.extra_priority.desc(), ImageWaitingPrompt.created.asc())
        .offset(PER_PAGE * page)
        .limit(PER_PAGE)
    )
    with logfire.span(
        "horde.db.get_sorted_wp",
        worker_id=str(worker.id),
        page=page,
        has_priority=priority_user_ids is not None,
    ):
        results = final_wp_list.populate_existing().with_for_update(skip_locked=True, of=ImageWaitingPrompt).all()
    pop_query_duration.record(_time.monotonic() - t0, {"horde.page": page})
    return results


def count_skipped_image_wp(worker, models_list=None, blacklist=None, priority_user_ids=None):
    ## Consolidated into a single query with conditional aggregation (replaces 15+ separate count queries).
    if models_list is None:
        models_list = []

    bridge_agent = worker.bridge_agent
    can_img2img = check_bridge_capability("img2img", bridge_agent)
    can_inpainting = check_bridge_capability("inpainting", bridge_agent)
    can_lora = check_bridge_capability("lora", bridge_agent)
    can_ti = check_bridge_capability("textual_inversion", bridge_agent)
    can_pp = check_bridge_capability("post-processing", bridge_agent)
    can_controlnet = check_bridge_capability("controlnet", bridge_agent)
    can_extended_controlnet = check_bridge_capability("extended_controlnet", bridge_agent)
    can_hires = check_bridge_capability("hires_fix", bridge_agent)
    can_return_ctrl = check_bridge_capability("return_control_map", bridge_agent)
    can_tiling = check_bridge_capability("tiling", bridge_agent)
    can_layer_diffuse = check_bridge_capability("layer_diffuse", bridge_agent)
    can_scheduler_field = check_bridge_capability("scheduler", bridge_agent)
    can_sigma_generators = check_bridge_capability("sigma_generators", bridge_agent)
    can_solver_options = check_bridge_capability("solver_options", bridge_agent)
    can_flow_shift = check_bridge_capability("flow_shift", bridge_agent)

    available_samplers = get_supported_samplers(bridge_agent, karras=False)
    available_karras_samplers = get_supported_samplers(bridge_agent, karras=True)

    # Base filters (shared across all counts)
    base_filters = [
        ImageWaitingPrompt.n > 0,
        ImageWaitingPrompt.active == True,  # noqa E712
        ImageWaitingPrompt.faulted == False,  # noqa E712
        ImageWaitingPrompt.expiry > datetime.utcnow(),
    ]

    # Build all conditional count expressions
    count_exprs = {}

    def count_distinct_wp(condition):
        # Distinct-by-WP avoids overcounting from WPModels join fan-out.
        return func.count(func.distinct(case((condition, ImageWaitingPrompt.id), else_=None)))

    # Worker targeting is evaluated per WP through EXISTS rather than through a
    # wp_allowed_workers join, matching the pop candidate selection semantics.
    wp_targets_this_worker = (
        db.session.query(WPAllowedWorkers.id)
        .filter(WPAllowedWorkers.wp_id == ImageWaitingPrompt.id, WPAllowedWorkers.worker_id == worker.id)
        .exists()
    )
    wp_has_worker_targets = db.session.query(WPAllowedWorkers.id).filter(WPAllowedWorkers.wp_id == ImageWaitingPrompt.id).exists()

    # models: WP specifies models that worker doesn't serve
    count_exprs["models"] = count_distinct_wp(and_(WPModels.model.not_in(models_list), WPModels.id != None))  # noqa E712

    # worker_id: WP targets specific workers (allowlist/blocklist) in a way that
    # excludes this worker. Under HORDE_REQUIRE_MATCHED_TARGETING the general
    # pass serves no allowlist WP at all (they are only picked up through
    # priority matching), so every allowlist WP counts as skipped there.
    if priority_user_ids or os.getenv("HORDE_REQUIRE_MATCHED_TARGETING", "0") != "1":
        excluded_by_targeting = or_(
            and_(
                ImageWaitingPrompt.worker_blacklist.is_(False),
                ~wp_targets_this_worker,
            ),
            and_(
                ImageWaitingPrompt.worker_blacklist.is_(True),
                wp_targets_this_worker,
            ),
        )
    else:
        excluded_by_targeting = or_(
            ImageWaitingPrompt.worker_blacklist.is_(False),
            and_(
                ImageWaitingPrompt.worker_blacklist.is_(True),
                wp_targets_this_worker,
            ),
        )
    count_exprs["worker_id"] = count_distinct_wp(and_(wp_has_worker_targets, excluded_by_targeting))

    # max_pixels
    count_exprs["max_pixels"] = count_distinct_wp(ImageWaitingPrompt.width * ImageWaitingPrompt.height >= worker.max_pixels)

    # img2img (only counted if worker can't do it)
    if worker.allow_img2img is False or not can_img2img:
        count_exprs["_img2img_raw"] = count_distinct_wp(ImageWaitingPrompt.source_image != None)  # noqa E712

    # painting (only counted if worker can't do it)
    if worker.allow_painting is False or not can_inpainting:
        count_exprs["_painting_raw"] = count_distinct_wp(ImageWaitingPrompt.source_processing.in_(["inpainting", "outpainting"]))

    # unsafe_ip
    if worker.allow_unsafe_ipaddr is False:
        count_exprs["unsafe_ip"] = count_distinct_wp(ImageWaitingPrompt.safe_ip == False)  # noqa E712

    # nsfw
    if worker.nsfw is False:
        count_exprs["nsfw"] = count_distinct_wp(ImageWaitingPrompt.nsfw == True)  # noqa E712

    # lora
    if worker.allow_lora is False or not can_lora:
        count_exprs["_lora_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("loras"))

    # TI
    if not can_ti:
        count_exprs["_ti_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("tis"))

    # post-processing
    if worker.allow_post_processing is False or not can_pp:
        count_exprs["_pp_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("post-processing"))

    # controlnet
    if worker.allow_controlnet is False or not can_controlnet:
        count_exprs["_controlnet_raw"] = count_distinct_wp(ImageWaitingPrompt.params.has_key("control_type"))
    elif not can_extended_controlnet:
        # A controlnet-capable but pre-extended worker cannot render control types outside the classic set.
        count_exprs["_controlnet_extended_bridge_raw"] = count_distinct_wp(
            and_(
                ImageWaitingPrompt.params.has_key("control_type"),
                ImageWaitingPrompt.params["control_type"].astext.notin_(LEGACY_IMAGE_CONTROL_TYPES),
            ),
        )
    elif worker.allow_extended_controlnet is False:
        # An extended-capable worker that opted out of extended types skips them by worker choice.
        count_exprs["_controlnet_extended_choice_raw"] = count_distinct_wp(
            and_(
                ImageWaitingPrompt.params.has_key("control_type"),
                ImageWaitingPrompt.params["control_type"].astext.notin_(LEGACY_IMAGE_CONTROL_TYPES),
            ),
        )

    # performance (slow workers)
    if worker.speed <= 500000:
        count_exprs["_perf_slow"] = count_distinct_wp(ImageWaitingPrompt.slow_workers == False)  # noqa E712

    # performance (extra slow workers)
    if worker.extra_slow_worker is True:
        count_exprs["_perf_extra_slow"] = count_distinct_wp(ImageWaitingPrompt.extra_slow_workers == False)  # noqa E712

    # untrusted
    if worker.user.trusted is False:
        count_exprs["untrusted"] = count_distinct_wp(ImageWaitingPrompt.trusted_workers == True)  # noqa E712

    # bridge_version (sampler + capability checks)
    bv_conditions = []
    bv_conditions.append(
        and_(
            ImageWaitingPrompt.params["sampler_name"].astext.not_in(available_samplers),
            ImageWaitingPrompt.params["karras"].astext.cast(Boolean).is_(False),
        ),
    )
    bv_conditions.append(
        and_(
            ImageWaitingPrompt.params["sampler_name"].astext.not_in(available_karras_samplers),
            ImageWaitingPrompt.params["karras"].astext.cast(Boolean).is_(True),
        ),
    )
    if not can_hires:
        bv_conditions.append(ImageWaitingPrompt.params["hires_fix"].astext.cast(Boolean).is_(True))
    if not can_return_ctrl:
        bv_conditions.append(ImageWaitingPrompt.params["return_control_map"].astext.cast(Boolean).is_(True))
    if not can_tiling:
        bv_conditions.append(ImageWaitingPrompt.params["tiling"].astext.cast(Boolean).is_(True))
    if not can_layer_diffuse:
        bv_conditions.append(ImageWaitingPrompt.params["transparent"].astext.cast(Boolean).is_(True))
    if not can_scheduler_field:
        # Attributed to bridge_version rather than a worker choice: there is no operator flag for this,
        # the bridge simply predates the field.
        bv_conditions.append(ImageWaitingPrompt.params["scheduler"].astext.in_(EXTENDED_SCHEDULERS))
    if not can_sigma_generators:
        bv_conditions.append(ImageWaitingPrompt.params["scheduler"].astext.in_(SIGMA_GENERATOR_SCHEDULERS))
    if not can_solver_options:
        bv_conditions.extend(ImageWaitingPrompt.params[field].astext.is_not(None) for field in SOLVER_KNOB_PARAMS)
    if not can_flow_shift:
        bv_conditions.append(ImageWaitingPrompt.params[FLOW_SHIFT_PARAM].astext.is_not(None))

    count_exprs["_bv_sampler"] = count_distinct_wp(or_(*bv_conditions))

    # Execute single query
    query = (
        db.session.query(*count_exprs.values())
        .select_from(ImageWaitingPrompt)
        .outerjoin(WPModels, ImageWaitingPrompt.id == WPModels.wp_id)
        .filter(*base_filters)
    )

    # Keep skipped-count filtering behavior aligned with pop candidate selection.
    if priority_user_ids:
        query = query.filter(ImageWaitingPrompt.user_id.in_(priority_user_ids))
        query = query.filter(
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id.in_(priority_user_ids),
            ),
        )
    else:
        query = query.filter(
            or_(
                worker.maintenance == False,  # noqa E712
                ImageWaitingPrompt.user_id == worker.user_id,
            ),
        )

    row = query.one()
    raw = dict(zip(count_exprs.keys(), row))

    # Now map raw results to the return dictionary with the bridge_version aggregation logic
    ret_dict = {}
    bridge_version_count = 0

    if raw.get("models", 0) > 0:
        ret_dict["models"] = raw["models"]
    if raw.get("worker_id", 0) > 0:
        ret_dict["worker_id"] = raw["worker_id"]
    if raw.get("max_pixels", 0) > 0:
        ret_dict["max_pixels"] = raw["max_pixels"]

    # img2img: attribute to setting or bridge depending on which is the cause
    img2img_count = raw.get("_img2img_raw", 0) or 0
    if img2img_count > 0:
        if worker.allow_img2img is False:
            ret_dict["img2img"] = img2img_count
        else:
            bridge_version_count += img2img_count

    # painting
    painting_count = raw.get("_painting_raw", 0) or 0
    if painting_count > 0:
        if worker.allow_painting is False:
            ret_dict["painting"] = painting_count
        else:
            bridge_version_count += painting_count

    if raw.get("unsafe_ip", 0) > 0:
        ret_dict["unsafe_ip"] = raw["unsafe_ip"]
    if raw.get("nsfw", 0) > 0:
        ret_dict["nsfw"] = raw["nsfw"]

    # lora
    lora_count = raw.get("_lora_raw", 0) or 0
    if lora_count > 0:
        if worker.allow_lora is False:
            ret_dict["lora"] = lora_count
        else:
            bridge_version_count += lora_count

    # TI
    ti_count = raw.get("_ti_raw", 0) or 0
    if ti_count > 0:
        bridge_version_count += ti_count

    # post-processing
    pp_count = raw.get("_pp_raw", 0) or 0
    if pp_count > 0:
        if worker.allow_post_processing is False:
            ret_dict["post-processing"] = pp_count
        else:
            bridge_version_count += pp_count

    # controlnet
    controlnet_count = raw.get("_controlnet_raw", 0) or 0
    if controlnet_count > 0:
        if worker.allow_controlnet is False:
            ret_dict["controlnet"] = controlnet_count
        else:
            bridge_version_count += controlnet_count
    extended_controlnet_bridge_count = raw.get("_controlnet_extended_bridge_raw", 0) or 0
    if extended_controlnet_bridge_count > 0:
        bridge_version_count += extended_controlnet_bridge_count
    extended_controlnet_choice_count = raw.get("_controlnet_extended_choice_raw", 0) or 0
    if extended_controlnet_choice_count > 0:
        ret_dict["controlnet"] = ret_dict.get("controlnet", 0) + extended_controlnet_choice_count

    # performance
    perf_count = (raw.get("_perf_slow", 0) or 0) + (raw.get("_perf_extra_slow", 0) or 0)
    if perf_count > 0:
        ret_dict["performance"] = perf_count

    if raw.get("untrusted", 0) > 0:
        ret_dict["untrusted"] = raw["untrusted"]

    # bridge_version sampler/capability
    bv_sampler = raw.get("_bv_sampler", 0) or 0
    bridge_version_count += bv_sampler
    if bridge_version_count > 0:
        ret_dict["bridge_version"] = bridge_version_count

    for key in [
        "bridge_version",
        "untrusted",
        "performance",
        "controlnet",
        "post-processing",
        "lora",
        "nsfw",
        "unsafe_ip",
        "painting",
        "img2img",
        "worker_id",
        "models",
    ]:
        if key not in ret_dict:
            ret_dict[key] = 0
    return ret_dict


def get_sorted_forms_filtered_to_worker(
    worker,
    forms_list=None,
    priority_user_ids=None,
    excluded_forms=None,
    annotation_types=None,
):
    if forms_list is None:
        forms_list = []
    if annotation_types is None:
        annotation_types = []
    final_interrogation_query = (
        db.session.query(InterrogationForms)
        .join(Interrogation)
        .filter(
            InterrogationForms.state == State.WAITING,
            InterrogationForms.name.in_(forms_list),
            or_(
                InterrogationForms.name != "annotation",
                InterrogationForms.payload["control_type"].astext.in_(annotation_types),
            ),
            InterrogationForms.expiry == None,  # noqa E712
            Interrogation.source_image != None,  # noqa E712
            Interrogation.image_tiles <= worker.max_power,
            or_(
                Interrogation.safe_ip == True,  # noqa E712
                worker.allow_unsafe_ipaddr == True,  # noqa E712
            ),
            or_(
                worker.maintenance == False,  # noqa E712
                Interrogation.user_id == worker.user_id,
            ),
            or_(
                worker.speed < 10,  # 10 seconds per form
                Interrogation.slow_workers == True,  # noqa E712
            ),
        )
        .order_by(Interrogation.extra_priority.desc(), Interrogation.created.asc())
    )
    if priority_user_ids is not None:
        final_interrogation_query = final_interrogation_query.filter(Interrogation.user_id.in_(priority_user_ids))
    # We use this to not retrieve already retrieved with priority_users
    retrieve_limit = 100
    if excluded_forms is not None:
        excluded_form_ids = [f.id for f in excluded_forms]
        # We only want to retrieve 100 requests, so we reduce the amount to retrieve from non-prioritized
        # requests by the prioritized requests.
        retrieve_limit -= len(excluded_form_ids)
        if retrieve_limit <= 0:
            return []
        final_interrogation_query = final_interrogation_query.filter(InterrogationForms.id.not_in(excluded_form_ids))
    return final_interrogation_query.limit(retrieve_limit).all()


# Returns the queue position of the provided WP based on kudos
# Also returns the amount of things until the wp is generated
# Also returns the amount of different gens queued
# In-process cache for pre-computed queue positions (refreshed at most once per second)
_wp_queue_positions_cache = {"image": {}, "text": {}}
_wp_queue_positions_time = {"image": 0.0, "text": 0.0}


def get_wp_queue_stats(wp):
    if not wp.needs_gen():
        return (-1, 0, 0)
    wp_type = wp.wp_type
    now = time.time()
    # Refresh in-process cache at most once per second per wp_type
    if now - _wp_queue_positions_time.get(wp_type, 0) > 1:
        cached_positions = hr.horde_r_get(f"{wp_type}_wp_queue_positions")
        if cached_positions is not None:
            try:
                parsed = json.loads(cached_positions)
                _wp_queue_positions_cache[wp_type] = parsed
                _wp_queue_positions_time[wp_type] = now
            except (TypeError, ValueError):
                pass
    positions = _wp_queue_positions_cache.get(wp_type, {})
    wp_stats = positions.get(str(wp.id))
    if wp_stats is not None:
        return tuple(wp_stats)
    # Check if we have positions data at all; if so, WP is not in the queue
    if positions:
        return (-1, 0, 0)
    # Fall back to legacy computation if pre-computed positions unavailable
    with logfire.span("horde.db.get_wp_queue_stats", wp_id=str(wp.id), wp_type=wp.wp_type):
        things_ahead_in_queue = 0
        n_ahead_in_queue = 0
        priority_sorted_list = retrieve_prioritized_wp_queue(wp.wp_type)
        if priority_sorted_list is None:
            logger.warning(
                "Cached WP priority query does not exist. Falling back to direct DB query. Please check thread on primary!",
            )
            priority_sorted_list = query_prioritized_wps(wp.wp_type)
        thing_divisor = hv.thing_divisors[wp.wp_type]
        for riter in range(len(priority_sorted_list)):
            iter_wp = priority_sorted_list[riter]
            queued_things = round(iter_wp.things * iter_wp.n / thing_divisor, 2)
            things_ahead_in_queue += queued_things
            n_ahead_in_queue += iter_wp.n
            if iter_wp.id == wp.id:
                things_ahead_in_queue = round(things_ahead_in_queue, 2)
                return (riter, things_ahead_in_queue, n_ahead_in_queue)
        return (-1, 0, 0)


def get_wp_by_id(wp_id, lite=False):
    try:
        wp_uuid = uuid.UUID(wp_id)
    except ValueError:
        logger.debug(f"Non-UUID wp_id sent: '{wp_id}'.")
        return None
    if SQLITE_MODE:
        wp_uuid = str(wp_uuid)
    # lite version does not pull ProcGens
    if lite:
        query = db.session.query(ImageWaitingPrompt).options(noload(ImageWaitingPrompt.processing_gens))
    else:
        query = db.session.query(ImageWaitingPrompt)
    return query.filter_by(id=wp_uuid).first()


def get_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError:
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    # The submit settlement always walks procgen -> wp -> requesting user and
    # procgen -> worker -> owning user, so loading them here folds four lazy
    # SELECT round trips into the lookup itself.
    return (
        db.session.query(ImageProcessingGeneration)
        .options(
            joinedload(ImageProcessingGeneration.wp).joinedload(ImageWaitingPrompt.user),
            joinedload(ImageProcessingGeneration.worker).joinedload(ImageWorker.user),
        )
        .filter_by(id=procgen_uuid)
        .first()
    )


def get_interrogation_by_id(i_id):
    try:
        i_uuid = uuid.UUID(i_id)
    except ValueError:
        logger.debug(f"Non-UUID i_id sent: '{i_id}'.")
        return None
    if SQLITE_MODE:
        i_uuid = str(i_uuid)
    return db.session.query(Interrogation).filter_by(id=i_uuid).first()


def get_form_by_id(form_id):
    try:
        form_uuid = uuid.UUID(form_id)
    except ValueError:
        logger.debug(f"Non-UUID form_id sent: '{form_id}'.")
        return None
    if SQLITE_MODE:
        form_uuid = str(form_uuid)
    return db.session.query(InterrogationForms).filter_by(id=form_uuid).first()


def get_all_wps():
    return (
        db.session.query(ImageWaitingPrompt)
        .filter(
            ImageWaitingPrompt.active == True,  # noqa E712
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_all_active_wps():
    return (
        db.session.query(ImageWaitingPrompt)
        .filter(
            ImageWaitingPrompt.active == True,  # noqa E712
            ImageWaitingPrompt.faulted == False,  # noqa E712
            ImageWaitingPrompt.n > 0,
            ImageWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


# TODO: Convert below three functions into a general "cached db request" (or something) class
# Which I can reuse to cache the results of other requests
def retrieve_worker_performances(worker_type=ImageWorker):
    avg_perf = db.session.query(func.avg(WorkerPerformance.performance)).join(worker_type).scalar()
    avg_perf = 0 if avg_perf is None else round(avg_perf, 2)
    return avg_perf  # noqa RET504


def refresh_worker_performances_cache(request_type="image"):
    ret_dict = {
        "image": retrieve_worker_performances(ImageWorker),
        "text": retrieve_worker_performances(TextWorker),
    }
    try:
        hr.horde_r_setex("worker_performances_avg_cache", timedelta(seconds=30), ret_dict["image"])
        hr.horde_r_setex(
            "text_worker_performances_avg_cache",
            timedelta(seconds=30),
            ret_dict["text"],
        )
    except Exception as err:
        logger.debug(f"Error when trying to set worker performances cache: {err}. Retrieving from DB.")
    return ret_dict[request_type]


def get_request_avg(request_type="image"):
    if hr.horde_r is None:
        return retrieve_worker_performances(WORKER_CLASS_MAP[request_type])
    if request_type == "image":
        perf_cache = hr.horde_r_get("worker_performances_avg_cache")
    else:
        perf_cache = hr.horde_r_get("text_worker_performances_avg_cache")
    if not perf_cache:
        return refresh_worker_performances_cache(request_type)
    return float(perf_cache)


def _waiting_prompt_has_inflight_generation(waiting_prompt: WaitingPrompt) -> bool:
    """Return whether a real generation is currently serving the request."""

    if waiting_prompt.wp_type not in {"image", "text"}:
        return False
    return (
        db.session.query(ProcessingGeneration.id)
        .filter(
            ProcessingGeneration.wp_id == waiting_prompt.id,
            ProcessingGeneration.generation.is_(None),
            ProcessingGeneration.faulted.is_(False),
            ProcessingGeneration.fake.is_(False),
        )
        .first()
        is not None
    )


def _iter_eligible_workers_for_request(
    waiting_prompt: WaitingPrompt,
) -> Iterator[ImageWorker | TextWorker]:
    """Yield recently active workers that pass every known request gate."""

    with logfire.span(
        "horde.db.get_worker_availability_for_request",
        wp_id=str(waiting_prompt.id),
        wp_type=waiting_prompt.wp_type,
    ):
        if waiting_prompt.faulted:
            return
        if waiting_prompt.expiry < datetime.utcnow():
            return
        worker_class: type[ImageWorker] | type[TextWorker]
        if isinstance(waiting_prompt, ImageWaitingPrompt):
            worker_class = ImageWorker
        elif isinstance(waiting_prompt, TextWaitingPrompt):
            worker_class = TextWorker
        else:
            return
        models_list = waiting_prompt.get_model_names()
        worker_ids = waiting_prompt.get_worker_ids()
        # The model constraint is a semi-join rather than an outer join: joining
        # worker_models returns one full worker+user row per matching model, so
        # a request allowing N models multiplies every candidate row N-fold
        # before the DISTINCT-free scan below iterates them.
        serves_requested_model = (
            db.session.query(WorkerModel.id)
            .filter(
                WorkerModel.worker_id == Worker.id,
                WorkerModel.model.in_(models_list),
            )
            .exists()
        )
        final_worker_list = (
            db.session.query(worker_class)
            .options(
                noload(Worker.performance),
                noload(Worker.suspicions),
                noload(Worker.stats),
                # Eagerly load relationships accessed by can_generate() to avoid N+1 queries
                selectinload(Worker.blacklist),
                contains_eager(Worker.user).selectinload(User.roles),
                selectinload(Worker.models),
            )
            .join(
                User,
            )
            .filter(
                Worker.last_check_in > datetime.utcnow() - timedelta(seconds=300),
                or_(
                    Worker.maintenance.is_(False),
                    and_(
                        Worker.maintenance.is_(True),
                        waiting_prompt.user_id == Worker.user_id,
                    ),
                ),
                or_(
                    Worker.paused.is_(False),
                    and_(
                        Worker.paused.is_(True),
                        waiting_prompt.user_id == Worker.user_id,
                    ),
                ),
            )
        )
        if waiting_prompt.trusted_workers:
            final_worker_list = final_worker_list.filter(User.trusted.is_(True))
        if not waiting_prompt.safe_ip:
            final_worker_list = final_worker_list.filter(Worker.allow_unsafe_ipaddr.is_(True))
        if waiting_prompt.nsfw:
            final_worker_list = final_worker_list.filter(Worker.nsfw.is_(True))
        if worker_ids:
            if waiting_prompt.worker_blacklist:
                final_worker_list = final_worker_list.filter(Worker.id.not_in(worker_ids))
            else:
                final_worker_list = final_worker_list.filter(Worker.id.in_(worker_ids))
        if models_list:
            final_worker_list = final_worker_list.filter(serves_requested_model)
        if isinstance(waiting_prompt, ImageWaitingPrompt):
            final_worker_list = final_worker_list.filter(
                waiting_prompt.width * waiting_prompt.height <= ImageWorker.max_pixels,
                # or_(
                #     'tis' not in waiting_prompt.params,
                #     and_(
                #         #TODO: Create an sql function I can call to check the worker bridge capabilities
                #         'tis' in waiting_prompt.params,
                #     ),
                # ),
            )
            if waiting_prompt.source_image is not None:
                final_worker_list = final_worker_list.filter(ImageWorker.allow_img2img.is_(True))
            if not waiting_prompt.slow_workers:
                final_worker_list = final_worker_list.filter(ImageWorker.speed >= 500000)
            if "loras" in waiting_prompt.params:
                final_worker_list = final_worker_list.filter(ImageWorker.allow_lora.is_(True))
        elif isinstance(waiting_prompt, TextWaitingPrompt):
            final_worker_list = final_worker_list.filter(
                waiting_prompt.max_length <= TextWorker.max_length,
                waiting_prompt.max_context_length <= TextWorker.max_context_length,
            )
            if not waiting_prompt.slow_workers:
                final_worker_list = final_worker_list.filter(TextWorker.speed >= 2)
        if isinstance(waiting_prompt, TextWaitingPrompt):
            final_worker_list = final_worker_list.options(selectinload(TextWorker.softprompts))
        for worker in final_worker_list.all():
            if isinstance(worker, ImageWorker):
                can_generate = worker.can_generate_with_model_names(
                    waiting_prompt,
                    [worker_model.model for worker_model in worker.models],
                )
            elif isinstance(worker, TextWorker):
                can_generate = worker.can_generate_with_softprompt_names(
                    waiting_prompt,
                    [softprompt.softprompt for softprompt in worker.softprompts],
                    model_names=[worker_model.model for worker_model in worker.models],
                )
            else:
                continue
            if can_generate[0]:
                yield worker


def get_worker_availability_for_request(waiting_prompt: WaitingPrompt) -> RequestWorkerAvailability:
    """Return the recently active worker capacity eligible for a request.

    Args:
        waiting_prompt: Request whose worker capacity should be measured.

    Returns:
        Exact worker and advertised-thread counts after the same SQL and
        Python capability gates used by the existing possibility check.

    Performance:
        Results are cached per request for 60 seconds. A cache miss evaluates
        every SQL candidate because an exact count cannot stop at the first
        valid worker.
    """

    # In-flight state is always read live so cached zero capacity cannot
    # contradict a generation that started after the cache was populated.
    has_inflight_generation = _waiting_prompt_has_inflight_generation(waiting_prompt)
    cached_availability = hr.horde_r_get(f"wp_availability_{waiting_prompt.id}")
    if cached_availability is not None:
        try:
            parsed_availability = json.loads(cached_availability)
            return RequestWorkerAvailability(
                worker_count=int(parsed_availability["workers"]),
                thread_count=int(parsed_availability["threads"]),
                has_inflight_generation=has_inflight_generation,
            )
        except (KeyError, TypeError, ValueError):
            logger.debug(f"Ignoring malformed worker availability cache for request {waiting_prompt.id}")

    eligible_workers = list(_iter_eligible_workers_for_request(waiting_prompt))
    availability = RequestWorkerAvailability(
        worker_count=len(eligible_workers),
        thread_count=sum(max(worker.threads, 1) for worker in eligible_workers),
        has_inflight_generation=has_inflight_generation,
    )
    worker_found = availability.worker_count > 0
    hr.horde_r_setex(
        f"wp_validity_{waiting_prompt.id}",
        timedelta(seconds=60),
        int(worker_found),
    )
    hr.horde_r_setex(
        f"wp_availability_{waiting_prompt.id}",
        timedelta(seconds=60),
        json.dumps(
            {
                "workers": availability.worker_count,
                "threads": availability.thread_count,
            },
        ),
    )
    return availability


def wp_has_valid_workers(wp: WaitingPrompt) -> bool:
    """Return whether a request has an in-flight generation or an eligible worker.

    Args:
        wp: Request whose current feasibility should be checked.

    Returns:
        True when the request is already processing or at least one recently
        active worker passes all known dispatch gates.
    """

    cached_validity = hr.horde_r_get(f"wp_validity_{wp.id}")
    if cached_validity is not None and bool(int(cached_validity)):
        return True
    request_has_started = wp.jobs > 0 and wp.n < wp.jobs
    if request_has_started and _waiting_prompt_has_inflight_generation(wp):
        return True
    if cached_validity is not None:
        return bool(int(cached_validity))
    worker_found = next(_iter_eligible_workers_for_request(wp), None) is not None
    hr.horde_r_setex(
        f"wp_validity_{wp.id}",
        timedelta(seconds=60),
        int(worker_found),
    )
    return worker_found


@logger.catch(reraise=True)
def retrieve_prioritized_wp_queue(wp_type):
    cached_queue = hr.horde_r_get(f"{wp_type}_wp_cache")
    if cached_queue is None:
        return None
    try:
        retrieved_json_list = json.loads(cached_queue)
    except (TypeError, OverflowError) as e:
        logger.error(f"Failed deserializing with error: {e}")
        return None
    deserialized_wp_list = []
    for json_row in retrieved_json_list:
        fake_wp_row = FakeWPRow(json_row)
        deserialized_wp_list.append(fake_wp_row)
    # logger.debug(len(deserialized_wp_list))
    return deserialized_wp_list


def query_prioritized_wps(wp_type="image"):
    waiting_prompt_type = WP_CLASS_MAP[wp_type]
    return (
        db.session.query(
            waiting_prompt_type.id,
            waiting_prompt_type.things,
            waiting_prompt_type.n,
            waiting_prompt_type.extra_priority,
            waiting_prompt_type.created,
            waiting_prompt_type.expiry,
        )
        .filter(
            waiting_prompt_type.n > 0,
            waiting_prompt_type.faulted == False,  # noqa E712
            waiting_prompt_type.active == True,  # noqa E712
        )
        .order_by(waiting_prompt_type.extra_priority.desc(), waiting_prompt_type.created.asc())
        .all()
    )


def prune_expired_stats():
    # clear up old requests (older than 5 mins)
    db.session.query(stats.FulfillmentPerformance).filter(
        stats.FulfillmentPerformance.created < datetime.utcnow() - timedelta(seconds=60),
    ).delete(synchronize_session=False)
    db.session.query(stats.ModelPerformance).filter(
        stats.ModelPerformance.created < datetime.utcnow() - timedelta(hours=1),
    ).delete(synchronize_session=False)
    db.session.commit()
    logger.debug("Pruned Expired Stats")


def compile_regex_filter(filter_type):
    all_filter_regex_query = db.session.query(Filter.regex).filter_by(filter_type=filter_type)
    all_filter_regex = [rfilter.regex for rfilter in all_filter_regex_query.all()]
    regex_string = "|".join(all_filter_regex)
    if not validate_regex(regex_string):
        logger.error("Error when checking compiled regex!. Avoiding cache store")
        return ""
    return regex_string


def retrieve_regex_replacements(filter_type):
    all_filter_regex_query = db.session.query(Filter.regex, Filter.replacement).filter_by(filter_type=filter_type)
    return [
        {
            "regex": rfilter.regex,
            "replacement": rfilter.replacement,
        }
        for rfilter in all_filter_regex_query.all()
        if validate_regex(rfilter.regex)
    ]


def get_all_users(sort="kudos", offset=0):
    user_order_by = User.created.asc() if sort == "age" else User.kudos.desc()
    return db.session.query(User).filter(User.oauth_id != "<wiped>").order_by(user_order_by).offset(offset).limit(25).all()


def get_style_by_uuid(style_uuid: str, is_collection=None):
    try:
        style_uuid = uuid.UUID(style_uuid)
    except ValueError:
        return None
    if SQLITE_MODE:
        style_uuid = str(style_uuid)
    style = None
    if is_collection is not True:
        style = db.session.query(Style).filter_by(id=style_uuid).first()
    if is_collection is True or not style:
        collection = db.session.query(StyleCollection).filter_by(id=style_uuid).first()
        return collection
    else:
        return style


def get_style_by_name(style_name: str, is_collection=None):
    """Goes through the styles and the categories and attempts to find a
    style or category that matches the given name
    The user can pre-specify a filter for category or style and/or username
    by formatting the name like
    category::db0#1::my_stylename
    alternatively this format is also allowed to allow multiple users to use the same name
    style::my_stylename
    db0#1::my_stylename
    """
    style_split = style_name.split("::")
    user = None
    # We don't change the is_collection if it comes preset in kwargs, as we then want it explicitly to return none
    # When searching for styles in collections and vice-versa
    if len(style_split) == 3:
        style_name = style_split[2]
        if is_collection is None:
            if style_split[0] == "collection":
                is_collection = True
            elif style_split[0] == "style":
                is_collection = False
        user = find_user_by_username(style_split[1])
    if len(style_split) == 2:
        style_name = style_split[1]
        if style_split[0] == "collection":
            if is_collection is None:
                is_collection = True
        elif style_split[0] == "style":
            if is_collection is None:
                is_collection = False
        else:
            user = find_user_by_username(style_split[0])
    seek_classes = [Style, StyleCollection]
    if is_collection is True:
        seek_classes = [StyleCollection]
    elif is_collection is False:
        seek_classes = [Style]
    for class_seek in seek_classes:
        style_query = db.session.query(class_seek).filter_by(name=style_name)
        if user is not None:
            style_query = style_query.filter_by(user_id=user.id)
        style = style_query.first()
        if style:
            return style


def retrieve_available_styles(
    style_type=None,
    sort="popular",
    public_only=True,
    page=0,
    tag=None,
    model=None,
):
    """Retrieves all style details from DB."""
    style_query = db.session.query(Style).filter_by(style_type=style_type)
    if tag is not None:
        style_query = style_query.join(StyleTag)
    if model is not None:
        style_query = style_query.join(StyleModel)
    if public_only:
        style_query = style_query.filter(Style.public.is_(True))
    if tag is not None:
        style_query = style_query.filter(StyleTag.tag == tag)
    if model is not None:
        style_query = style_query.filter(StyleModel.model == model)
    style_order_by = Style.created.asc() if sort == "age" else Style.use_count.desc()
    return style_query.order_by(style_order_by).offset(page).limit(25).all()


def retrieve_available_collections(
    collection_type=None,
    sort="popular",
    public_only=True,
    page=0,
):
    """Retrieves all collection details from DB."""
    style_query = db.session.query(StyleCollection)
    if collection_type is not None:
        style_query = style_query.filter_by(style_type=collection_type)
    if public_only:
        style_query = style_query.filter(StyleCollection.public.is_(True))
    style_order_by = StyleCollection.created.asc() if sort == "age" else StyleCollection.use_count.desc()
    return style_query.order_by(style_order_by).offset(page).limit(25).all()


def get_all_active_worker_messages(worker_id):
    return (
        db.session.query(WorkerMessage)
        .filter(
            or_(
                WorkerMessage.worker_id == worker_id,
                WorkerMessage.worker_id.is_(None),
            ),
            WorkerMessage.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_worker_messages(user_id=None, worker_id=None, validity="all", page=0):
    wmquery = db.session.query(WorkerMessage)
    if user_id is not None:
        wmquery = wmquery.filter(or_(WorkerMessage.user_id == user_id, WorkerMessage.worker_id.is_(None)))
    if worker_id is not None:
        wmquery = wmquery.filter(WorkerMessage.worker_id == worker_id)
    if validity == "active":
        wmquery = wmquery.filter(WorkerMessage.expiry > datetime.utcnow())
    if validity == "expired":
        wmquery = wmquery.filter(WorkerMessage.expiry <= datetime.utcnow())
    return wmquery.offset(page).limit(50).all()


def get_all_users_passkeys():
    """Retrieves all users passkeys."""
    return {
        user.id: user.proxy_passkey
        for user in db.session.query(User.proxy_passkey, User.id, User.flagged)
        .filter(
            User.proxy_passkey.is_not(None),
        )
        .all()
        if user.flagged is False or user.flagged is None
    }
