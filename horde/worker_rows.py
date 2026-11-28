# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Store the finished JSON details row of every registered worker in Redis, one hash per view.

Each row holds the bytes of the object that ``GET /v2/workers/{id}`` returns for that worker and view, already
marshalled, so a reader can join rows into a response without loading or marshalling any worker. Background jobs on
the quorum node keep the rows fresh in three tiers, each counted by ``WorkerRowsTier``:

* the worker list job rewrites every active worker on each run, and marks dirty the workers that stopped being active;
* the same job drains a bounded number of workers marked dirty by a change to their details;
* a rolling pass rewrites every registered worker in ID order, which bounds how stale any row can be.

Critical members:

* ``WorkerListView``: the views stored, each in its own hash.
* ``store_worker_rows``, ``remove_worker_rows``: write and delete rows.
* ``mark_worker_rows_dirty``: queue workers for the next drain after a change to their details.
* ``read_worker_rows``: read rows by worker ID with one ``HMGET``.
* ``reconcile_worker_rows``: compare the stored rows with the registered workers, run by the refresh of
  ``worker_list_entries``.
* ``read_active_worker_ids``: the IDs of the workers that were active on the last run of the worker list job.

The marshalling imports the API models lazily, because ``horde.apis`` imports ``horde.database``, which imports this
module.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection, Iterable, Sequence
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.metrics import WorkerRowsTier, worker_rows_rolling_pass_duration, worker_rows_written

if TYPE_CHECKING:
    from horde.classes.base.worker import WorkerTemplate

__all__ = [
    "WORKER_ROWS_DIRTY_CHUNK_SIZE",
    "WORKER_ROWS_ROLLING_CHUNK_SIZE",
    "WORKER_ROWS_ROLLING_INTERVAL_SECONDS",
    "WorkerListView",
    "advance_rolling_position",
    "build_worker_rows",
    "mark_departed_active_workers",
    "mark_worker_rows_dirty",
    "read_active_worker_ids",
    "read_rolling_position",
    "read_worker_rows",
    "reconcile_worker_rows",
    "remove_worker_rows",
    "store_worker_rows",
    "take_dirty_worker_ids",
]


class WorkerListView(StrEnum):
    """Represent the set of worker fields that a caller receives."""

    PUBLIC = "public"
    """The fields open to every caller. The owner and messages are included only for owners who made their
    workers public.
    """
    MODERATOR = "moderator"
    """Every field, including addresses, contacts, pause state and suspicion counts."""
    OWNER = "owner"
    """The fields of an owner's workers, including the address, contact and messages, without suspicion counts."""

    @property
    def details_privilege(self) -> int:
        """Return the ``details_privilege`` argument of ``WorkerTemplate.get_details`` that builds this view's rows."""
        if self is WorkerListView.MODERATOR:
            return 2
        if self is WorkerListView.OWNER:
            return 1
        return 0

    @property
    def rows_key(self) -> str:
        """Return the key of the Redis hash that holds this view's rows, keyed by worker ID."""
        return f"worker_rows:{self.value}"

    @property
    def snapshot_key(self) -> str:
        """Return the Redis key under which ``store_worker_list`` stores this view's list of active workers.

        Raises:
            ValueError: The view is the owner view, which the full worker list does not serve.
        """
        if self is WorkerListView.MODERATOR:
            return "worker_cache_privileged"
        if self is WorkerListView.OWNER:
            raise ValueError("the owner view is not stored in a snapshot of the active workers")
        return "worker_cache"


