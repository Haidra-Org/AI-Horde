---
title: "Samplers and schedulers reference"
summary: "What sampler_name and scheduler select, why deterministic samplers agree once converged, measured steps-to-converge and cost, and the combinations known to fail."
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

What the `sampler_name` and `scheduler` fields actually select, what separates the options, and which
combinations are known to fail.

## What each field does

A **sampler** is the numerical method that integrates the reverse diffusion process. A **scheduler** (a
sigma schedule) decides *where in the noise range* the sampler's steps land. The scheduler does not
change how many steps are taken, so it does not change cost: measured across all nine schedules, mean
render time varied between 2.29s and 2.36s, which is noise.

Two consequences follow, and they explain most confusion about these fields:

**Deterministic samplers agree with each other once converged.** Most samplers integrate the same
probability-flow ODE, so at sufficient steps they arrive at the same image. If two of them produce
near-identical output at 25 steps, that is the correct result and not a defect. What separates them is
*how few steps they need to get there*, not where they end up.

**Only stochastic samplers produce genuinely different images.** Ancestral (`_a` suffix) and SDE samplers
inject fresh noise at each step, so they have no fixed point to converge to and will keep producing
different output at any step count. This is why they look distinct in side-by-side comparisons while the
deterministic ones do not.

## Trajectory steps, estimated work, and execution ceilings are different units

A second-order sampler evaluates the model twice per step, so comparing samplers at equal step counts
charges them unequally. The correct comparison is at equal wall time: published guidance puts it as
comparing Heun at 30 steps against Euler at 15

The SDK calls this relationship **sampler work**: one work unit approximates the marginal inference work
of an ordinary first-order model evaluation at the same payload. Fixed samplers carry a one-, two-, or
three-unit marginal rate. This is intentionally not called an exact evaluation count because terminal
steps and solver reuse make exact NFE differ slightly, and it is not wall time because hardware and
payload overhead still matter.

`k_dpm_adaptive` is excluded from the comparisons below because it chooses its own iteration count rather
than following the schedule. AI-Horde uses a stable 40-work-unit request estimate for usage, TTL, and
upfront policy. A backend may separately advertise sampler execution contract `1.0`, which contains
the atomic `bounded_dpm_adaptive_v1` guarantee: at most `ceil(1.25 * trajectory_steps)` solver
iterations. Each iteration costs the requested solver order—two or three work units—so a 20-step
request has a hard ceiling of 50 or 75 work units respectively. An estimate and a ceiling answer
different questions and are never substituted for one another.

The public sampler-constraints document is self-describing. Its `schema_version` identifies the JSON
shape, while `execution_contracts` publishes each worker conformance version and the complete formulas
behind its atomic guarantees. A worker reports only `sampler_execution_contract_version`; it does not
need to assemble or interpret a list of individual guarantees.

## Measured: cost per step and how quickly each sampler settles

Wall time per render is fixed overhead (text encode, VAE decode, model juggling) plus steps times
per-step cost. Fitting time against step count separates the two, which matters because comparing raw
wall time at one step count understates a higher-order sampler's real cost by diluting it with the fixed
part. The cost figures below come from renders taken through the production pipeline at 10, 20, 30 and
40 steps, three repeats each with warmups discarded, seed and prompt and cfg scale held fixed, on both
SD1.5 at 512x512 and SDXL at 1024x1024. Each ratio is that sampler's fitted slope over `k_euler`'s slope
on the same model. The fixed overhead the fit sets aside is around 0.29s at 512x512 and 1.40s at
1024x1024.

`settled` is the similarity between a pair's 24-step and 60-step renders. Higher means the sampler had
more nearly stopped changing by 24 steps. It is a rate proxy, not an absolute quality score, and it is
**meaningless for stochastic samplers**, which have no fixed point and cannot settle by construction.
Their rows are retained for completeness rather than for comparison.

The two cost columns are wall-time fits on one card. Read them as evidence for the fixed marginal work
families rather than as a price. The learned Kudos model consumes raw trajectory steps plus sampler
identity and is not multiplied by these work rates.

