---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Select chat service behaviour through base-URL service profiles

## Context and Problem Statement

The same question as [ADR 0020](0020-select-chat-service-by-request-body-and-model-variants.md): where does a
client's choice of Horde service behaviour travel? This record proposes the base URL as the primary surface.

## Decision Drivers

- A client configured with one base URL should get one consistent service for every request it sends.
- The path is the one configuration every OpenAI client exposes, including those without a model picker or extra
  fields.
- A path can carry an authorised private configuration without any request change.

## Considered Options

- Base-URL profiles resolved by the native service, with revisions pinned per request
- A `horde` body object plus model id variants
- Both, with the path as a static default table

## Decision Outcome

Chosen option: "Base-URL profiles resolved by the native service, with revisions pinned per request".

Each profile is a prefix such as `/openai/validated/v1`. The front maps a matched prefix to a profile reference; the
native service resolves and authorises it, pins the resolved revision on the request, and includes the profile in the
idempotency intent. Public presets start as validated configuration; saved private profiles follow when needed.

### Consequences

- Good: Works for a client that can set only the base URL.
- Good: A named preset is a dependable promise for that client.
- Bad: Invisible to model pickers; a user cannot discover presets from the model list.
- Bad: Clients that hardcode `/v1` cannot use a preset.
- Bad: Needs a resolver, a revision store and native authorisation before the first preset ships.

## Pros and Cons of the Options

### Base-URL profiles as the primary surface

- Good: One configuration point per application.
- Bad: The most infrastructure for the least client reach.

### Body object plus variants

- Good: Reaches pickers and SDKs; does not need a resolver.
- Bad: A per-request choice, so an application must send it every time.

### Both, with the path as a static default table

- Good: Keeps the single-configuration case without the resolver.
- Bad: Two ways to say one thing.

## More Information

This record and [ADR 0020](0020-select-chat-service-by-request-body-and-model-variants.md) are mutually exclusive.
Accepting one rejects the other. The review ruling R1 chose ADR 0020, with the static prefix table from the third
option kept as an alias mechanism.

Disposition: deferred until after the first text round trip, which uses exact native model ids and a small body
extension. That round trip does not require a profile resolver, revisions, saved configurations or prefix routing. This
alternative remains proposed; importing the ADRs does not accept it implicitly.
