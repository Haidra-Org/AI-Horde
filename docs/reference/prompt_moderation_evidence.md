---
title: "Prompt moderation evidence reference"
summary: "What the horde retains when a prompt is rejected or a worker reports a job, how long it keeps it, and the moderator API over it."
topics: [requests, moderation, operations]
order: 65
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Prompt moderation evidence reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [moderation](../topics.md#moderation), [operations](../topics.md#operations), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

A rejection or a worker report writes a row that outlasts the request it came from, so a moderator can review it after
the request expired or the account was deleted. A replacement that succeeded and then generated is never recorded.

## Code map

| Concept                     | File                                        | Symbol                               |
| --------------------------- | ------------------------------------------- | ------------------------------------ |
| Evidence row                | `horde/classes/base/prompt_moderation.py`   | `PromptModerationEvent`              |
| Review disposition          | `horde/classes/base/prompt_moderation.py`   | `PromptModerationReview`             |
| Reasons and statuses        | `horde/classes/base/prompt_moderation.py`   | `PromptModerationReason`, `PromptReviewStatus` |
| Capture                     | `horde/database/prompt_moderation.py`       | `record_prompt_evidence`             |
| Listing query               | `horde/database/prompt_moderation.py`       | `get_prompt_events`                  |
| Review update               | `horde/database/prompt_moderation.py`       | `review_prompt_event`                |
| Retention                   | `horde/database/prompt_moderation.py`       | `prune_moderation_evidence`          |
| Retention schedule          | `horde/database/threads.py`                 | `prune_moderation_history`           |
| Rejection capture hook      | `horde/apis/v2/base.py`                     | `GenerateTemplate._record_prompt_rejection` |
| Worker report capture       | `horde/classes/base/user.py`                | `User.record_problem_job`            |
| Listing endpoint            | `horde/apis/v2/moderation.py`               | `OperationsPromptEvents`             |
| Review endpoint             | `horde/apis/v2/moderation.py`               | `OperationsPromptReview`             |

## Recorded events

A row holds the account, any proxied account, whichever request, job and worker identifiers exist, the reason, the
outcome, and the known prompt stages.

| Reason             | Outcome    | Raised by                                                  |
| ------------------ | ---------- | ---------------------------------------------------------- |
| `filter_rejection` | `rejected` | The prompt filter rejected the prompt, or the replacement filter emptied it or was asked to replace more than 7000 characters |
| `model_rejection`  | `rejected` | The NSFW-model replacement emptied the prompt, for an NSFW model or for a flagged account on any model |
| `worker_csam`      | `censored` | An image worker reported the job                            |

A `worker_csam` row records that a worker reported the job. It carries no finding about the account.

Some stages cannot be known at capture time. A rejected submission has no waiting-request ID, since no row was created.
A worker report cannot recover the styled pre-moderation prompt, so `moderation_prompt` stays `null`. For a rejected
submission, `effective_prompt` holds the last value the pipeline produced, may itself be `null`, and never reached a
worker.

Each stage is clipped to 16,000 characters behind a `text_truncated` flag. The limit applies to evidence alone, the
original submission on the waiting request keeps its full length. `job_id` is unique, so a repeated worker report for
one job resolves to the existing event rather than a second row. A rejected attempt has no request ID, so each attempt
is a separate row.

## Moderator API

Every endpoint requires the moderator `apikey` header and responds with `Cache-Control: private, no-store` set.

- `GET /api/v2/operations/moderation/prompts` returns newest-first evidence. Accepts `limit` (1-100, default 50),
  `before_id`, `user_id`, `reason`, `outcome`, `status`, `since`, and `until`. Times are ISO 8601 with an explicit
  timezone; `since` is inclusive and `until` exclusive. Follow `next_cursor` as the next `before_id`.
- `PATCH /api/v2/operations/moderation/prompts/<event_id>` takes a JSON `status` (`pending`, `reviewed`, `dismissed`)
  and an optional `note` of at most 2,000 characters. `pending` reopens an event. The authenticated key supplies
  reviewer identity; the body cannot.

The prompt listing defaults to every disposition; `status=pending` is the review queue. A review records the latest
disposition only.

`tests/integration/test_prompt_moderation.py` covers these contracts.

## Retention

Prompt evidence and its reviews expire after 30 days. The primary node's maintenance loop deletes up to 1,000 rows
every minute using the creation-time index. Deleting an event cascades to its review.

## Sharp edges

- **Evidence can be lost.** The write uses a separate transaction on a second pooled connection, so rolling back a
  rejected submission cannot erase it. A pool or database failure logs the exception type alone, never the bound
  prompt text, and preserves the rejection. Monitor those failure logs and pool capacity.
- **The rejection path takes a second pooled connection.** Rejections spike during an abuse flood, when the pool is
  already contended.
- **No foreign keys.** A rejected submission has no waiting row, and evidence must outlive request expiry and account
  deletion. Only the review references the event.
- **Retention is eventual.** Ordinary replacement volume does not reach these tables; sustained rejection or
  problem-job volume still consumes storage. Monitor the maintenance loop for failures and backlog. Live-row cleanup
  leaves backups alone.
- **Privileged accounts are recorded.** Moderator rejections and administrator problem jobs produce evidence, though
  both are exempt from the IP blocking and notification thresholds.
- **Discord alerts cite an event ID and a moderator API link in place of the prompt.** Problem-job notification
  thresholds are unchanged. Following the link requires a client supplying moderator credentials.

## Related

- [Prompt provenance reference](prompt_provenance.md)
