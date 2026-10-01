---
status: accepted
date: 2026-09-29
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Decay moderation evidence in tiers under an anonymizing ceiling

## Context and Problem Statement

A rejected prompt or a worker's report of suspected child sexual abuse material writes a `prompt_moderation_events`
row with the account, any proxied account, the address, the requested models and the prompt text. A worker report also
writes a `user_problem_jobs` record with the account, the address and the job, which the problem-job alerts count.
Moderators review these rows and decide any action.

The records exist to identify accounts attempting to generate illegal content and to enforce the restrictions that
follow. A restriction cannot be enforced against an account or address whose record is gone, so any route that removes
a record while the moderation purpose stands defeats it. The Horde is open source, so every deletion path is public; a
deployment must not offer a self-service route, such as an account wipe, to destroying evidence.

Deleting a whole row at a fixed age removes every value at once. The prompt text and address carry most of the privacy
cost and lose review value quickly, while the reason, outcome, models and capture time stay useful for counting
rejections and reports long after. A single hard-coded window also leaves operators no way to match retention to their
legal assessment, and the privacy policy has no authoritative value to disclose.

## Decision Drivers

- Keep records for as long as the moderation purpose requires.
- Bound identifiable retention of records serving no such purpose with a limit no configuration can exceed or
  disable.
- Keep enough of each row to count rejections and reports by reason, outcome, model and worker.
- Let operators set the windows without a code change.
- Keep the published disclosure equal to the behavior.

## Considered Options

- Tiered decay under an anonymizing ceiling
- Delete each event after one configurable window
- Keep events indefinitely and restrict access

## Decision Outcome

Chosen option: "Tiered decay under an anonymizing ceiling".

Each event decays by age from capture. `HORDE_MODERATION_IPADDR_RETENTION_DAYS` (default 30) removes the address.
`HORDE_MODERATION_TEXT_RETENTION_DAYS` removes the three prompt stages. It defaults to `none`, which keeps the text
until the ceiling, so evidence no moderator has reviewed yet stays reviewable for the whole evidence period. Either
window can be `none`, and an operator can set a shorter text window. `HORDE_MODERATION_EVIDENCE_CEILING_DAYS` (default
365, at most 365, never disabled) anonymizes every event of every reason that is not under moderation action: it
clears the account, proxied account, address, address pseudonym, request and job identifiers and prompt stages. The
reason, outcome, models, capture time, truncation flag, reporting worker and notes remain. A window longer than the
ceiling, or any malformed value, stops startup.

An event is under moderation action when it has at least one note, or its account is flagged, or its account is
suspicious: not trusted and holding at least `User.SUSPICION_THRESHOLD` suspicions, as `User.is_suspicious` judges it.
Every retention step skips such events, with no cap, for as long as the condition holds. `_under_moderation_action` in
`horde/database/prompt_moderation.py` is the single definition; a worker report record uses the account part only.

Capture also stores an address pseudonym, `ip_subject_key`: HMAC-SHA256 under the deployment's `secret_key` over a
moderation label and the IP subject (the IPv4 address, or the IPv6 /64). It outlives the address window and is cleared
at the ceiling, so address filters keep matching an event whose address is gone. The IPv4 space can be enumerated, so
no pseudonym is produced while the secret is unset or a published placeholder.

Worker report records follow the same address window and ceiling: their address is removed after the address window
and the record is deleted at the ceiling, unless their account is flagged or suspicious. Their alerts count the last
hour or day, so neither step changes an alert.

Retention never deletes an event by age. Each removal sets a flag (`text_redacted`, `ipaddr_redacted`, `anonymized`)
so a null value reads as removed, and a partial index per flag keeps each bounded pass off finished rows.

Notes are never deleted. An anonymized event still takes new notes, which are stored with it.

An account wipe never touches evidence events, notes or worker report records. They keep their `user_id`, and
retention treats them as it treats any other account's records.

The privacy document renders its "Moderation records" section from the same policy object the retention pass applies,
both reading it when they run, and the moderator listing reports that policy, so neither can drift from the behavior.

### Consequences

- Good: Text and addresses of unactioned records leave the database on the shortest schedule the operator chooses,
  and identity leaves by the ceiling at the latest.
- Good: A restriction stays enforceable, since neither retention nor an account wipe removes the records behind it.
- Good: Counts by reason, outcome, model and worker stay derivable from the rows after anonymization.
- Good: The disclosure and the listing always state the windows in force.
- Bad: Records under moderation action have no end date. Retention resumes on an event only once it has no note and
  its account is neither flagged nor suspicious.
- Bad: The exemption is evaluated at each pass, so a record anonymized before its account was flagged stays
  anonymized.
- Bad: Rows are never deleted by age, so the row count grows with rejection and report volume for as long as the
  table exists.
- Bad: A backlog larger than the per-minute catch-up bound (20 passes of 1,000 rows per step) leaves values past
  their windows until the tick catches up; a saturation counter reports each tick that used every pass.
- Bad: Records under moderation action never get a retention flag, so every pass rescans them.
- Bad: Lengthening a window cannot restore values already removed.
- Bad: The pseudonym keeps events of one address linkable until the ceiling, after the address itself is gone.

## Pros and Cons of the Options

### Delete each event after one configurable window

- Good: Simplest schema and pass; storage is bounded by time.
- Bad: The window must satisfy the least private value (text) and the most useful one (counts) at once.
- Bad: Aggregate history disappears with the text.

### Keep events indefinitely and restrict access

- Good: No information is lost for review or statistics.
- Bad: Identifiable data has no end date, and there is no retention period to disclose.

## Confirmation

`tests/unit/test_moderation_retention_policy.py` fixes the parsing rules: defaults, `none`, malformed values, windows
above the ceiling, and a ceiling above 365 or disabled. `tests/unit/test_ip_subject.py` fixes the IP subject and the
pseudonym, including the refusal of a published secret. `tests/integration/test_prompt_moderation.py` fixes each
retention step, its batch bound and idempotence, notes surviving anonymization and a note stored on an anonymized
event, the exemption from every step for an event with a note, of a flagged account or of a suspicious untrusted
account (a trusted account at the threshold is not exempt), address filtering by pseudonym, the worker report records'
window, ceiling and exemption, evidence surviving an account wipe, the listing's `retention` and flags, and the privacy
document in both formats. `tests/integration/test_moderation_migration.py` verifies the nullable account
and problem-job address columns, the flags and the partial indexes, and that the models build the same moderation
tables as the migration.

## More Information

The public privacy document states the windows, the exemption for moderation action, and that deleting an account
does not remove these records; it links the source code at `HORDE_REPOSITORY`. [Moderation operations reference](../reference/moderation_operations.md) documents the variables and the
pass.
