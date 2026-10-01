# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Locust entrypoint for the prompt moderation evidence scenario.

Spawns ``RejectionFlooder``, ``ModeratorReviewer`` and ``ModerationProber`` (see ``locustsuite.users.moderation``)
with fixed per-class counts from the CLI. It reuses the suite's key-parsing and target-preflight ``test_start``
handler by importing ``locustsuite.events`` for its side effects; that import must precede the configuration handler
below so keys are parsed first.

The flooder trips filters the operator seeds for the run, so the deployment's real filter terms are never sent:
create one filter the NSFW-model check strips (``--moderation-model-token``) and two the general filter matches
together (``--moderation-filter-tokens``), each a nonsense word that no real prompt contains.

Usage:
    locust -f tests/stress/locustfile_moderation.py --host https://dev.example \\
        --headless --users 12 --spawn-rate 3 --run-time 15m \\
        --requestor-api-keys R1,R2,... --moderation-moderator-api-key M \\
        --moderation-nsfw-model "Some NSFW Model" --moderation-model-token zzqmodeltoken \\
        --moderation-filter-tokens zzqfiltera,zzqfilterb \\
        --moderation-flooders 8 --moderation-reviewers 2 --moderation-probers 2 \\
        --moderation-evidence-path moderation_evidence.jsonl --moderation-address-label host-a \\
        --csv moderation --csv-full-history

The run's pass criterion is ``tests/stress/check_moderation_results.py`` over the evidence file, not Locust's exit
code, which only reflects per-response expectations.
"""

# ruff: noqa: I001
from __future__ import annotations

from locust import events

from locustsuite import events as _suite_events  # noqa: F401
from locustsuite.users.moderation import (
    ModerationProber,
    ModeratorReviewer,
    RejectionFlooder,
    configure_moderation,
    start_moderation_evidence,
    stop_moderation_evidence,
)

__all__ = ["ModerationProber", "ModeratorReviewer", "RejectionFlooder"]


@events.init_command_line_parser.add_listener
def _add_moderation_arguments(parser) -> None:
    group = parser.add_argument_group("AI Horde Moderation Evidence Scenario")
    group.add_argument(
        "--moderation-moderator-api-key",
        type=str,
        env_var="HORDE_MODERATION_MODERATOR_API_KEY",
        default="",
        help="API key of a moderator account; the reviewer and prober users need it.",
    )
    group.add_argument(
        "--moderation-nsfw-model",
        type=str,
        env_var="HORDE_MODERATION_NSFW_MODEL",
        default="",
        help="A model the deployment's model reference marks NSFW, so the NSFW-model check runs.",
    )
    group.add_argument(
        "--moderation-model-token",
        type=str,
        env_var="HORDE_MODERATION_MODEL_TOKEN",
        default="",
        help="A word a seeded NSFW-model filter (type 10) strips; alone it empties the prompt and is a model rejection.",
    )
    group.add_argument(
        "--moderation-filter-tokens",
        type=str,
        env_var="HORDE_MODERATION_FILTER_TOKENS",
        default="",
        help="Two comma-separated words matched by two seeded filters of different types; together they are a filter rejection.",
    )
    group.add_argument(
        "--moderation-pad-chars",
        type=int,
        env_var="HORDE_MODERATION_PAD_CHARS",
        default=0,
        help="Random characters appended as a negative prompt to every flood request (0 = none; 16000 = at the clip).",
    )
    group.add_argument(
        "--moderation-listing-limit",
        type=int,
        env_var="HORDE_MODERATION_LISTING_LIMIT",
        default=100,
        help="Page size the reviewer requests (1 to 100).",
    )
    group.add_argument(
        "--moderation-note-chance",
        type=float,
        env_var="HORDE_MODERATION_NOTE_CHANCE",
        default=0.05,
        help="Probability that a reviewer note task writes a note (notes are 30 a minute per key).",
    )
    group.add_argument(
        "--moderation-evidence-path",
        type=str,
        env_var="HORDE_MODERATION_EVIDENCE_PATH",
        default="moderation_evidence.jsonl",
        help="JSONL file of every flood request, text fetch and probe, truncated at test start; check_moderation_results.py reads it.",
    )
    group.add_argument(
        "--moderation-address-label",
        type=str,
        env_var="HORDE_MODERATION_ADDRESS_LABEL",
        default="local",
        help="Name of this source address in the evidence; give each load-generating host its own, since timeouts are per address.",
    )
    group.add_argument(
        "--moderation-threshold-hint",
        type=int,
        env_var="HORDE_MODERATION_THRESHOLD_HINT",
        default=5,
        help="The deployment's HORDE_MODEL_REJECTION_TIMEOUT_THRESHOLD, recorded in the evidence for the checker's report.",
    )
    for flag, env_var, cls_name in (
        ("--moderation-flooders", "HORDE_MODERATION_FLOODERS", "RejectionFlooder"),
        ("--moderation-reviewers", "HORDE_MODERATION_REVIEWERS", "ModeratorReviewer"),
        ("--moderation-probers", "HORDE_MODERATION_PROBERS", "ModerationProber"),
    ):
        group.add_argument(flag, type=int, env_var=env_var, default=0, help=f"Concurrent {cls_name} users (0 = weight-based).")


@events.test_start.add_listener
def _configure(environment, **_kwargs) -> None:
    options = environment.parsed_options
    configure_moderation(options)
    RejectionFlooder.fixed_count = options.moderation_flooders
    ModeratorReviewer.fixed_count = options.moderation_reviewers
    ModerationProber.fixed_count = options.moderation_probers
    start_moderation_evidence(environment)


@events.test_stop.add_listener
def _close_evidence(environment, **_kwargs) -> None:
    stop_moderation_evidence(environment)
