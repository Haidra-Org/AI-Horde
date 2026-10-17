---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Select chat service behaviour through the request body and model id variants

## Context and Problem Statement

A chat client must be able to choose Horde service behaviour such as validated backends, trusted workers or faster
workers without a Horde-specific client. OpenAI clients expose three places such a choice can travel: the model id,
extra request body fields, and the base URL. Which of these carries service selection?

## Decision Drivers

- An unmodified client that can only set base URL, key and model must still be able to select behaviour.
- Model pickers built from the model list must show what a user can select.
- Clients that hardcode `/v1` must not lose the selection.
- One handler stack, without a resolver, revisions or saved profiles until a concrete need exists.
- The closest comparable service, OpenRouter, uses a body object and `:suffix` variants, and its clients implement
  the suffix resolution rule.

## Considered Options

- A `horde` body object plus `:variant` suffixes on the model id, with base-URL prefixes as optional aliases
- Base-URL service profiles as the primary surface, resolved by the native service with revisions
- Account-level defaults only

## Decision Outcome

Chosen option: "A `horde` body object plus `:variant` suffixes on the model id, with base-URL prefixes as optional
aliases".

The `horde` object is the primary extension and borrows OpenRouter's vocabulary where the concept exists (`only`,
`ignore`, `require_parameters`, `quantizations`, `max_kudos`). Variants `:validated`, `:fast` and `:trusted` each set
one native boolean, combine in any order, and are refused with 404 when unknown. A base-URL prefix is a static table
entry that supplies `horde` defaults before validation; explicit request values win. The response `model` is always
the base name.

### Consequences

- Good: Works through `extra_body` in every SDK and through the model field in every picker.
- Good: A prefix still serves a client that owns its model list and cannot send fields.
- Bad: Variant names are a public vocabulary that must be documented and kept small.
- Bad: Base model names do not contain a colon, by operational rule. The front resolves a variant suffix before a
  request reaches the native route, so workers never see it. The live model list and the text model reference do
  not contain such a name.

## Pros and Cons of the Options

### Body object plus variants

- Good: Matches what OpenRouter-aware clients send unprompted.
- Bad: A guarantee cannot be expressed; a preset is defaults, and a hard constraint belongs on the credential.

### Base-URL profiles as the primary surface

- Good: A client configured once gets the behaviour for everything it sends.
- Bad: Invisible to pickers; lost by clients that hardcode the path; needs a resolver, revision pinning and an
  idempotency binding to be safe, which is most of the original design's length.

### Account defaults only

- Good: Does not change the request or the path.
- Bad: One credential cannot serve two applications with different needs.

## More Information

This record and [ADR 0021](0021-select-chat-service-by-base-url-profiles.md) are mutually exclusive. Accepting one
rejects the other. The design author's original direction was ADR 0021; the review ruling R1 chose this record.

Sequencing: the first text round trip ships only the small body object for implemented native selection and
context behaviour. Variants, prefix aliases and additional options follow it; until then model ids are exact native
names. The colon stays reserved for variants, which does not cost anything while advertised and referenced text model
names do not contain one. This record's full alias surface and the competing profile resolver do not block the
first text round trip.
