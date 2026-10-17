---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Keep anonymous access on the chat front

## Context and Problem Statement

The pilot proxy at oai.aihorde.net accepts the anonymous key. The chat front maps a missing `Authorization` header
to that key. Anonymous access makes the front an open target for agent loops and proxy scrapers on a DNS-only
hostname without edge protection. Does the front require a registered key?

## Decision Drivers

- Anonymous use is the service's core proposition for the average user.
- Abuse must be bounded without removing the feature.
- The Horde already limits anonymous requests and ranks them last by kudos.

## Considered Options

- Keep anonymous access; bound abuse with the existing anonymous limits and per-source caps on the front
- Require a registered key on the front
- Anonymous access for non-streamed requests only

## Decision Outcome

Chosen option: "Keep anonymous access; bound abuse with the existing anonymous limits and per-source caps on the
front".

A missing `Authorization` header is the anonymous key. The front adds a per-source inflight cap beside its global
cap, HAProxy adds a stick table on the listener, and the proxy passkey keys the Horde's limiter on the end-user
address. The front does not otherwise distinguish anonymous requests.

### Consequences

- Good: The product proposition is preserved.
- Bad: Anonymous agent loops consume capacity; priority by kudos and the caps are the only brake.

## Pros and Cons of the Options

### Keep anonymous access

- Good: Core feature.
- Bad: Abuse surface.

### Require a key

- Good: Attribution for every request.
- Bad: Changes what the service is.

### Anonymous for non-streamed only

- Good: Streams, the long-held connections, need a key.
- Bad: An arbitrary line that pushes anonymous users to the worse mode.

## More Information

First-release operational requirements: enforce bytes and body-read time before JSON materialization, bound response writes
and total streamed/non-streamed lifetime, and sanitize forwarded identity at the trusted listener. Process-local
source caps multiply with process count and aggregate users behind NAT; test service-wide policy. Separate cancel
connections do not reserve native execution capacity. These bounds do not introduce a registered-key requirement.

Ruling R19 of the review.
