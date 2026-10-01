# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Parse the moderation evidence retention settings, refusing any value that would weaken the ceiling.

Also parse the per-subject countermeasure settings, which accept only positive whole numbers.

Also check that every event column has a decided retention fate, and that the evidence tables build outside
PostgreSQL with UTC capture-time defaults.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy

from horde.classes.base.prompt_moderation import PromptModerationEvent, PromptModerationNote
from horde.database.prompt_moderation import (
    DEFAULT_MODEL_REJECTION_TIMEOUT_THRESHOLD,
    DEFAULT_SUBJECT_TEXT_CAP_PER_HOUR,
    EVENT_COLUMN_RETENTION,
    EVIDENCE_CEILING_ENV,
    IPADDR_RETENTION_ENV,
    MODEL_REJECTION_TIMEOUT_THRESHOLD_ENV,
    REMOVAL_FLAGS,
    SUBJECT_TEXT_CAP_PER_HOUR_ENV,
    TEXT_RETENTION_ENV,
    RetentionFate,
    RetentionPolicy,
    load_positive_setting,
    load_retention_policy,
    validate_column_retention,
)

pytestmark = pytest.mark.unit


def test_absent_settings_use_the_defaults() -> None:
    policy = load_retention_policy({})

    assert policy == RetentionPolicy(text=None, ipaddr=timedelta(days=30), ceiling=timedelta(days=365))
    assert (policy.text_days, policy.ipaddr_days, policy.ceiling_days) == (None, 30, 365)


@pytest.mark.parametrize("spelling", ["none", "NONE", " None "])
def test_none_leaves_text_and_address_until_the_ceiling(spelling: str) -> None:
    policy = load_retention_policy({TEXT_RETENTION_ENV: spelling, IPADDR_RETENTION_ENV: spelling})

    assert policy.text is None
    assert policy.ipaddr is None
    assert (policy.text_days, policy.ipaddr_days) == (None, None)
    assert policy.ceiling == timedelta(days=365)


def test_configured_windows_are_read_in_days() -> None:
    policy = load_retention_policy({TEXT_RETENTION_ENV: "7", IPADDR_RETENTION_ENV: "3", EVIDENCE_CEILING_ENV: "60"})

    assert policy == RetentionPolicy(text=timedelta(days=7), ipaddr=timedelta(days=3), ceiling=timedelta(days=60))


def test_window_equal_to_the_ceiling_is_accepted() -> None:
    policy = load_retention_policy({TEXT_RETENTION_ENV: "120", IPADDR_RETENTION_ENV: "120", EVIDENCE_CEILING_ENV: "120"})

    assert policy.text == policy.ipaddr == policy.ceiling


@pytest.mark.parametrize("variable", [TEXT_RETENTION_ENV, IPADDR_RETENTION_ENV, EVIDENCE_CEILING_ENV])
@pytest.mark.parametrize("raw", ["", "0", "-5", "1.5", "thirty", "30d"])
def test_malformed_values_are_refused_with_the_variable_name(variable: str, raw: str) -> None:
    with pytest.raises(ValueError, match=variable):
        load_retention_policy({variable: raw})


@pytest.mark.parametrize("variable", [TEXT_RETENTION_ENV, IPADDR_RETENTION_ENV])
def test_window_longer_than_the_ceiling_is_refused(variable: str) -> None:
    with pytest.raises(ValueError, match=f"{variable}.*exceeds {EVIDENCE_CEILING_ENV}"):
        load_retention_policy({variable: "91", EVIDENCE_CEILING_ENV: "90"})


def test_default_window_longer_than_a_short_ceiling_is_refused() -> None:
    """Lowering the ceiling below the address default without lowering the address window is refused."""
    with pytest.raises(ValueError, match=IPADDR_RETENTION_ENV):
        load_retention_policy({EVIDENCE_CEILING_ENV: "20"})


def test_default_text_is_kept_until_a_short_ceiling() -> None:
    """Text has no default window, so any ceiling at or above the address default is accepted without setting it."""
    policy = load_retention_policy({EVIDENCE_CEILING_ENV: "30"})

    assert policy.text is None
    assert policy.ceiling == timedelta(days=30)


def test_ceiling_above_the_maximum_is_refused() -> None:
    with pytest.raises(ValueError, match=f"{EVIDENCE_CEILING_ENV} must be at most 365"):
        load_retention_policy({EVIDENCE_CEILING_ENV: "366"})


@pytest.mark.parametrize("spelling", ["none", "None"])
def test_ceiling_cannot_be_disabled(spelling: str) -> None:
    with pytest.raises(ValueError, match=f"{EVIDENCE_CEILING_ENV} cannot be disabled"):
        load_retention_policy({EVIDENCE_CEILING_ENV: spelling})


