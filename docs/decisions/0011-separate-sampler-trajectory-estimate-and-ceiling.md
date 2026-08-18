---
status: accepted
date: 2026-08-18
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Separate sampler trajectory, estimated work, and execution ceilings

## Context and Problem Statement

An image request's `steps` field describes progress along a denoising trajectory. It does not describe
one portable unit of compute: fixed higher-order samplers can evaluate the model two or three times per
trajectory step, multistep samplers reuse prior evaluations, and an adaptive sampler chooses its own
iteration count. Reusing `steps` for convergence limits, usage, time budgets, and worker protection
therefore assigns several incompatible meanings to one number.

The service needs to preserve the user's requested trajectory while accounting for expected marginal
inference and protecting workers from a maximum workload. Adaptive execution makes the last two
quantities different: a stable estimate is useful for ordinary policy, while a proven ceiling requires
a backend execution guarantee.

## Decision Drivers

- Convergence checks must continue to describe requested trajectory progress.
- Usage, TTL, upfront policy, and downgrade policy must scale with expected marginal inference.
- A worker's workload limit must use a finite upper bound rather than an expectation.
- Sampler accounting must remain portable across API, SDK, backend, and worker implementations.
- Stored legacy payloads with an already-validated unknown sampler need deterministic compatibility.

## Considered Options

- Represent trajectory steps, estimated work, and maximum work as distinct typed quantities
- Continue using requested steps for every policy
- Count exact model evaluations after execution and use that value everywhere
- Treat the estimated work value as the worker-protection ceiling

## Decision Outcome

Chosen option: "Represent trajectory steps, estimated work, and maximum work as distinct typed
quantities".

`TrajectoryStepCount` carries requested schedule progress. `SamplerWorkEstimate` carries
first-order-equivalent marginal work for usage and operational planning. `SamplerWorkCeiling` carries a
finite maximum only when the sampler profile or an advertised execution contract proves one. Fixed
samplers derive estimate and ceiling from their one-, two-, or three-unit marginal work rate.
AI-Horde assigns `k_dpm_adaptive` a stable 40-work-unit estimate; its ceiling remains unavailable unless
the worker advertises a contract that bounds adaptive execution. Unknown sampler strings found in
legacy stored payloads use the first-order compatibility profile.

Learned kudos pricing remains a separate forecast. Its model already consumes trajectory steps and
sampler identity, so multiplying its result by operational work would count sampler cost twice.

### Consequences

- Good: Higher-order samplers receive proportional usage and time budgets without changing the
  requested trajectory.
- Good: Model convergence rules and request step limits retain their user-facing meaning.
- Good: Worker workload limits fail closed when adaptive execution has no proven finite ceiling.
- Good: Unit-bearing SDK types make accidental estimate/ceiling substitution visible in code review and
  static analysis.
- Bad: Call sites must choose among three related values and carry their units explicitly.
- Bad: Adaptive accounting intentionally accepts a gap between expected and maximum work.
- Bad: The legacy unknown-sampler fallback is less strict than the SDK registry and must stay confined to
  payloads that passed validation when stored.

## Pros and Cons of the Options

### Continue using requested steps for every policy

- Good: One value flows through every existing call site.
- Bad: It undercounts fixed higher-order samplers and cannot express adaptive maximum work.
- Bad: Increasing an accounting value would incorrectly alter trajectory-quality decisions.

### Count exact model evaluations after execution and use that value everywhere

- Good: Settlement could describe work already performed precisely.
- Bad: Admission, TTL, downgrade, and worker matching need a value before execution begins.
- Bad: Backend-specific evaluation details are not a portable requester contract.

### Treat the estimated work value as the worker-protection ceiling

- Good: Every request has one finite number before dispatch.
- Bad: An expectation provides no safety guarantee for an adaptive tail and can admit work above a
  worker's configured limit.

## Confirmation

`tests/unit/test_trajectory_and_cost_steps.py` fixes the trajectory/work split at usage and TTL call
sites. `tests/unit/test_sampler_work_policy.py` fixes adaptive estimate, ceiling, compatibility, and
budget behavior. Worker `limit_max_steps` coverage confirms that an adaptive request without a known
execution contract fails closed.
