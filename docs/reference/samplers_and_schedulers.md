---
title: "Samplers and schedulers reference"
summary: "Sampler and scheduler controls, recorded SD1.5/SDXL measurements, compatibility caveats, work estimates, pricing and job leases."
topics: [generation]
order: 80
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Samplers and schedulers reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation)
<!-- END GENERATED: topics -->

Use `sampler_name` to select the numerical solver and `scheduler` to select its noise schedule. This reference
compares the available options and records combinations known to fail. [Service accounting](#service-accounting) covers how the service turns a
sampler choice into work estimates, kudos and job leases.

## What each field does

A **sampler** selects the numerical solver; a **scheduler** selects its noise schedule, the noise levels at which
steps run. Solver options can also change the output. A fixed seed alone does not guarantee identical images
across different settings or workers.

The accepted values and combinations come from `GET /api/v2/status/sampler_constraints`. Use its
`samplers`, `accepted_settings` and rejected pairings when building requests. The
[frontend integration guide](../how-to/extended_image_frontend.md) explains how to apply that contract.
[Service accounting](#service-accounting) specifies work estimates, pricing and leases independently of the
wall-time measurements recorded here.

The following measurements describe the SD1.5/SDXL setup in the next section. They do not establish universal
quality rankings, convergence guarantees or step counts for other models. The raw benchmark artifacts are not
included in this checkout; the service tests named in the code map do not reproduce them.

## Measured cost and convergence {#measured-cost-per-step-and-how-quickly-each-sampler-settles}

Wall time per render is fixed overhead (text encode, VAE decode, model juggling) plus steps times
per-step cost. Fitting time against step count separates the two, which matters because comparing raw
wall time at one step count understates a higher-order sampler's real cost by diluting it with the fixed
part. The cost figures below come from renders taken through the production pipeline at 10, 20, 30 and
40 steps, three repeats each with warmups discarded, seed and prompt and cfg scale held fixed, on both
SD1.5 at 512x512 and SDXL at 1024x1024. Each ratio is that sampler's fitted slope over `k_euler`'s slope
on the same model.

The fixed overhead excluded by the fit is around 0.29s at 512x512 and 1.40s at
1024x1024.

`settled` is the similarity between a pair's 24-step and 60-step renders. Higher means the sampler had
more nearly stopped changing by 24 steps. It is a rate proxy, not an absolute quality score, and it is
not a convergence test for stochastic samplers: changing the number of steps also changes their noise path.
Their rows record image similarity for this comparison and should not be ranked as solver accuracy.

The two cost columns are wall-time fits on one card. Read them as evidence for the fixed marginal work
families described in [Work estimates and execution ceilings](#trajectory-steps-estimated-work-and-execution-ceilings-are-different-units).
They are not prices. The learned Kudos model consumes raw trajectory steps plus sampler identity and is not multiplied
by these work rates.

The SDXL column is the one to read for what a sampler itself costs. At 1024x1024 the GPU work of a step
swamps the host-side work of issuing it, and every sampler lands within a fifth of its evaluation count.
At 512x512 a step takes around 50ms, so per-step host work is a visible fraction of it and inflates the
one-evaluation samplers by up to a third. The same samplers are clean on SDXL, which is what identifies
the effect as overhead rather than solver cost. Within the one-evaluation family the ordering of the
SDXL column is inside measurement noise and is not worth reading as a ranking.

| Sampler | Family | Cost per step, SD1.5 | Cost per step, SDXL | Settled by 24 | Notes |
| --- | --- | --- | --- | --- | --- |
| `k_euler` | ODE 1st | 1.00x | 1.00x | 0.59 | The baseline both columns are relative to. |
| `ddim` | ODE 1st | 1.13x | 0.96x | 0.59 | |
| `k_dpm_fast` | ODE 1st | 1.16x | 0.97x | 0.83 | Settled highest of the first-order solvers here. |
| `gradient_estimation` | ODE 1st | 1.18x | 0.99x | 0.56 | |
| `k_lms` | ODE 1st | 1.02x | 1.00x | 0.47 | |
| `deis` | ODE 1st | 1.13x | 0.96x | 0.46 | |
| `k_dpmpp_2m` | ODE 1st | 1.08x | 0.98x | 0.55 | One evaluation per step despite the "2M": it is multistep, reusing the previous evaluation. |
| `ddpm` | stochastic | 1.05x | 1.03x | (0.38)* | |
| `er_sde` | stochastic | 1.07x | 0.98x | (0.28)* | |
| `ipndm` | ODE 1st | 1.01x | 1.00x | 0.59 | |
| `k_euler_a` | ancestral | 1.08x | 1.01x | 0.39 | Ancestral noise changes the comparison. |
| `res_multistep` | ODE 1st | 1.01x | 0.98x | 0.55 | |
| `sa_solver` | stochastic | 1.08x | 0.94x | (0.13)* | Stochastic Adams; the low settle figure is the giveaway. |
| `uni_pc_bh2` | ODE 1st | 1.17x | 0.99x | 0.54 | |
| `dpmpp_2m_sde` | stochastic | 1.33x | 1.14x | (0.55)* | |
| `uni_pc` | ODE 1st | 1.34x | 0.98x | 0.56 | The largest gap between the two columns, and entirely the small-model overhead. |
| `k_heun` | ODE 2nd | 2.03x | 1.87x | 0.77 | Two evaluations per step. |
| `k_dpm_2` | ODE 2nd | 2.17x | 1.94x | 0.75 | Two evaluations per step; higher similarity than `k_euler` in this comparison. |
| `k_dpmpp_2s_a` | ancestral 2nd | 2.35x | 2.01x | 0.27 | Pays second-order cost without converging. |
| `k_dpmpp_sde` | stochastic 2nd | 2.58x | 2.22x | (0.51)* | |
| `heunpp2` | ODE 3rd | 3.21x | 2.93x | 0.71 | Three evaluations per step for all but the last two steps. |

\* A stochastic sampler. Its `settled` figure is not comparable with the others (see above).

`k_dpm_adaptive` publishes no ratio at all. It picks its own step count, so wall time per requested step
is not a quantity it has: the fit lands near 0.6 r-squared on both baselines against better than 0.97
everywhere else, and attributes ten seconds of a 1024x1024 render to fixed overhead.

The ordering matches theory: one-evaluation solvers sit at the baseline, two-evaluation solvers between
1.9x and 2.2x on SDXL, and the one three-evaluation solver at 2.9x. The `k_heun` and `k_dpm_2` similarity figures exceed `k_euler`'s in this comparison, at roughly double its
cost per step. Whether fewer steps compensate for that cost requires a comparison at equal elapsed time.

## Solver options

Solver options are tuning arguments in addition to the sampler name and noise schedule.
These arguments reach the solver through
`comfy.samplers.ksampler(name, extra_options)`, which builds them into the `KSAMPLER` object the solver
runs from. That is the mechanism the backend's own per-sampler nodes use: `SamplerDPMPP_2M_SDE` and its
siblings each construct a sampler with its options already baked in. `SamplerCustom` is a different node
that consumes an already-built sampler and does not pass its own options. The stock sampler node used by the
Horde's graphs does not have inputs for these options. Hordelib exposes them
(`sampler_eta`, `sampler_s_noise`, `sampler_s_churn`, `sampler_solver_type`, `sampler_order`), all
defaulting to unset so an existing request renders identically.

| Option | Applies to | What it does | Measured effect |
| --- | --- | --- | --- |
| `eta` | ancestral and SDE solvers | Stochastic strength of the reverse-time SDE. | `eta=0` collapses `dpmpp_2m_sde` onto deterministic `k_dpmpp_2m` (0.996 similarity); `eta=1` sits at 0.50 from that, `eta=2.5` at 0.38. A continuous dial from reproducible to highly varied. |
| `s_churn` | `euler`, `heun`, `dpm_2`, `heunpp2` | Karras churn: injects noise into an otherwise deterministic solver. | Adds a noise control to these solvers while retaining the seed. |
| `s_noise` | most solvers | Scale of the noise added per step. | Strong: `s_noise=1.4` on `dpmpp_2m_sde` sits at 0.30 from the default. |
| `solver_type` | the `dpmpp_2m_sde` family, `seeds_2` | `midpoint` or `heun` corrector. | Mild but real, around 0.94 from the default. |
| `order` | `deis`, `ipndm`, `k_lms`, `sa_solver` | Multistep order. | Order 1 raises inside `deis` and `ipndm`, so the accepted floor is 2. |

The request fields expose these controls independently. Compare one setting at a time with the model, prompt,
seed and other settings fixed. An observed change for one solver does not establish the same effect for every
solver that accepts the field.

## Convergence measurements {#convergence-is-rarer-than-the-folklore-suggests}

A step ladder that stops once two consecutive step counts agree above 0.98 (hordelib's *extremely similar*
band) was run across 27 samplers on two of the nine schedules. **8 of 54 pairs settled at all, and 6 of
those only at 48 steps**, the ladder's ceiling. An early stop was recorded for `k_dpm_adaptive` at 8 steps,
which follows from it choosing its own iteration count rather than the schedule's.

The 54 pairs include stochastic and ancestral samplers. Their changing noise paths make this similarity
criterion unsuitable for comparing convergence with deterministic solvers. Only two of the nine schedules were
laddered, so the recorded counts do not describe all supported schedules or models.

Two readings are required rather than one because the ladder is not monotonic: changing the step count
changes the sigma spacing, so a higher count is a different trajectory rather than a refinement of a lower
one. `k_euler` reads 0.94 between 10 and 12 steps and then falls back to 0.87 between 12 and 16.

In this SD1.5 test, additional steps continued to change the image. Similarity alone does not establish whether
the change improves quality or justifies the extra work.

## Schedules

The descriptions and visual assessments in this table record the tested setup unless a row specifies a service
compatibility rule. They are not model-independent recommendations.

`normal` and `karras` are the only schedules that the legacy `karras` flag can select. They are therefore the only two
sent to a bridge without the `scheduler` capability.

| Schedule | What it does | Where it matters |
| --- | --- | --- |
| `normal` | The model's own uniform-ish spacing. The default when `karras: false`. | Baseline. Least favourable for higher-order SDE solvers. |
| `karras` | Concentrates steps at low sigmas. The default when `karras: true`. | Fine detail and texture. Selected by the legacy `karras: true` flag. |
| `simple` | Close to `sgm_uniform` in practice; the most robust across samplers here. | Low-step comparisons in this test. |
| `sgm_uniform` | Uniform variance treatment, from score-based modelling. | Consistency-style and turbo models; low step counts. |
| `exponential` | Front-loads high-noise removal. | Composition over fine detail; shifts colour noticeably at low steps. |
| `ddim_uniform` | DDIM's original spacing. | Pairs with `ddim`. Degrades to static at very low step counts; holds up at normal ones. |
| `beta` | Beta-distribution curve shape. | Usable at normal step counts, unreliable at very low ones. |
| `linear_quadratic` | Quadratic ramp. | See the recorded failures section. |
| `kl_optimal` | KL-optimal spacing. | Usable at normal step counts; often paired with `uni_pc`. |
| `align_your_steps`, `gits` | Built by dedicated backend nodes from a fixed table of sigmas. Each is defined only for the model baselines its table was built for. horde_sdk holds that restriction. | Sent only to bridges with the `sigma_generators` capability (reGen 17 and later). |

## Known failures and sharp edges

These are measured locally on SD1.5 unless stated.

| Combination | Behaviour |
| --- | --- |
| `dpmpp_3m_sde` + `normal` | Diverges to colour noise at every step count from 8 to 50, on SD1.5 and SDXL. Reproduces through ComfyUI's own nodes, so it is the solver's constraint rather than the horde's. The backend substitutes a convergent schedule and discloses it. |
| `dpmpp_3m_sde` + `karras` | Converges at 25 steps, diverges at 8. A schedule can be safe at one step count and unsafe at another. |
| `dpmpp_3m_sde` + `simple` or `sgm_uniform` | The two schedules it converges on, which is why `simple` is the substitute. Confirmed on inspection at 25 steps on both SD1.5 and SDXL. **Not** established at 8 steps: there the substitute was judged static or overcooked on SD1.5, and underbaked on SDXL by an estimated 3 to 7 steps, so the substitution should not be relied on below roughly 12 steps. |
| `linear_quadratic`, any sampler | Blurred at every step count tested, on SD1.5 and SDXL both. At 25 steps SDXL fares somewhat better but the output was still ruled largely unusable on inspection. |
| `lcm` + `exponential` / `kl_optimal` / `linear_quadratic` | Warned against upstream; `lcm` targets very low step counts and does not tolerate elaborate schedules. Not measured here because `lcm` needs its own LoRA to be meaningful. |
| `uni_pc` + `simple` / `ddim_uniform` | Upstream reports "flat, lifeless" output. Not reproduced as a failure here at 25 steps. |
| `*_cfg_pp` samplers | Apply the CFG++ correction, which expects a `cfg_scale` of roughly 1 to 2 rather than the usual range; above that they oversaturate rather than improving adherence. Offered, and warned about rather than refused, since the image still renders. |

## Comparing solver controls

Keep the model, prompt and seed fixed when comparing controls. `sampler_eta` and `sampler_s_churn` affect noise
where the selected solver accepts them; changing the seed produces a separate comparison. The solver-options
table records measured examples. It does not guarantee a particular visual style or prompt adherence.

For distilled or turbo models, start with the model's published guidance and supported settings. Neither these
measurements nor the API's accepted range establishes one CFG value or step count that suits every model.

## Sampler selection {#choosing}

Use the contract's recommended ordering for the picker, while retaining access to other accepted samplers.
Model-specific recommendations take precedence when selecting an example to test. The
[frontend guide](../how-to/extended_image_frontend.md#render-controls-from-the-selected-sampler) describes
recommended and advanced presentation tiers.

Compare output and elapsed time at the resolution you intend to use. In the recorded SDXL measurements,
second-order solvers cost roughly twice as much per step as one-evaluation solvers. A smaller step count saves
time only when it compensates for that cost. Similarity to a higher-step render is not an absolute quality score.

For repeatability, retain the model, seed and all settings, and compare on the same worker. The Horde cannot
promise bit-identical output across workers, whose hardware, backend and loaded model configuration can differ.

## Service accounting

### Work estimates and execution ceilings {#trajectory-steps-estimated-work-and-execution-ceilings-are-different-units}

Step count alone does not describe the work of a sampler. The service distinguishes requested steps, estimated
work and an execution ceiling so that pricing and worker limits use the appropriate quantity. These distinctions
are recorded in [ADR 11](../decisions/0011-separate-sampler-trajectory-estimate-and-ceiling.md), and the published
contract is described in [ADR 12](../decisions/0012-publish-versioned-sampler-contracts.md).

Heun evaluates the model twice per step, while Euler evaluates it once. Compare their render time as well as
step counts. Under the SDK's marginal work rates, 15 Heun steps and 30 Euler steps each cost 30 work units.

The SDK expresses this difference as **sampler work**. One work unit approximates the additional computation
for one ordinary first-order model evaluation with the same input. Fixed samplers have a one-, two-, or
three-unit marginal rate. The unit is not an exact evaluation count, because terminal steps and solver reuse make
the exact number of evaluations differ slightly. It is not wall time either, because hardware and payload overhead
still matter.

`k_dpm_adaptive` does not have a fixed rate, because it chooses its own iteration count and does not follow the
schedule. AI-Horde uses a stable 40-work-unit request estimate for it, for usage, TTL and upfront policy
(`AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY`). A backend may separately advertise sampler execution contract `1.0`,
which contains the atomic `bounded_dpm_adaptive_v1` guarantee: at most `ceil(1.25 * trajectory_steps)` solver
iterations. Each iteration costs the requested solver order (two or three work units), so a 20-step request has a
hard ceiling of 50 or 75 work units. The estimate and the ceiling are separate quantities, and one is never used in
place of the other.

The public sampler-constraints document includes the information needed to interpret its fields.
Its `schema_version` identifies the JSON format.
`execution_contracts` publishes each worker conformance version and the complete formulas behind its atomic
guarantees. A worker reports only `sampler_execution_contract_version`. It does not need to assemble or interpret a
list of individual guarantees.

### Pricing {#what-this-means-for-pricing}

Kudos for an image request comes from a small trained model that predicts how long the job will take. Its inputs
include the raw requested step count and a one-hot slot for the sampler, so the cost of each sampler is learned.
`k_heun` is priced above `k_euler` because the model learned that it takes longer. Nothing multiplies its step count.

Two consequences follow:

- A sampler that the model was not trained on does not have a slot of its own. Before the lookup,
  `CANONICAL_KUDOS_SAMPLERS` in `horde/classes/stable/kudos.py` maps it to a trained slot with the same marginal
  work profile. Every sampler added after the checkpoint was frozen gets its price this way, so adding samplers does
  not reprice any sampler already served. A name that is in neither the map nor the trained list is priced as
  `k_euler`.
- The trained vocabulary tops out at two evaluations per step. The three-evaluation solvers (`heunpp2`, `seeds_3`)
  map to the `k_heun` slot and are underpriced by roughly a third. Correcting that needs a retrained checkpoint.

Estimated work is used for quantities that scale with expected compute: job TTL, upfront policy and usage totals. A
worker that opts into `limit_max_steps` uses maximum work instead. Fixed samplers have a finite ceiling derived from
their work profile. Adaptive work requires an explicitly advertised backend execution contract.

### Job lease allowances {#why-job-ttl-is-longer-than-isolated-inference}

The [image lease constants](service_limits.md#image-leases-and-sampler-work) collect these allowances, their rationale
and their defining and consuming symbols alongside the other worker deadlines.

The lease policy and its alternatives are recorded in
[ADR 13](../decisions/0013-image-job-ttl-is-a-prefetch-lease.md).

The job TTL begins when a worker pops an assignment and ends when it submits the result. It is a lease and does not
forecast inference time. Workers are encouraged to keep a shallow local look-ahead queue, so they can load a model or
fetch LoRAs while inference for the preceding assignment is still running. Queue residence, storage and network
variance, and inference all have to fit inside the same lease.

For an ordinary assignment the service starts with:

```text
30 seconds + 2 seconds * estimated work * (width * height / 512^2)
```

The scalable term corresponds to 0.131072 megapixel-work units per second. The normal-speed worker
classification uses 0.5 MPS, so the lease provides about 3.8 times the isolated compute time of a
worker exactly at that threshold. Requests that opt into slow workers can still be served below it.
If one equally expensive assignment is already ahead in the local queue, the lease is about 1.9 times their combined
compute time at that threshold. The allowance includes time for prefetching and runtime variance. It
does not mean that median hardware needs two seconds for a 512-square first-order step.

`ImageProcessingGeneration.set_job_ttl` then adjusts the lease in this order:

1. ControlNet doubles it.
2. The baseline of the assigned model multiplies it by the baseline's `horde_policy.ttl` from the served catalog
   (see [baseline policy](baseline_policy.md)). In the catalog packaged with `horde_model_reference`, the Flux,
   Qwen Image, Z-Image Turbo, Krea2 Turbo, Moss Image and Anima baselines have 3, and the Stable Diffusion and
   Stable Cascade baselines have 1. A baseline that is not in the catalog has 1.
3. A 150-second minimum applies. It protects small jobs, where fixed model and asset preparation can dominate.
4. Each requested LoRA adds a fixed download allowance of about 107 seconds (a 400 MB file at 30 Mbps,
   `LORA_DOWNLOAD_SECONDS`).
5. A worker explicitly marked extra-slow receives three times the result.

Combinations can therefore produce intentionally large deadlines. This ordering is part of the worker pop contract.
Do not rearrange it as a mathematical simplification.

Sampler work is the proportional input, so fixed higher-order samplers receive two or three times the scalable
allowance. `k_dpm_adaptive` uses the service's stable 40-work-unit estimate. Its backend execution ceiling is a
safety bound and does not forecast runtime, so it is not used in the TTL. Schedulers and solver presentation controls
do not change the lease. Source processing, post-processing and hires fix do not change it either, because the shared
fixed and queue allowances cover their ordinary setup cost. LoRAs are the exception, because a cache miss on several
maximum-size files costs more than the whole floor.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Sampler and scheduler vocabularies | `horde/consts.py` | `KNOWN_SAMPLERS`, `EXTENDED_SCHEDULERS`, `SIGMA_GENERATOR_SCHEDULERS` |
| Published constraints and execution contracts | `horde/sampler_constraints.py` | `compile_sampler_constraints`, `published_sampler_constraints` |
| Bridge capability and sampler support | `horde/bridge_reference.py` | `check_bridge_capability`, `get_supported_samplers` |
| Learned pricing vocabulary | `horde/classes/stable/kudos.py` | `KudosModel`, `CANONICAL_KUDOS_SAMPLERS` |
| Work rates and adaptive execution bounds | `horde_sdk.generation_parameters.image.sampler_work` | `SAMPLER_WORK_PROFILES`, `SamplerExecutionContractVersion` |
| Adaptive work estimate | `horde/sampler_work_policy.py` | `AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY` |
| Job lease | `horde/classes/stable/processing_generation.py` | `ImageProcessingGeneration.set_job_ttl`, `LORA_DOWNLOAD_SECONDS` |

`tests/unit/test_sampler_constraints.py` checks the published contract against the registry.
`tests/unit/test_kudos_pricing.py` covers pricing, and `tests/unit/test_bridge_reference.py` covers bridge gating.
These tests check service contracts, rather than reproducing the benchmark measurements above.
