---
title: "Prompt provenance reference"
summary: "What each stored prompt value means, who may read the original submission, and why a missing original is never filled in."
topics: [requests, moderation]
order: 60
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Prompt provenance reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [moderation](../topics.md#moderation), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

A waiting request stores two prompts: the one the client sent and the one workers receive. They differ whenever a
style is applied or the replacement filter rewrites the text.

## Code map

| Concept                            | File                                 | Symbol                                      |
| ---------------------------------- | ------------------------------------ | ------------------------------------------- |
| Effective worker input             | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.prompt`                    |
| Original submission                | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.submitted_prompt`          |
| Named consumers of the original    | `horde/classes/base/waiting_prompt.py` | `SubmittedPromptPurpose`                  |
| Privileged read                    | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.read_privileged_submitted_prompt` |
| Requester serialization            | `horde/classes/base/waiting_prompt.py` | `WaitingPrompt.get_submitted_request`     |
| Capture point                      | `horde/apis/v2/base.py`              | `GenerateTemplate._post_inner`              |
| Ownership check                    | `horde/apis/v2/base.py`              | `assert_submitting_key`                     |
| Image retrieval endpoint           | `horde/apis/v2/stable.py`            | `ImageAsyncRequest.get`                     |
| Text retrieval endpoint            | `horde/apis/v2/kobold.py`            | `TextAsyncRequest.get`                      |

## Prompt meanings

| Value                            | Meaning                                                            | Intended consumers                              |
| -------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------- |
| `WaitingPrompt.prompt`           | Effective generation input, including styles and moderation         | Generation, worker, and scheduling code          |
| `WaitingPrompt.submitted_prompt` | Parsed submission string, before styles or moderation               | Authenticated request owner, evidence capture    |
| Event `moderation_prompt`        | Input to moderation after style expansion                           | Moderator evidence review                        |

`submitted_prompt` is captured in `GenerateTemplate._post_inner` before the subclass `validate()` runs, because that
is where styles and moderation are applied. Each constructor path receives the captured string explicitly: the waiting
prompt constructor commits internally, so setting provenance afterwards would leave a committed row whose provenance is
temporarily missing.

## Access contract

The column is nullable, deferred, and configured with `raiseload=True`. Ordinary ORM queries, polymorphic loads, joins,
and relationship loads omit it, and an accidental lazy attribute access raises instead of fetching the text. Reads go
through `read_privileged_submitted_prompt(purpose=...)`, which names one of the two approved consumers in
`SubmittedPromptPurpose`. The scalar read leaves the deferred attribute unloaded on the ORM instance.

The purpose declares caller intent. It records which reviewed path reads the column, so a future consumer has to
identify itself. Authorization is a separate step, the retrieval endpoints call `assert_submitting_key`
first and serialize only once it returns.

`tests/unit/test_submitted_request.py` covers this contract, including that ORM queries and eager loads omit the
column, that the explicit read leaves it unloaded, and that the original is absent from every existing export.
`tests/integration/test_request_parameters.py` covers the retrieval endpoints: the original is returned after a
filter rewrite, a row without provenance omits the prompt, and the response is not cacheable.

## Retrieval responses

`GET /api/v2/generate/request/<id>` and `GET /api/v2/generate/text/request/<id>` return the original prompt alongside
normalized parameters, under `Cache-Control: private, no-store`. The response uses the generation input shape. It is
an approximation: parameters carry defaults and any style's overrides, while the prompt is the pre-style submission. A
client reproducing a styled request needs the style it originally specified.

## Sharp edges

- **A missing original stays missing.** Rows created before the column existed have `NULL`, and the response omits
  `prompt` entirely. Never write `submitted_prompt or prompt`, infer an original from a replacement, or backfill from
  `prompt`. The effective prompt reveals what the filters rewrote, which is the leak this column exists to close.
- **Deferred loading is an ORM rule.** The database does not enforce anything: `vars()`, `__dict__` serialization,
  enumerating ORM columns for export, `select(WaitingPrompt.__table__)`, `SELECT *`, `undefer`, and newly constructed
  instances can all reach the original prompt text. Public payloads must be built from explicit field lists.
- **The read needs a live session.** A detached instance raises. The retrieval endpoints materialize the response
  dictionary before removing the session.
- **Worker payloads and shared-image metadata keep the effective `prompt`.** Shared metadata already exposes the
  effective prompt. This contract leaves that open and must never add the original.
- **Raw-submission logging is unchanged.** `CorruptPrompt` log lines and the object-storage rejection upload still
  carry the rejected prompt, and database backups still contain originals.
- **New copy or retry paths must capture their own submission.** Directly constructed waiting prompts without
  provenance remain valid legacy rows; never copy one request's original onto another request's effective prompt.

## Related

- [Prompt moderation evidence reference](prompt_moderation_evidence.md)
