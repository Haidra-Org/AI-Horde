---
status: accepted
date: 2026-08-18
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Treat image job TTL as a conservative prefetch lease

## Context and Problem Statement

An image job's TTL starts when a worker pops the assignment and ends when it submits the result. Workers
benefit from a shallow local look-ahead queue: model loading, LoRA downloads, and other preparation for
the next assignment can overlap inference already in progress. A deadline sized only for isolated
inference would discourage that prefetch and expire valid jobs while they wait locally.

The lease still needs to scale with payload cost. Requested trajectory steps alone undercount
higher-order samplers, as decided in
[ADR 11](0011-separate-sampler-trajectory-estimate-and-ceiling.md). Model preparation and short-job
overhead do not scale cleanly with pixel-work, and known slow paths need additional room. Excessively long
leases delay recovery when a worker disappears, so every allowance has a stale-detection cost.

## Decision Drivers

- Workers should be able to prefetch a small number of jobs and overlap model or asset I/O.
- Fixed higher-order samplers must receive time proportional to estimated marginal work.
- Short jobs need enough fixed time for model and asset preparation.
- The deadline must remain deterministic at pop time and independent of worker-reported queue internals.
- Existing ControlNet, slow-model, and extra-slow-worker behavior must remain compatible.

## Considered Options

- Use a conservative deterministic pop-to-submit lease based on estimated work
- Predict completion from rolling worker speed and outstanding jobs
- Start or renew the lease through a worker execution acknowledgement
- Size the lease from requested trajectory steps alone

## Decision Outcome

Chosen option: "Use a conservative deterministic pop-to-submit lease based on estimated work".

For estimated sampler work `W` and pixel ratio `P = width * height / 512^2`, an ordinary assignment
starts with `30 + 2 * W * P` seconds. ControlNet multiplies that value by two. An assignment whose
selected model has a Flux, Qwen Image, or Z-Image Turbo baseline multiplies it by three. The service then
applies a 150-second minimum; an explicitly extra-slow worker receives three times the resulting lease.
The final value rounds upward to whole seconds for database and wire compatibility.

The scalable term corresponds to 0.131072 megapixel-work units per second. Relative to the 0.5-MPS
normal-speed classification, it provides about 3.8 times isolated compute time. With one equally
expensive assignment ahead locally, it provides about 1.9 times their combined compute time. This margin
covers shallow prefetch, preparation, and runtime variance. The multiplier order is part of the policy:
moving a multiplier across the minimum changes short-job leases.

`k_dpm_adaptive` uses the stable request estimate from ADR 11. Its maximum execution ceiling is a safety
bound rather than a runtime forecast and does not replace the TTL estimate.

### Consequences

- Good: A normal-speed worker can keep one comparable assignment prefetched with substantial variance
  and I/O headroom.
- Good: Time budgets scale across one-, two-, and three-unit fixed sampler families.
- Good: The minimum protects short jobs whose preparation dominates inference.
- Good: The selected procgen model controls the slow-model factor, so another model listed on a
  multi-model request does not inflate an ordinary assignment.
- Bad: The lease is deliberately longer than median isolated inference and delays reassignment after a
  worker disappears.
- Bad: ControlNet, slow-model, and extra-slow factors compound into very large deadlines.
- Bad: The stable adaptive estimate can over-budget short adaptive runs and under-budget an unusually
  expensive tail.
- Bad: LoRAs, source processing, post-processing, and hires-fix have no independent factors; their
  ordinary setup cost relies on the shared fixed and queue allowances.

## Pros and Cons of the Options

### Predict completion from rolling worker speed and outstanding jobs

- Good: The deadline can track observed hardware and active load.
- Bad: Aggregate speed is stale, model-dependent, and partly worker-reported.
- Bad: Server-visible outstanding work does not reveal a worker's actual local execution order or
  preparation overlap.

### Start or renew the lease through a worker execution acknowledgement

- Good: Queue residence and active execution receive separate deadlines.
- Good: Heartbeats can reclaim disappeared workers without shortening valid long generations.
- Bad: This requires a new worker protocol and persistent lease-renewal state across the fleet.
- Bad: Legacy workers still need the deterministic pop-time lease during migration.

### Size the lease from requested trajectory steps alone

- Good: The calculation is simple and preserves the historic input.
- Bad: It under-budgets samplers with two or three marginal work units per trajectory step.
- Bad: Adaptive requested steps do not predict adaptive iteration work.

## Confirmation

`tests/unit/test_trajectory_and_cost_steps.py` fixes the effective MPS, one-prefetched-job headroom,
minimum, sampler scaling, assigned-model behavior, modifier order, rounding, and pop-response TTL.
`docs/reference/samplers_and_schedulers.md` records the public formula and the distinction between an
execution estimate and a maximum-work ceiling.