The SDXL column is the one to read for what a sampler itself costs. At 1024x1024 the GPU work of a step
swamps the host-side work of issuing it, and every sampler lands within a fifth of its evaluation count.
At 512x512 a step takes around 50ms, so per-step host work is a visible fraction of it and inflates the
one-evaluation samplers by up to a third; the same samplers are clean on SDXL, which is what identifies
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
| `ddpm` | stochastic | 1.05x | 1.03x | ~~0.38~~ | |
| `er_sde` | stochastic | 1.07x | 0.98x | ~~0.28~~ | |
| `ipndm` | ODE 1st | 1.01x | 1.00x | 0.59 | |
| `k_euler_a` | ancestral | 1.08x | 1.01x | 0.39 | Does not converge by design. |
| `res_multistep` | ODE 1st | 1.01x | 0.98x | 0.55 | |
| `sa_solver` | stochastic | 1.08x | 0.94x | ~~0.13~~ | Stochastic Adams; the low settle figure is the giveaway. |
| `uni_pc_bh2` | ODE 1st | 1.17x | 0.99x | 0.54 | |
| `dpmpp_2m_sde` | stochastic | 1.33x | 1.14x | ~~0.55~~ | |
| `uni_pc` | ODE 1st | 1.34x | 0.98x | 0.56 | The largest gap between the two columns, and entirely the small-model overhead. |
| `k_heun` | ODE 2nd | 2.03x | 1.87x | 0.77 | Two evaluations per step. |
| `k_dpm_2` | ODE 2nd | 2.17x | 1.94x | 0.75 | Two evaluations per step; settled highest overall. |
| `k_dpmpp_2s_a` | ancestral 2nd | 2.35x | 2.01x | 0.27 | Pays second-order cost without converging. |
| `k_dpmpp_sde` | stochastic 2nd | 2.58x | 2.22x | ~~0.51~~ | |
| `heunpp2` | ODE 3rd | 3.21x | 2.93x | 0.71 | Three evaluations per step for all but the last two steps. |

`k_dpm_adaptive` publishes no ratio at all. It picks its own step count, so wall time per requested step
is not a quantity it has: the fit lands near 0.6 r-squared on both baselines against better than 0.97
everywhere else, and attributes ten seconds of a 1024x1024 render to fixed overhead.

The ordering matches theory: one-evaluation solvers sit at the baseline, two-evaluation solvers between
1.9x and 2.2x on SDXL, and the one three-evaluation solver at 2.9x. The two highest `settled` figures
belong to second-order solvers, which is the trade being made: they cost roughly double per step and
reach their answer in fewer steps. Whether that is a net win depends on the step count, which is why the
honest comparison is at equal time.

### What this means for pricing

Kudos for an image request comes from a small trained model that predicts how long the job will take. It
is given the raw requested step count and a one-hot slot for the sampler, so **per-sampler cost is
already learned rather than derived**: `k_heun` is priced above `k_euler` because the model learned it
takes longer, not because anything multiplies its step count.

Two consequences are worth stating, because both are easy to assume otherwise:

- A sampler the model was never trained on has no slot of its own. It is collapsed onto a trained slot
  with the same marginal work profile before the lookup (`CANONICAL_KUDOS_SAMPLERS` in
  `horde/classes/stable/kudos.py`), which is where every sampler added after the checkpoint was frozen
  gets its price. Adding samplers therefore reprices nothing that was already being served.
- The trained vocabulary tops out at two evaluations per step, so the three-evaluation solvers
  (`heunpp2`, `seeds_3`) take the most expensive slot available and are under-priced by roughly a third.
  Correcting that needs a retrained checkpoint rather than a multiplier.

Estimated work is used for quantities that scale with expected compute: job TTL, upfront policy, and
usage totals. A worker opting into `limit_max_steps` instead uses maximum work: fixed samplers have a
finite profile-derived ceiling, while adaptive work requires an explicitly advertised backend
execution contract.

### Why job TTL is longer than isolated inference

The job TTL begins when a worker pops an assignment and ends when it submits the result. It is a lease,
not a benchmark forecast. Workers are encouraged to keep a shallow local look-ahead queue so they can
load a model or fetch LoRAs while inference for the preceding assignment is still running. Queue
residence, storage and network variance, and inference all have to fit inside the same lease.

For an ordinary assignment the service starts with:

```text
30 seconds + 2 seconds * estimated work * (width * height / 512^2)
```

The scalable term corresponds to 0.131072 megapixel-work units per second. The normal-speed worker
classification uses 0.5 MPS, so the lease provides about 3.8 times the isolated compute time of a
worker exactly at that threshold. Requests which opt into slow workers can still be served below it.
If one equally expensive assignment is already ahead in the local queue, the normal-speed comparison
becomes about 1.9 times their combined compute time. This is deliberately conservative: the difference
is capacity for prefetching and runtime variance, not a claim that median hardware needs two seconds
for a 512-square first-order step.

