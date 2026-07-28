# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit coverage for ``PromptChecker`` suspicion scoring.

The compiled filters normally come from the database via redis. These tests pin
known regexes onto the shared checker and freeze its refresh window, so the
scoring rules (one point per filter group, emoji triggers on ``filter_10``,
single-filter targeting) are exercised without any moderation data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import regex as re

from horde.argparser import args
from horde.detection import prompt_checker


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.setattr(args, "disable_filters", False)
    monkeypatch.setattr(prompt_checker, "next_refresh", datetime.utcnow() + timedelta(days=1))
    monkeypatch.setattr(
        prompt_checker,
        "compiled",
        {
            "filter_10": re.compile(r"zebracorn", re.IGNORECASE),
            "filter_11": re.compile(r"quixotle", re.IGNORECASE),
            "filter_20": re.compile(r"vermillex", re.IGNORECASE),
        },
    )
    return prompt_checker


def test_clean_prompt_is_not_suspicious(checker):
    assert checker("a perfectly ordinary prompt") == (0, [])


def test_first_group_filter_scores_once(checker):
    suspicion, matches = checker("this is zebracorn content")
    assert suspicion == 1
    assert matches == ["zebracorn"]


def test_second_filter_of_first_group_scores(checker):
    suspicion, matches = checker("this is quixotle content")
    assert suspicion == 1
    assert matches == ["quixotle"]


def test_both_groups_matching_scores_twice(checker):
    suspicion, matches = checker("zebracorn and vermillex")
    assert suspicion == 2
    assert matches == ["zebracorn", "vermillex"]


def test_child_emoji_triggers_first_group(checker):
    suspicion, matches = checker("an otherwise clean prompt 👧")
    assert suspicion == 1
    assert matches == ["👧"]


def test_unrelated_emoji_does_not_trigger(checker):
    assert checker("an otherwise clean prompt 🚀") == (0, [])


def test_emoji_does_not_affect_later_group_filters(checker):
    """Emoji only gate ``filter_10``; other filters score off their regex alone."""
    suspicion, matches = checker("🚀 vermillex 🚀")
    assert suspicion == 1
    assert matches == ["vermillex"]


def test_child_emoji_and_second_group_score_together(checker):
    suspicion, matches = checker("👧 vermillex")
    assert suspicion == 2
    assert matches == ["👧", "vermillex"]


def test_targeted_filter_id_ignores_other_filters(checker):
    assert checker("quixotle and vermillex", 10) == (0, [])
    assert checker("vermillex", 20)[0] == 1


def test_negative_prompt_is_excluded_from_scan(checker):
    assert checker("clean prompt###zebracorn") == (0, [])


def test_disabled_filters_short_circuit(checker, monkeypatch):
    monkeypatch.setattr(args, "disable_filters", True)
    assert checker("zebracorn and vermillex") == (0, [])