WORKER_ROWS_LIFETIME: timedelta = timedelta(hours=1)
"""How long a row hash and the set of active worker IDs stay after their last write.

Every write refreshes the expiry, and the worker list job writes every few seconds, so the hashes expire only when the
builder has stopped. Readers then find no rows instead of rows that are never refreshed again. An hour leaves room for
a quorum handover and for several rolling passes.
"""
WORKER_ROWS_DIRTY_KEY: str = "worker_rows:dirty"
"""The Redis set of IDs of workers whose rows must be rewritten on the next drain."""
WORKER_ROWS_ACTIVE_KEY: str = "worker_rows:active"
"""The Redis set of IDs of the workers that were active on the last run of the worker list job.

It is kept in Redis rather than in the process, so a new quorum node still finds the workers that stopped being active
since the last run of the previous node.
"""
WORKER_ROWS_ROLLING_POSITION_KEY: str = "worker_rows:rolling_position"
"""The Redis key of the last worker ID refreshed by the rolling pass. A missing key starts the pass at the lowest ID."""
WORKER_ROWS_ROLLING_PASS_STARTED_KEY: str = "worker_rows:rolling_pass_started"
"""The Redis key of the Unix time of the last wrap of the rolling pass, from which the next wrap measures the pass."""
WORKER_ROWS_DIRTY_CHUNK_SIZE: int = 500
"""The most dirty workers drained by one run of the worker list job.

The drain shares the job's two-second interval with the active worker snapshot, so the limit bounds how much one run
can be delayed. A burst, such as every worker of one user, drains over a few runs.
"""
WORKER_ROWS_ROLLING_CHUNK_SIZE: int = 500
"""The number of workers rewritten by one run of the rolling pass.

The loader issues the same number of statements for any chunk size, so the size sets the pass length. With about
twenty thousand registered workers and ``WORKER_ROWS_ROLLING_INTERVAL_SECONDS`` between runs, a pass takes about
twenty minutes, well inside ``WORKER_ROWS_LIFETIME``.
"""
WORKER_ROWS_RECONCILE_LIMIT: int = 5000
"""The most workers whose rows one reconciliation removes because they are no longer registered.

Deletions normally remove their rows at once, so only a few rows are left over between two reconciliations. The
limit bounds the ``HDEL`` commands of one run when many are left over, and later runs remove the rest.
"""
WORKER_ROWS_DIRTY_MARK_CHUNK_SIZE: int = 5000
"""The most worker IDs sent in one ``SADD`` when a reconciliation marks workers without rows dirty.

A reconciliation marks every registered worker without a public row in one run, in chunks of this size. When the row
hashes are lost, because Redis restarted or failed over, the hashes expired, or the row store is new, every worker
lacks rows and most pages cannot be served until the rows are back. Marking them all at once lets every following run
of the worker list job restore up to ``WORKER_ROWS_DIRTY_CHUNK_SIZE`` workers, without waiting for later
reconciliations. The reconciliation runs once a minute with the refresh of ``worker_list_entries``, so the workers can
be marked up to a minute after the loss. Under a full backlog, a run of the worker list job lasts longer than its
two-second interval, so the rebuild takes a few minutes. The dirty set is a set, so marking a worker that is already
dirty does not grow it. In steady state only a few workers lack rows, and one command marks them.
"""
WORKER_ROWS_ROLLING_INTERVAL_SECONDS: int = 30
"""The seconds between runs of the rolling pass.

Together with ``WORKER_ROWS_ROLLING_CHUNK_SIZE`` it sets how long a pass takes, which is the longest a row that no
write site marked dirty stays stale.
"""


def _as_text(redis_value: str | bytes) -> str:
    """Convert a value read from Redis to text. Clients created with ``decode_responses`` return text already."""
    if isinstance(redis_value, bytes):
        return redis_value.decode()
    return redis_value