A 150-second minimum protects small jobs, for which fixed model and asset preparation can dominate.
ControlNet doubles the computed lease; an assignment whose selected model has a Flux, Qwen Image, or
Z-Image Turbo baseline triples it. Those factors compound before the minimum is applied. A worker
explicitly marked extra-slow receives three times the resulting lease after the minimum. Consequently,
combinations can produce intentionally large deadlines. This ordering is part of the worker pop
contract and should not be rearranged as a mathematical simplification.

Sampler work is the proportional input, which is why fixed higher-order samplers receive two or three
times the scalable allowance. `k_dpm_adaptive` instead uses the service's stable 40-work-unit estimate;
its backend execution ceiling is a safety bound, not a runtime forecast, and is not substituted into
TTL. Schedulers and solver presentation controls do not independently change the lease. Neither do
LoRAs, source processing, post-processing, or hires-fix: their ordinary setup cost is covered by the
shared fixed and queue allowances rather than by a separate per-feature multiplier.

## Solver options

A sampler takes tuning arguments beyond its name and schedule. They reach the solver through
`comfy.samplers.ksampler(name, extra_options)`, which builds them into the `KSAMPLER` object the solver
runs from. That is the mechanism the backend's own per-sampler nodes use: `SamplerDPMPP_2M_SDE` and its
siblings each construct a sampler with its options already baked in. `SamplerCustom` is a different node
that consumes an already-built sampler and passes no options of its own. The stock sampler node the
horde's graphs are built around has no inputs for them, so they
were therefore pinned at their defaults and unreachable from any horde request. hordelib now exposes them
(`sampler_eta`, `sampler_s_noise`, `sampler_s_churn`, `sampler_solver_type`, `sampler_order`), all
defaulting to unset so an existing request renders identically.

| Option | Applies to | What it does | Measured effect |
| --- | --- | --- | --- |
| `eta` | ancestral and SDE solvers | Stochastic strength of the reverse-time SDE. | `eta=0` collapses `dpmpp_2m_sde` onto deterministic `k_dpmpp_2m` (0.996 similarity); `eta=1` sits at 0.50 from that, `eta=2.5` at 0.38. A continuous dial from reproducible to highly varied. |
| `s_churn` | `euler`, `heun`, `dpm_2`, `heunpp2` | Karras churn: injects noise into an otherwise deterministic solver. | The only way to get run-to-run variation out of the deterministic solvers without changing the seed. |
| `s_noise` | most solvers | Scale of the noise added per step. | Strong: `s_noise=1.4` on `dpmpp_2m_sde` sits at 0.30 from the default. |
| `solver_type` | the `dpmpp_2m_sde` family, `seeds_2` | `midpoint` or `heun` corrector. | Mild but real, around 0.94 from the default. |
| `order` | `deis`, `ipndm`, `k_lms`, `sa_solver` | Multistep order. | Order 1 raises inside `deis` and `ipndm`, so the accepted floor is 2. |

Why this matters for the request vocabulary: `eta` is the only *controllable* variation dial the stack
has. Changing the seed varies everything at once and cannot be dialled; `eta` moves continuously from
"give me exactly this image again" to "explore around this image". Options are filtered per sampler at the
point the sampler is built, because handing `sample_euler` an `eta` raises `TypeError` from inside graph
execution rather than being rejected as a bad argument.

### Convergence is rarer than the folklore suggests

A step ladder that stops once two consecutive step counts agree above 0.98 (hordelib's *extremely similar*
band) was run across 27 samplers on two of the nine schedules. **8 of 54 pairs settled at all, and 6 of
those only at 48 steps**, the ladder's ceiling. The single early stop was `k_dpm_adaptive` at 8 steps,
which follows from it choosing its own iteration count rather than the schedule's.

That headline overstates the case in two ways worth correcting. The stochastic and ancestral samplers are
counted in the 54 but **cannot satisfy a two-in-a-row similarity bar at any step count**: they inject
fresh noise every step and have no fixed point, so their non-convergence is a definition rather than a
finding, and including them inflates the ratio. The meaningful denominator is the deterministic pairs
alone. Separately, only two of the nine schedules were laddered, so none of this is a statement about the
schedule space.

Two readings are required rather than one because the ladder is not monotonic: changing the step count
changes the sigma spacing, so a higher count is a different trajectory rather than a refinement of a lower
one. `k_euler` reads 0.94 between 10 and 12 steps and then falls back to 0.87 between 12 and 16.

The practical consequence: at 512px on SD1.5, raising steps keeps changing the image well past the point
most guidance implies it stops mattering, though the cost vs benefit of doing this is still questionable.

## Schedules