@pytest.mark.parametrize(
    ("text", "ipaddr", "ceiling"),
    [
        (None, None, timedelta(days=366)),
        (None, None, timedelta(0)),
        (timedelta(days=2), None, timedelta(days=1)),
        (None, timedelta(days=2), timedelta(days=1)),
        (timedelta(0), None, timedelta(days=1)),
    ],
)
def test_policy_constructed_directly_enforces_the_same_bounds(
    text: timedelta | None,
    ipaddr: timedelta | None,
    ceiling: timedelta,
) -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(text=text, ipaddr=ipaddr, ceiling=ceiling)


def test_every_event_column_has_a_retention_fate() -> None:
    assert set(EVENT_COLUMN_RETENTION) == set(PromptModerationEvent.__table__.columns.keys())


def test_column_without_a_fate_is_refused() -> None:
    columns = [*EVENT_COLUMN_RETENTION, "undecided_column"]

    with pytest.raises(ValueError, match=r"no fate for \['undecided_column'\]"):
        validate_column_retention(EVENT_COLUMN_RETENTION, REMOVAL_FLAGS, columns)


def test_fate_for_a_missing_column_is_refused() -> None:
    column_retention = {**EVENT_COLUMN_RETENTION, "dropped_column": RetentionFate.KEPT}

    with pytest.raises(ValueError, match=r"no column for \['dropped_column'\]"):
        validate_column_retention(column_retention, REMOVAL_FLAGS, list(EVENT_COLUMN_RETENTION))


def test_removable_fate_without_a_flag_is_refused() -> None:
    removal_flags: dict[RetentionFate, str] = {fate: flag for fate, flag in REMOVAL_FLAGS.items() if fate != RetentionFate.IDENTITY}

    with pytest.raises(ValueError, match="Removal flags"):
        validate_column_retention(EVENT_COLUMN_RETENTION, removal_flags, list(EVENT_COLUMN_RETENTION))


def test_evidence_tables_build_on_sqlite_with_utc_capture_defaults() -> None:
    engine = sqlalchemy.create_engine("sqlite://")
    tables = [PromptModerationEvent.__table__, PromptModerationNote.__table__]
    PromptModerationEvent.metadata.create_all(engine, tables=tables)
    before = datetime.utcnow().replace(microsecond=0)
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO prompt_moderation_events (user_id, reason, outcome) VALUES (1, 'filter_rejection', 'rejected')",
            ),
        )
        event_id, created, anonymized = connection.execute(
            sqlalchemy.text("SELECT id, created, anonymized FROM prompt_moderation_events"),
        ).one()
        connection.execute(
            sqlalchemy.text("INSERT INTO prompt_moderation_notes (event_id, author_id, note) VALUES (:event_id, 2, 'n')"),
            {"event_id": event_id},
        )
        (note_created,) = connection.execute(sqlalchemy.text("SELECT created FROM prompt_moderation_notes")).one()
    engine.dispose()
    after = datetime.utcnow() + timedelta(seconds=1)
    for value in (created, note_created):
        assert before <= datetime.fromisoformat(str(value)) <= after
    assert not anonymized


@pytest.mark.parametrize(
    ("variable", "default"),
    [
        (MODEL_REJECTION_TIMEOUT_THRESHOLD_ENV, DEFAULT_MODEL_REJECTION_TIMEOUT_THRESHOLD),
        (SUBJECT_TEXT_CAP_PER_HOUR_ENV, DEFAULT_SUBJECT_TEXT_CAP_PER_HOUR),
    ],
)
def test_countermeasure_settings_default_when_absent_and_read_when_set(variable: str, default: int) -> None:
    assert load_positive_setting({}, variable, default, "events") == default == 5
    assert load_positive_setting({variable: " 12 "}, variable, default, "events") == 12


@pytest.mark.parametrize("variable", [MODEL_REJECTION_TIMEOUT_THRESHOLD_ENV, SUBJECT_TEXT_CAP_PER_HOUR_ENV])
@pytest.mark.parametrize("raw", ["", "0", "-1", "2.5", "five", "none"])
def test_malformed_countermeasure_settings_are_refused_with_the_variable_name(variable: str, raw: str) -> None:
    with pytest.raises(ValueError, match=variable):
        load_positive_setting({variable: raw}, variable, 5, "events")


def test_text_location_is_reset_with_the_text_and_has_no_flag_of_its_own() -> None:
    text_location = [column for column, fate in EVENT_COLUMN_RETENTION.items() if fate == RetentionFate.TEXT_LOCATION]

    assert text_location == ["text_state"]
    assert RetentionFate.TEXT_LOCATION not in REMOVAL_FLAGS
    assert {"text_sha256", "text_chars"} <= {column for column, fate in EVENT_COLUMN_RETENTION.items() if fate == RetentionFate.TEXT}
