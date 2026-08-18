---
status: accepted
date: 2026-08-18
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Publish versioned sampler contracts from one authoritative registry

## Context and Problem Statement

Extended sampler support is a compatibility matrix across sampler names, schedules, numeric solver
settings, solver-type vocabularies, model baselines, measured guidance, and marginal work profiles.
Python clients can consume the SDK definitions directly. Browser frontends and other clients need the
same rules over HTTP. Workers also need a compact way to state that their adaptive execution obeys a
specific maximum-work guarantee.

Copying the matrix into the API, each frontend, and each worker lets those copies drift. Silent drift is
unsafe: an older worker can ignore an unknown setting and return a successful image that differs from
the request, while an incorrect adaptive claim can bypass a worker's workload limit. The distinct work
units used by this contract are established by [ADR 11](0011-separate-sampler-trajectory-estimate-and-ceiling.md).

## Decision Drivers

- Request validation, frontend controls, and backend behavior need the same sampler definitions.
- A client must be able to discover hard constraints without authentication or worker-endpoint access.
- Additive requester fields must preserve legacy clients and workers.
- Unknown or future worker guarantees must fail closed wherever a finite ceiling is required.
- Published guidance must remain distinguishable from hard rejection rules.

## Considered Options

- Keep one typed SDK registry and publish versioned projections of it
- Maintain independent API, frontend, and worker capability tables
- Have workers advertise a list of individual guarantee tokens
- Accept every field and let each backend ignore unsupported values

## Decision Outcome

Chosen option: "Keep one typed SDK registry and publish versioned projections of it".

The SDK owns sampler identities, accepted settings, schedule relationships, work profiles, and atomic
execution guarantees. The API validates against those definitions and exposes an anonymous
`GET /api/v2/status/sampler_constraints` document. Its `schema_version` versions the JSON shape;
`execution_contracts` publishes complete meanings for worker conformance versions. Hard constraints,
recommendations, advisories, presentation tiers, and measured ratios remain separate fields so clients
cannot mistake guidance for rejection policy.

A worker advertises one `sampler_execution_contract_version`. The server accepts only versions it can
parse and maps the version to the complete atomic guarantee set. A missing, malformed, or future version
proves no adaptive ceiling. Bridge capability gates separately prevent settings from reaching workers
that would ignore them. Legacy requester fields and payload shapes remain valid, and additive fields are
omitted from worker payloads that predate them.

### Consequences

- Good: SDK clients and HTTP clients construct controls from the rules used by server validation.
- Good: One version string gives a worker an atomic conformance claim whose meaning the public document
  fully describes.
- Good: Unknown execution versions cannot accidentally weaken worker protection.
- Good: Presentation recommendations can evolve without changing request validity.
- Bad: The API and SDK versions must be released in a compatible order.
- Bad: Non-SDK clients must cache and reject sampler-document schema versions they do not understand.
- Bad: Backend capabilities still require bridge-version gates until every worker implements the same
  requester surface.

## Pros and Cons of the Options

### Maintain independent API, frontend, and worker capability tables

- Good: Each consumer can use a shape optimized for itself.
- Bad: Every sampler addition requires synchronized edits across repositories and releases.
- Bad: Drift either rejects valid requests or silently dispatches settings a backend ignores.

### Have workers advertise a list of individual guarantee tokens

- Good: A worker can opt into guarantees independently.
- Bad: Partial combinations create execution-contract states the service must reason about and test.
- Bad: A worker can assemble a combination that no backend release implements atomically.

### Accept every field and let each backend ignore unsupported values

- Good: The API performs little compatibility work.
- Bad: A successful response no longer proves the requested sampler behavior was executed.
- Bad: Worker workload protection cannot trust adaptive ceilings.

## Confirmation

`tests/unit/test_sampler_constraints.py` compares the published document with the accepted registry and
checks its hard, advisory, presentation, measurement, and execution-contract sections.
`tests/integration/test_sampler_constraints_endpoint.py` verifies anonymous HTTP publication and strict
JSON/Swagger compatibility. Sampler and scheduler gating suites verify that old workers never receive
fields they cannot apply.