| Schedule | What it does | Where it matters |
| --- | --- | --- |
| `normal` | The model's own uniform-ish spacing. The default when `karras: false`. | Baseline. Least favourable for higher-order SDE solvers. |
| `karras` | Concentrates steps at low sigmas. The default when `karras: true`. | Fine detail and texture. The long-standing horde default for quality. |
| `simple` | Close to `sgm_uniform` in practice; the most robust across samplers here. | Low step counts, and as a safe default. |
| `sgm_uniform` | Uniform variance treatment, from score-based modelling. | Consistency-style and turbo models; low step counts. |
| `exponential` | Front-loads high-noise removal. | Composition over fine detail; shifts colour noticeably at low steps. |
| `ddim_uniform` | DDIM's original spacing. | Pairs with `ddim`. Degrades to static at very low step counts; holds up at normal ones. |
| `beta` | Beta-distribution curve shape. | Usable at normal step counts, unreliable at very low ones. |
| `linear_quadratic` | Quadratic ramp. | The sharpest tool in the set: see the failures section. |
| `kl_optimal` | KL-optimal spacing. | Usable at normal step counts; often paired with `uni_pc`. |

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

## Eliciting a specific effect

Recipes, in the order a requester is likely to want them. Each names the axis that actually controls the
effect rather than the one that is conventionally fiddled with.

| Goal | Recipe | Why this axis |
| --- | --- | --- |
| The same image again | Any deterministic solver, fixed seed, `eta` unset or 0 | The ODE has one solution for a given noise. Note the horde still cannot promise bit-identical output across workers, since they differ in GPU and driver. |
| Explore *near* an image you like | Keep the seed and prompt, raise `sampler_eta` from 0 toward 1 on an SDE solver | The only continuous variation dial in the stack. Seed changes everything at once; eta changes how far the walk strays. |
| Variation from a deterministic solver | `sampler_s_churn` above 0 on `k_euler`, `k_heun`, `k_dpm_2` | Churn is the only noise these solvers admit; without it they have no source of variation but the seed. |
| A different image entirely | Change the seed | Strongest single axis measured (0.13 similarity to the original). |
| Cheapest acceptable result | First-order solver, `simple` or `sgm_uniform`, 4-8 steps | One-evaluation solvers all cost about the same per step, and low-step behaviour is decided by the schedule, where these two are the robust pair. |
| Most fidelity per step, cost no object | `k_dpm_2` or `k_heun`, 12-20 steps | They settle soonest (0.75-0.77 by 24 steps) but cost roughly double per step, so they win only when the step saving beats the ratio. |
| A looser, painterly look | An ancestral solver (`k_euler_a`, `k_dpmpp_2s_a`) | They inject noise every step and never converge, so fine detail keeps reorganising instead of resolving. |
| Smooth gradients and clean surfaces | `dpmpp_2m_sde`, 20-30 steps, `karras` | Reported upstream for exactly this, and it is a first-order-cost solver despite the SDE. |
| Working with a distilled or turbo model | cfg 1.0, `sgm_uniform` or `simple`, 4-8 steps | Distilled models are trained against a uniform trajectory; non-uniform spacing creates truncation error in few-step regimes. |
| Prompt adherence without artefacts | Stay under cfg 13 | Above that, guidance itself produces oversaturation and edge artefacts regardless of solver, which is easily misattributed to the sampler. |

Two anti-recipes worth stating: do not reach for `karras` because the step count is low (it artefacts at 4
steps on most solvers), and do not reach for `linear_quadratic` on a non-flow model at all.

## Choosing

- **Default for quality**: `k_dpmpp_2m` with `karras`, 25-30 steps. Converges quickly and is stable
  across step counts.
- **Cheapest usable image**: a first-order solver (`k_euler`, `k_dpmpp_2m`, `ddim`, `deis`) with `simple`
  or `sgm_uniform`, at 4-8 steps. `uni_pc` and `uni_pc_bh2` are reported as strong at 10-15 steps and cost
  around 1.35x per step here.
- **Fewest steps regardless of cost**: a second-order solver (`k_dpm_2`, `k_heun`) or `heunpp2`. They
  settle soonest but cost 2.1x to 3.2x per step, so they only win if the step saving exceeds the ratio.
- **Deliberate variation between runs**: an ancestral or SDE sampler. These do not converge, which is
  the point.
- **Reproducibility**: a deterministic sampler. Note that the horde cannot promise bit-identical output
  across workers regardless of sampler, since workers differ in GPU and driver.
- **Schedule**: leave it alone unless you are working at low step counts, where `simple` and
  `sgm_uniform` were the most robust here. `karras` remains a good default at 20+ steps.
