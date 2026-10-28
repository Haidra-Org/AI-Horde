---
status: rejected
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Select chat service behaviour through base-URL service profiles

## Context and Problem Statement

A chat client must be able to choose Horde service behaviour, such as trusted workers or validated backends, without
a Horde-specific client. This record evaluates the base URL as the primary place that choice travels.
[ADR 0020](0020-select-chat-service-by-request-body-and-model-variants.md) is the accepted decision on this question.

## Decision Drivers

- A client configured with one base URL should get one consistent service for every request it sends.
- The path is the one configuration every OpenAI client exposes, including those without a model picker or extra
  fields.
- A path can carry an authorised private configuration without any request change.

## Considered Options

- Base-URL profiles resolved by the native service, with revisions pinned per request
- A `horde` body object
- Both, with the path as a static default table

## Decision Outcome

Rejected. The evaluated option maps a prefix such as `/openai/validated/v1` to a profile reference. The native
service resolves and authorises the profile, pins the resolved revision on the request, and includes the profile in
the operation key's fingerprint. Public presets are validated configuration, and a user can save private profiles.

### Consequences

- Good: Works for a client that can set only the base URL.
- Good: A named preset is a dependable promise for that client.
- Bad: Invisible to model pickers, so a user cannot discover presets from the model list.
- Bad: Clients that hardcode `/v1` cannot use a preset.
- Bad: Needs a resolver, a revision store and native authorisation before any preset is served.

## Pros and Cons of the Options

### Base-URL profiles

- Good: One configuration point per application.
- Bad: The most infrastructure for the least client reach.

### A `horde` body object

- Good: Reaches every SDK through extra body fields and does not need a resolver.
- Bad: A per-request choice, so an application sends it every time.

### Both, with the path as a static default table

- Good: Keeps the single-configuration case without the resolver.
- Bad: Two ways to say one thing.