def build_worker_rows(workers: Iterable[WorkerTemplate]) -> dict[WorkerListView, dict[str, bytes]]:
    """Create the row bytes of each worker in every view, keyed by view and then by worker ID.

    Each row is the details object marshalled with the model and options of ``GET /v2/workers/{id}``, encoded as
    compact JSON. The workers must have every relationship read by ``get_details()`` loaded, as the bulk loaders in
    ``horde.database.functions`` return them, or each row runs its own lazy loads.

    Args:
        workers: The workers to build rows for.
    """
    # A module-level import would be circular: horde.apis imports horde.database, which imports this module.
    from flask_restx import marshal

    from horde.apis.v2.base import models

    rows_by_view: dict[WorkerListView, dict[str, bytes]] = {view: {} for view in WorkerListView}
    for worker in workers:
        worker_id = str(worker.id)
        for view in WorkerListView:
            marshalled_details = marshal(
                worker.get_details(view.details_privilege),
                models.response_model_worker_details,
                skip_none=True,
            )
            rows_by_view[view][worker_id] = json.dumps(
                marshalled_details,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
    return rows_by_view


def store_worker_rows(workers: Sequence[WorkerTemplate], tier: WorkerRowsTier) -> None:
    """Write the rows of ``workers`` in every view, replacing their previous rows.

    Args:
        workers: The workers to write, loaded as ``build_worker_rows`` requires.
        tier: The refresh that loaded the workers, for the written row count.
    """
    if not workers:
        return
    for view, rows in build_worker_rows(workers).items():
        hr.horde_r_hset_many(view.rows_key, rows, WORKER_ROWS_LIFETIME)
        worker_rows_written.add(len(rows), {"horde.worker_rows.tier": tier.value})


def remove_worker_rows(worker_ids: Collection[str]) -> None:
    """Delete the rows of ``worker_ids`` from every view."""
    for view in WorkerListView:
        hr.horde_r_hdel(view.rows_key, worker_ids)


def mark_worker_rows_dirty(worker_ids: Collection[str]) -> None:
    """Queue ``worker_ids`` for a rewrite of their rows on the next run of the worker list job.

    Call this after the change to the workers has been committed, so the rewrite reads the changed rows.
    """
    hr.horde_r_sadd(WORKER_ROWS_DIRTY_KEY, worker_ids)


def read_worker_rows(view: WorkerListView, worker_ids: Sequence[str]) -> list[bytes | None]:
    """Return the stored row of each of ``worker_ids`` in ``view``, in the order of ``worker_ids``.

    The rows are read from the primary Redis with one ``HMGET``. A worker without a stored row returns None.
    """
    stored_rows = hr.horde_r_hmget(view.rows_key, worker_ids)
    return [stored_row.encode() if isinstance(stored_row, str) else stored_row for stored_row in stored_rows]


def take_dirty_worker_ids(limit: int) -> list[str]:
    """Return up to ``limit`` dirty worker IDs and remove them from the dirty set.

    The IDs are removed before the caller loads the workers. A worker marked dirty again while it is being loaded is
    then added back to the set and rewritten on the next drain. Removing after the load would drop that mark. If the
    caller fails after this call, the rolling pass rewrites the workers instead.
    """
    dirty_worker_ids = [_as_text(member) for member in hr.horde_r_srandmember(WORKER_ROWS_DIRTY_KEY, limit)]
    hr.horde_r_srem(WORKER_ROWS_DIRTY_KEY, dirty_worker_ids)
    return dirty_worker_ids


def mark_departed_active_workers(active_worker_ids: Collection[str]) -> None:
    """Mark dirty the workers that were active on the previous run and are not in ``active_worker_ids``.

    Their rows were last written while they were active, so they still read ``online: true`` until rewritten. The
    current IDs then replace the stored set for the next run.

    Args:
        active_worker_ids: The IDs of the workers active on this run.
    """
    departed_worker_ids = read_active_worker_ids().difference(active_worker_ids)
    mark_worker_rows_dirty(departed_worker_ids)
    hr.horde_r_replace_set(WORKER_ROWS_ACTIVE_KEY, active_worker_ids, WORKER_ROWS_LIFETIME)


def read_active_worker_ids() -> set[str]:
    """Return the IDs of the workers that were active on the last run of the worker list job.

    The set is read from the primary Redis. It is empty when the job has not run for ``WORKER_ROWS_LIFETIME``, so a
    caller then treats every worker as offline.
    """
    return {_as_text(member) for member in hr.horde_r_smembers(WORKER_ROWS_ACTIVE_KEY)}


def reconcile_worker_rows(registered_worker_ids: Collection[str]) -> None:
    """Remove the rows of workers that are no longer registered and mark dirty the workers without a public row.

    A worker deleted without a call to ``remove_worker_rows`` keeps its rows, and a row lost from Redis is otherwise
    restored only by the rolling pass. The removal is bounded by ``WORKER_ROWS_RECONCILE_LIMIT``. Every registered
    worker without a public row is marked dirty in the same run (see ``_mark_unstored_workers_dirty``), so lost hashes
    are rebuilt at the rate of the dirty drain. The fields are read on the primary.

    A worker registered after ``registered_worker_ids`` was read may lose its new rows here. It is active, so the next
    run of the worker list job writes them again within seconds. A worker deleted after that read is marked dirty, and
    the drain then finds no worker and removes its rows.

    Args:
        registered_worker_ids: The IDs of every registered worker of a listed type, read in the same run.
    """
    stored_worker_ids_by_view = {view: {_as_text(field) for field in hr.horde_r_hkeys(view.rows_key)} for view in WorkerListView}
    stored_worker_ids = set().union(*stored_worker_ids_by_view.values())
    departed_worker_ids = sorted(stored_worker_ids.difference(registered_worker_ids))[:WORKER_ROWS_RECONCILE_LIMIT]
    remove_worker_rows(departed_worker_ids)
    unstored_worker_ids = sorted(set(registered_worker_ids).difference(stored_worker_ids_by_view[WorkerListView.PUBLIC]))
    _mark_unstored_workers_dirty(unstored_worker_ids)


def _mark_unstored_workers_dirty(unstored_worker_ids: Sequence[str]) -> None:
    """Mark dirty every worker in ``unstored_worker_ids``, with at most ``WORKER_ROWS_DIRTY_MARK_CHUNK_SIZE`` IDs per command.

    More than one chunk of workers without rows means the row hashes were lost as a whole, and most pages of the paged
    lists are refused until the drain has restored the rows. The rebuild is logged so the refusals can be explained.

    Args:
        unstored_worker_ids: The IDs of the registered workers without a public row.
    """
    if len(unstored_worker_ids) > WORKER_ROWS_DIRTY_MARK_CHUNK_SIZE:
        logger.warning(f"The row store lacks the rows of {len(unstored_worker_ids)} registered workers. Marking all of them dirty.")
    for chunk_start in range(0, len(unstored_worker_ids), WORKER_ROWS_DIRTY_MARK_CHUNK_SIZE):
        mark_worker_rows_dirty(unstored_worker_ids[chunk_start : chunk_start + WORKER_ROWS_DIRTY_MARK_CHUNK_SIZE])


def read_rolling_position() -> str | None:
    """Return the last worker ID refreshed by the rolling pass, or None when the pass starts from the lowest ID.

    The position is read from the primary Redis, so a new quorum node resumes where the previous one stopped.
    """
    if hr.horde_r is None:
        return None
    stored_position = hr.horde_r.get(WORKER_ROWS_ROLLING_POSITION_KEY)
    if stored_position is None:
        return None
    return _as_text(stored_position)


def advance_rolling_position(chunk_worker_ids: Sequence[str], chunk_size: int) -> None:
    """Move the rolling pass past the chunk just refreshed, or wrap to the lowest ID after the last worker.

    A chunk shorter than ``chunk_size`` reached the last worker, so the pass wraps. Each wrap records the time since the
    previous wrap as the duration of the pass.

    Args:
        chunk_worker_ids: The IDs of the chunk just refreshed, in ascending order.
        chunk_size: The number of IDs requested for the chunk.
    """
    if len(chunk_worker_ids) >= chunk_size:
        hr.horde_r_set(WORKER_ROWS_ROLLING_POSITION_KEY, chunk_worker_ids[-1])
        return
    wrapped_at = time.time()
    previous_wrap = hr.horde_r.get(WORKER_ROWS_ROLLING_PASS_STARTED_KEY) if hr.horde_r is not None else None
    if previous_wrap is not None:
        try:
            worker_rows_rolling_pass_duration.record(wrapped_at - float(_as_text(previous_wrap)))
        except ValueError:
            logger.warning(f"Ignoring a malformed rolling pass start time: {previous_wrap!r}")
    hr.horde_r_set(WORKER_ROWS_ROLLING_PASS_STARTED_KEY, str(wrapped_at))
    hr.horde_r_delete(WORKER_ROWS_ROLLING_POSITION_KEY)
