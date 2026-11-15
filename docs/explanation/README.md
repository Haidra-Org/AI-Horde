<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Explanation

These pages explain the problems behind each subsystem and the reasons for its design.

For the alternatives considered in an individual design choice, see [Decisions](../decisions/index.md).

## Documents

<!-- BEGIN GENERATED: documents (gen_doc_index.py) -->
| Document | Summary |
| --- | --- |
| [Kudos accounting, projection, and concurrency](kudos_accounting.md) | Why kudos movements are recorded as typed events, how the projector materializes them, and how spends stay safe while balances lag. |
| [Kudos event retention](kudos_retention.md) | Why the kudos event tables keep only an online window, what replaces older rows for each dataset, and what guards each irreversible drop. |
| [Request feasibility and queue pressure](request_feasibility_and_queue_pressure.md) | Interpret request progress, worker compatibility, capacity counts, queue estimates and stall warnings. |
| [Chat requests on the Horde](chat_requests.md) | How chat requests use the text queue, worker capability declarations and a separate OpenAI-compatible front, with the design tradeoffs and client guarantees. |
<!-- END GENERATED: documents -->
