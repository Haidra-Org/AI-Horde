---
title: "Service limits and behavioral constants"
summary: "Request lifetimes, worker leases, admission thresholds, payload bounds, pricing units and retention rules, with rationale and implementation references."
topics: [requests, workers, generation, kudos, moderation]
order: 5
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Service limits and behavioral constants

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [kudos](../topics.md#kudos), [moderation](../topics.md#moderation), [requests](../topics.md#requests), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

These values govern when requests expire, which workers can serve them, what clients can submit, and what is charged
or retained. Values are code defaults unless marked fixed or derived; a deployment setting or worker declaration can
further restrict service. Each section identifies the defining symbols and the paths that use them. For process
capacity, caches, background work and storage tuning, see [Operational constants](operational_constants.md).

An **unrecorded** rationale means the reviewed code and decision records do not explain the exact value. The
mechanism can still have a clear purpose. A limit accepted by validation does not guarantee an eligible worker,
successful generation or completion within a deadline. MiB and KiB denote binary byte units.

## Request lifetime and expiry

A request's expiry, a worker's job lease and a client's HTTP deadline are separate clocks.

| Property | Value and scope | Purpose and rationale | Definition and use |
| --- | --- | --- | --- |
| Ordinary request lifetime | Fixed 1,200 seconds (20 minutes) after creation or refresh | Bounds the lifetime of queued requests and their transient state. The exact 20-minute choice is unrecorded | `horde/utils.py`: `REQUEST_LIFETIME_SECONDS`, `get_expiry_date`; `horde/classes/base/waiting_prompt.py`: `WaitingPrompt.expiry`, `WaitingPrompt.refresh` |
| Extra-slow assignment lifetime | Fixed 3,600 seconds (1 hour) from refresh by an extra-slow worker | Allows slower hardware a longer completion window | `horde/utils.py`: `EXTRA_SLOW_REQUEST_LIFETIME_SECONDS`, `get_extra_slow_expiry_date`; `WaitingPrompt.refresh` |
| Expiry cleanup | 60-second background interval, plus execution and scheduling delay | Reclaims expired requests and stale jobs in batches. This is periodic enforcement, rather than an exact deletion time | `horde/database/__init__.py`: `start_background_threads`; `horde/database/threads.py`: `check_waiting_prompts`, `check_interrogations` |
| Request fault limit | Fixed threshold 2; repeated-fault termination triggers when the count exceeds it, on the third fault | Ends repeatedly failing requests. Exact threshold rationale is unrecorded; a stranded image request can fault earlier | `horde/classes/base/waiting_prompt.py`: `FAULTED_GENERATIONS_LIMIT`, `WaitingPrompt.mark_faulted_after_repeated_faults`, `WaitingPrompt.mark_faulted_when_stranded`; `horde/database/threads.py`: `check_waiting_prompts` |
| Interrogation form lease | Fixed 3 minutes from form pop | Recovers a form when its worker stops submitting. Exact duration rationale is unrecorded | `horde/utils.py`: `get_interrogation_form_expiry_date`; `horde/classes/stable/interrogation.py`: `InterrogationForms.pop`, `InterrogationForms.abort` |

`WaitingPrompt.refresh` extends an ordinary request to at least 20 minutes from the refresh; polling status is not
itself a refresh. Assignment can refresh expiry, and an extra-slow assignment sets the longer lifetime. Interrogations
also use `get_expiry_date`, with their own refresh and usage paths in `Interrogation`. Thus 20 minutes is not an
absolute creation-to-deletion guarantee for every request. Cleanup releases outstanding holds and deletes request
rows; a later status read can return not found. See [cancellation settlement](chat_requests.md#settlement-on-cancellation)
for the distinct treatment of work already in flight.

## Worker leases and eligibility

### Text and chat leases

| Property | Value and scope | Purpose and rationale | Definition and use |
| --- | --- | --- | --- |
| Base job lease | Fixed 150 seconds from pop, for text and chat | Allows preparation and generation while bounding exposure to a stalled worker | `horde/classes/kobold/worker.py`: `TEXT_JOB_LEASE_SECONDS`; `horde/classes/kobold/processing_generation.py`: `TextProcessingGeneration.set_job_ttl` |
| Preparation allowance | Fixed 30 seconds of that lease | Reserves time for reaching the assignment and processing the prompt before estimating output capacity | `horde/classes/kobold/worker.py`: `TEXT_JOB_PREPARATION_SECONDS`, `get_text_worker_lease_capacity` |
| Assumed unmeasured speed | Fixed 10 tokens/second | Lets a worker without samples receive ordinary jobs; implies capacity for 1,200 tokens in the remaining 120 seconds | `horde/classes/kobold/worker.py`: `UNMEASURED_TEXT_WORKER_TOKENS_PER_SECOND`, `get_effective_text_worker_speed` |
| Progress renewal caps | Fixed 600 seconds from pop for trusted owners; 200 otherwise | Renewals require newly posted output. The lower cap bounds exposure to workers without a trust record | `horde/classes/kobold/processing_generation.py`: `TRUSTED_TEXT_JOB_LEASE_CAP_SECONDS`, `UNTRUSTED_TEXT_JOB_LEASE_CAP_SECONDS`, `renew_text_job_ttl` |
| Online worker window | Fixed 300 seconds after check-in | Keeps a briefly restarting worker in the online pool. Exact window rationale is unrecorded | `horde/classes/base/worker.py`: `WORKER_ONLINE_SECONDS`; `horde/classes/kobold/worker.py`: `CHAT_WORKER_ONLINE_WINDOW`, `capable_chat_worker_clauses` |
| Normal-speed text threshold | 3 tokens/s for models with multiplier at least 20, 4 for at least 13, otherwise 5; 3 when the model list is empty | Applies when slow workers are disallowed. Exact cutoffs are unrecorded | `horde/classes/kobold/worker.py`: `get_minimum_text_worker_speed` |

Capacity is `floor(effective_speed * (150 - 30))`. Chat prefers workers whose capacity covers the requested output;
slower workers can take it when no capable worker is online. Legacy text is not gated by this capacity preference.
Progress extends a streaming chat job to 150 seconds after the new output, subject to its pop-relative cap. Repeated
posts do not renew, and lapsed leases cannot be revived. Cleanup enforces stale aborts periodically, so lease expiry
does not imply an immediate abort at that second. See the [job lease contract](chat_requests.md#job-lease),
[lease decision](../decisions/0038-lease-text-jobs-for-a-fixed-term-renewed-by-progress.md) and
[speed-gate decision](../decisions/0041-enforce-the-speed-gate-for-chat-requests-and-shadow-it-for-legacy-text.md).

### Image leases and sampler work

`horde/classes/stable/processing_generation.py`, `ImageProcessingGeneration.set_job_ttl`, computes a lease in this
order: start with `30 + 2 * W * P` seconds, where `W` is estimated sampler work and `P = width * height / 512^2`;
multiply by 2 for ControlNet; apply the selected model baseline's TTL multiplier; apply a 150-second minimum; add the
LoRA allowance; multiply by 3 for an extra-slow worker; round upward to whole seconds.

| Property | Value and scope | Purpose and rationale | Definition and use |
| --- | --- | --- | --- |
| Image fixed/proportional allowance | 30 seconds plus 2 seconds per 512-square work unit; minimum 150 seconds | Covers shallow prefetch, preparation and runtime variation. Multiplier order preserves the short-job allowance | `ImageProcessingGeneration.set_job_ttl`; [image lease decision](../decisions/0013-image-job-ttl-is-a-prefetch-lease.md) |
| LoRA download allowance | About 106.67 seconds per requested LoRA, before rounding the whole lease | Assumes an uncached maximum-size 400 MB file at 30 Mbps; added after the minimum so short jobs keep the allowance | `horde/classes/stable/processing_generation.py`: `LORA_MAX_SIZE_MB`, `LORA_ASSUMED_DOWNLOAD_MBPS`, `LORA_DOWNLOAD_SECONDS` |
| Adaptive sampler estimate | Fixed 40 first-order-equivalent work units for `k_dpm_adaptive` | A stable policy estimate before execution. It does not prove a maximum workload | `horde/sampler_work_policy.py`: `AI_HORDE_SAMPLER_WORK_ESTIMATION_POLICY`; [work units](samplers_and_schedulers.md) |
| Normal-speed image threshold | Fixed 500,000 pixel-work units/second (0.5 megapixel-work units/s) | Separates ordinary and slow workers; exact threshold rationale is unrecorded | `horde/classes/stable/worker.py`: `MINIMUM_IMAGE_WORKER_SPEED`; `horde/database/functions.py`: `get_sorted_wp_filtered_to_worker` |

Model-specific multipliers and resolution floors come from the baseline catalog. They can evolve independently of
these fixed allowances; use the [baseline policy](baseline_policy.md) and published sampler constraints for the
effective policy. An adaptive workload without a proven ceiling fails worker maximum-work protection even though it
has a finite time-budget estimate.

## Admission and charging

### Free output, input and image work {#free-output-and-image-work}

Six text settings in `horde/config.py`, `Config`, size the free output and input allowances used by
`horde/classes/kobold/waiting_prompt.py`, `TextWaitingPrompt`. Both allowances share the two wait thresholds.

| Environment name | Default | Purpose and rationale |
| --- | --- | --- |
| `HORDE_TEXT_FREE_OUTPUT_CEILING` | 4,096 tokens | An idle fleet can serve instruction and reasoning replies longer than the former 512-token allowance |
| `HORDE_TEXT_FREE_OUTPUT_FLOOR` | 512 tokens | Keeps the default chat output length within the allowance at any load |
| `HORDE_TEXT_FREE_INPUT_CEILING` | 16,384 tokens | Matches the default context of current koboldcpp releases, so an idle fleet serves a default client's full context free |
| `HORDE_TEXT_FREE_INPUT_FLOOR` | 4,096 tokens | Processing a long prompt occupies a worker before it generates anything, so a backed-up queue serves only short prompts free |
| `HORDE_TEXT_FREE_OUTPUT_WAIT_FULL_SECONDS` | 60 seconds | A queue clearing within a minute keeps both full allowances |
| `HORDE_TEXT_FREE_OUTPUT_WAIT_FLOOR_SECONDS` | 300 seconds | At five minutes of estimated backlog, longer free requests would prolong later waits |

Between the wait thresholds each allowance falls linearly. A request without an eligible worker gets the floors.
Some unfunded requests with no online service receive `NoEligibleWorker` instead of a misleading kudos refusal. A
request disallowing slow workers needs an upfront hold regardless of the allowances. The input allowance applies to
the prompt tokens processed by a worker, and a context smaller than the prompt bounds that count. Native text can opt
into a downgrade, which lowers the context for an input overage only under `context_fit: ignore`. Chat does not
downgrade to the free allowances. The allowances use work ahead of the request, and status estimates also account for
the request's own output. See
[free output and input allowances](chat_requests.md#free-output-allowance),
[ADR 0037](../decisions/0037-size-the-free-output-allowance-by-the-estimated-wait.md) and
[ADR 0043](../decisions/0043-bound-free-input-by-a-wait-sized-allowance.md).

Image admission uses inline values in `horde/classes/stable/waiting_prompt.py`,
`ImageWaitingPrompt.require_upfront_kudos`: the free resolution starts at
`1024 + 10 * total_threads - round(0.9 * queued_requests)`, is bounded to 576 through 1,024 pixels per side with
baseline-specific floors, and is tested as an area budget. LCM above 10 estimated work units, ControlNet above 20,
or work above a model's `max_steps` requirement (fallback 40) requires upfront kudos. These are operational work
budgets, separate from trajectory-quality limits. The exact numerical tuning is unrecorded. Disallowing slow workers
also requires upfront kudos. The optional `HORDE_UPFRONT_KUDOS_ON_WORKERLIST=1` rule applies to targeting in both
request families.

### Accounting units, floors and surcharges

| Property | Value and scope | Purpose and rationale | Definition and use |
| --- | --- | --- | --- |
| Prompt sizing estimate | 3 characters/token, rounded upward | Conservative tokenizer-free context fitting; does not measure the worker's actual tokens | `horde/classes/kobold/request_fit.py`: `CHARS_PER_TOKEN_ESTIMATE`, `estimate_prompt_tokens`; `horde/oai/mapping.py`: `CHARACTERS_PER_ESTIMATED_TOKEN` |
| Output charge estimate | `ceil(characters / 4)`, floor 20, capped at requested output | Pays for returned output using a server-observable estimate, including tool names/arguments and reasoning; floor pays for prompt processing | `horde/classes/kobold/processing_generation.py`: `TextProcessingGeneration.get_things_count`; [pricing](chat_requests.md#pricing) |
| Activation tax | 1 kudo, plus 5 per extra source image, plus applicable adjustments | Charges request activation and extra image resources. Exact amounts are unrecorded | `horde/classes/base/waiting_prompt.py`: `EXTRA_SOURCE_IMAGE_HORDE_TAX`, `WaitingPrompt._activate`, `WaitingPrompt.extrapolate_dry_run_kudos` |
| Style surcharge | 2 kudos when using another author's style | Credits the author from the surcharge actually collected; own styles have no surcharge or author credit | `horde/apis/v2/base.py`: `GenerateTemplate.decide_style_surcharge`; [style pricing](style_contract.md) |
| Image asset charges | 3 kudos per LoRA; 1 for a request using textual inversions | LoRA charge compensates download time; exact TI amount rationale is unrecorded | `horde/classes/stable/waiting_prompt.py`: `ImageWaitingPrompt.calculate_kudos` |
| Account floors | -50 anonymous, 14 pseudonymous, 25 otherwise | Floors affect debit forgiveness and spendable availability; exact amounts are unrecorded | `horde/classes/base/user.py`: `User.get_min_kudos`; `horde/database/kudos_reservations.py`: `available_kudos` |
| Annotation work classes | 3, 5 or 8 kudos per tile; unknown detector uses 5 | Groups weightless, weighted and hub detectors by recorded runtime cost | `horde/consts.py`: `ANNOTATION_KUDOS_BUCKET_WEIGHTLESS`, `ANNOTATION_KUDOS_BUCKET_WEIGHTED`, `ANNOTATION_KUDOS_BUCKET_HUB`, `annotation_detector_kudos_bucket`; `horde/classes/stable/interrogation.py`: `Interrogation.set_forms` |

Reported token counts do not change settlement. The 3-character sizing estimate and 4-character charging estimate
serve different purposes and must not be substituted for each other. See [accounting invariants](kudos_accounting.md)
for holds, projection, forgiveness and compensation-only repair.

### Account capacity and worker rewards

| Property | Value and scope | Definition, consumers and rationale |
| --- | --- | --- |
| Account concurrency | Stored default 30; account-specific value is returned | `horde/classes/base/user.py`: `User.concurrency`, `User.get_concurrency`; `horde/apis/v2/base.py`: admission. Exact default rationale is unrecorded; the model-dependent alternative after the unconditional return is inactive |
| Shared-key count | 3 ordinarily, 10 for trusted users, 1,000 for education accounts | `User.max_sharedkeys`: bounds keys per account; exact tiers unrecorded |
| Same-address worker restriction | Threshold 3 ordinarily, 20 for trusted users; invited-worker count can raise it | `User.SAME_IP_WORKER_THRESHOLD`, `User.SAME_IP_TRUSTED_WORKER_THRESHOLD`, `User.exceeding_ipaddr_restrictions`; predicate uses strictly greater than both the threshold and invited count. Restricts scripted worker spam |
| Uptime reward interval | More than 600 accumulated eligible uptime seconds since the prior reward | `horde/classes/base/worker.py`: `WorkerTemplate.uptime_reward_threshold`, `WorkerTemplate.check_in`. Stale, paused and maintenance periods do not accrue eligible uptime |
| Uptime reward amount | Base worker 100 kudos; interrogation worker 40; image worker `50 + 2 * model_count`, plus 30 when LoRAs are allowed | `WorkerTemplate.calculate_uptime_reward`; `horde/classes/stable/interrogation_worker.py`: `InterrogationWorker.calculate_uptime_reward`; `horde/classes/stable/worker.py`: `ImageWorker.calculate_uptime_reward`. Exact amounts unrecorded; interrogation uptime bypasses evaluation so infrequently assigned operators can earn spendable kudos |
| Text uptime reward | Base `25 + 15 * min(context, 262144) / 1024`; known-model factor `max(0.25, model_multiplier / 7)` and unvalidated known-backend factor 0.3; unknown-model factor 0.5 | `horde/classes/kobold/worker.py`: `TextWorker.calculate_uptime_reward`. Rewards context and model size, with 7B as the recorded baseline; exact coefficients unrecorded |
| Image bridge reward factors | Current official bridge 1; older official bridge 0.9; other bridge 0.75 | `horde/classes/stable/worker.py`: `ImageWorker.get_bridge_kudos_multiplier`; incentivizes current validated worker software |

Uptime rewards follow the accounting trust/evaluation path unless a worker type explicitly bypasses it. They are
separate from generation rewards and from [automatic trust promotion](#trust-moderation-and-retained-history).

## Payload limits and defaults

### Text and chat

`horde/classes/kobold/request_fit.py` defines `DEFAULT_MAX_LENGTH=80`, `DEFAULT_MAX_CONTEXT_LENGTH=2048`,
`MAX_LENGTH_LIMIT=16384` and `MAX_CONTEXT_LENGTH_LIMIT=1048576`. These bound native text output and context growth.
Chat instead defaults to 512 output tokens (`horde/chat_contract.py`, `DEFAULT_CHAT_MAX_TOKENS`), because 80 is too
short for a chat reply. Front token limits range from 16 through 16,384, and `n` must be 1 (`horde/oai/schema.py`,
`ChatCompletionRequest`). The exact lower bound and context ceiling rationale are unrecorded.

The following fixed bounds are defined in `horde/chat_contract.py` and used by request validation and
`horde/classes/kobold/chat_request.py`. SDK-owned bounds come from the locked dependency's
`horde_sdk.generation_parameters.text.chat_limits` module and are enforced by the shared limit checks.

| Property | Value | Symbols and rationale |
| --- | --- | --- |
| Message/model counts | 1,024 messages; 5 native chat model candidates | `MAX_CHAT_MESSAGE_COUNT`, `MAX_CHAT_MODEL_COUNT`: bound flattening and per-model checks |
| Message text | 1 MiB of UTF-8 text | SDK `MAX_CHAT_TEXT_BYTES`: bounds validation and stored payload work |
| Tool definitions and specifications | 128 tools; 256 KiB for tools, tool choice and response format together | SDK `MAX_CHAT_TOOL_COUNT`, `MAX_CHAT_TOOL_SPEC_BYTES`: bounds validation and schema work |
| Schema depth | 10 levels below the root | SDK `MAX_CHAT_SCHEMA_NESTING`: bounds recursive schema processing |
| Tool calls and names | 128 calls; function names 64 characters; call IDs 256 characters | `MAX_CHAT_TOOL_CALL_COUNT`, `MAX_CHAT_TOOL_NAME_LENGTH`, `MAX_CHAT_TOOL_CALL_ID_LENGTH`: bounded protocol objects; exact lengths unrecorded |
| Sampling defaults | Temperature/top-p 1; frequency/presence penalties 0; repetition penalty 1; top-k/min-p 0 | SDK `CHAT_SAMPLING_DEFAULTS`, applied by `build_chat_job_payload`: OpenAI defaults plus disabled KoboldAI filters |
| Omitted reasoning effort | `medium`, only for a declaring worker | `DEFAULT_CHAT_REASONING_EFFORT`, applied by `apply_worker_chat_defaults`: avoids an unlimited thinking budget consuming a small output limit; no answer guarantee |

The front can configure stricter body, message and text caps through `FrontSettings`. Worker declarations further
bound context and output. At submission, chat lowers an output limit beyond the online declarations to their largest
declared value and reports the change. See [native chat contracts](chat_requests.md) and [front screening](chat_front.md).

### Images and styles

| Property | Value and scope | Definition, consumers and rationale |
| --- | --- | --- |
| Image-generation dimensions and batch | 64 through 3,072 per side, default 512; `n` 1 through 20; steps 1 through 500, default 30 | `horde/apis/models/stable_v2.py`: `ImageModels` request parameter models. Workers and baseline/sampler policy can narrow acceptance. Exact broad schema bounds are unrecorded |
| Source-image pixels | 3,072 squared | `horde/image.py`: `MAX_SOURCE_IMAGE_PIXELS`; conversion paths bound decoding/re-encoding cost |
| Chat image count and bytes | 20 images; 5 MiB per image; 25 MiB total for trusted users/patrons, 5 MiB for others | `horde/chat_contract.py`: `MAX_CHAT_IMAGE_COUNT`, `MAX_CHAT_IMAGE_BYTES`, `MAX_CHAT_IMAGE_TOTAL_BYTES`, `MAX_CHAT_IMAGE_TOTAL_BYTES_UNTRUSTED`; validation bounds per-image work and transfer, both before and after JPEG conversion |
| Chat body bytes | 36 MiB | `MAX_CHAT_BODY_BYTES`: leaves room for base64 expansion of 25 MiB of images, 1 MiB of text and JSON overhead |
| Stored chat image | Longest side 2,048; JPEG quality 90; white background | `horde/image.py`: `MAX_CHAT_IMAGE_SIDE`, `CHAT_IMAGE_JPEG_QUALITY`, `CHAT_IMAGE_BACKGROUND`; matches the vision loader's resolution and JPEG support while preserving small text |
| Image context allowance | 512 tokens/image | `horde/chat_contract.py`: `CHAT_IMAGE_PROMPT_TOKENS`; `estimate_chat_prompt_tokens` in `horde/classes/kobold/chat_request.py` matches the worker's overflow allowance |
| Image download URL lifetime | At least 600 seconds, or job TTL if longer | `horde/classes/kobold/waiting_prompt.py`: `MIN_CHAT_IMAGE_URL_SECONDS`, `TextWaitingPrompt`; allows the worker's local queue wait |
| Offloaded messages URL lifetime | Fixed 120 seconds | Same file: `CHAT_MESSAGES_URL_SECONDS`; intended for immediate fetch after pop, with a shorter bearer-URL exposure |
| Messages offload threshold | 256 KiB compact JSON when enabled; offload and gzip both disabled by default | `horde/config.py`: `Config.chat_messages_offload_bytes`, `chat_messages_offload_enabled`, `chat_messages_offload_gzip`; `build_chat_messages_offload` in `horde/classes/kobold/chat_request.py` moves large payloads out of pop responses |
| Style template fields | 16 fields, names at most 32 characters, descriptions 1 through 200 characters | `horde/classes/base/style_contract.py`: `MAX_TEMPLATE_FIELDS`, `TEMPLATE_FIELD_NAME_MAX_LENGTH`, `TEMPLATE_FIELD_DESCRIPTION_MIN_LENGTH`, `TEMPLATE_FIELD_DESCRIPTION_MAX_LENGTH`; bounded declarations, exact counts unrecorded |
| Supplied template text | 7,500 words per value and across all values together | `horde/classes/base/style_application.py`: `MAX_TEMPLATE_FIELD_WORDS`, `resolve_template_field_values`; bounds request-supplied template values, exact choice unrecorded |

Sampler-specific ranges and baseline multipliers have their authoritative homes in the published
[sampler contract](samplers_and_schedulers.md), [baseline policy](baseline_policy.md) and
[style contract](style_contract.md). Their registries carry combinations that cannot be expressed by a single cap.

## HTTP limits, streaming and delivery

### Chat-front deadlines and admission

All front settings here are configurable in `horde/oai/settings.py`, `FrontSettings`, using `HORDE_OAI_` plus the
uppercased field name. `horde/oai/app.py` and `horde/oai/waiter.py` enforce them.

| Field | Default | Behavior and rationale |
| --- | --- | --- |
| `nonstream_timeout_seconds`, `stream_timeout_seconds` | 300 s, 600 s | Total response budgets beginning before submission. Streaming allows more time because keepalives hold the connection open; both bound a delivery slot's lifetime |
| `body_read_timeout_seconds` | 30 s | A slow sender cannot hold the connection indefinitely |
| `final_status_timeout_seconds` | 2 s | Allows a final status read for an outcome reached near the deadline |
| `fail_fast_grace_seconds` | 20 s | Tolerates temporary lack of eligible workers during restart or model loading; declaration gaps have separate refusal rules |
| `max_inflight`, `max_inflight_per_source` | 1,000; 5 | Per-process open-request caps, returning 503. The second prevents one address filling the process; users behind one egress share it |
| `heartbeat_seconds` | 5 s | SSE keepalive interval, below typical listener inactivity timeouts |
| `submit_recovery_attempts` | 2 | Bounded recovery of ambiguous submits with the same operation key, within the response budget |
| `delta_pacing_max_window_seconds`, `delta_pacing_max_chunks_per_second` | 2 s; 20 chunks/s | Smooths worker progress posts into readable text with bounded additional latency; zero window disables pacing |

These HTTP budgets can end the client's wait before the native request's renewable lifetime expires. Cancellation is
attempted at disconnect or timeout; a failed cancellation can leave chargeable work running. Retry hints do not make
a fresh client request idempotent. See [front cancellation controls](operational_constants.md#front-connections-and-cancellation)
and the [front error contract](chat_front.md#errors-the-front-returns).

### Rate limits and retained progress

| Property | Value and scope | Definition, use and rationale |
| --- | --- | --- |
| Ordinary submit limits | 2/second and 90/minute per address on routes using these limiters | `horde/apis/limiter_api.py`: `REQUEST_2SEC_LIMIT_PER_IP`, `REQUEST_90MIN_LIMIT_PER_IP`; `horde/apis/v2/kobold.py`, `horde/apis/v2/stable.py`: submit decorators. Exact rates unrecorded |
| Whitelisted submit limits | 10/second and 300/minute | `get_request_2sec_limit_per_ip`, `get_request_90min_limit_per_ip`: higher service-address limits. Chat has separate configurable `HORDE_CHAT_REQUEST_2SEC_LIMIT_PER_IP` and `HORDE_CHAT_REQUEST_90MIN_LIMIT_PER_IP`, falling back to these values |
| Native text/chat status | 60/minute per address, method and concrete path | `horde/apis/v2/kobold.py`: status decorators; `horde/apis/limiter_api.py`: `get_request_path`. Bounds polling; exact rate unrecorded |
| Progress size | 1,000 accepted entries/generation; 16 bytes per requested output token | `horde/chat_progress.py`: `MAX_CHAT_PROGRESS_ENTRIES`, `CHAT_PROGRESS_BYTES_PER_TOKEN`; `horde/chat_progress.lua`: atomic enforcement. Bounds Redis memory |
| Retained request stream | 900 seconds after last append; approximate trim to 4,000 entries | `CHAT_PROGRESS_STREAM_TTL_SECONDS`, `CHAT_PROGRESS_STREAM_MAX_ENTRIES`: permits catching up after a stalled reader while expiring idle streams |
| Progress lease grace | 90 seconds beyond job TTL | `CHAT_PROGRESS_LEASE_GRACE_SECONDS`: keeps Redis authentication/state briefly beyond the assignment's lease; does not extend the job deadline |
| Webhook attempts | 3; HTTPS only; 3 s connect/low-speed timeout, 6 s total attempt deadline, 64 KiB response cap | `horde/classes/base/processing_generation.py`: `WEBHOOK_ATTEMPTS`, `WEBHOOK_SCHEMES`, `WEBHOOK_HOP_TIMEOUT_SECONDS`, `WEBHOOK_ATTEMPT_DEADLINE_SECONDS`, `WEBHOOK_MAX_RESPONSE_BYTES`; bounds subscriber cost. Delivery remains best effort |
| Image URL fetch | 5 MiB, 3 redirects, 2 s connect/low-speed timeout, 10 s total across redirects | `horde/image.py`: `MAX_FETCHED_IMAGE_BYTES`, `MAX_IMAGE_FETCH_REDIRECTS`, `IMAGE_FETCH_TIMEOUT_SECONDS`, `IMAGE_FETCH_DEADLINE_SECONDS`, `fetch_public_image`; bounds untrusted URL work. Synchronous DNS can exceed the deadline |

## Trust, moderation and retained history

| Property | Value and scope | Definition, use and rationale |
| --- | --- | --- |
| Automatic trust promotion | Account at least 7 days old, escrow strictly above configured `KUDOS_TRUST_THRESHOLD`; unset disables promotion | `horde/classes/base/user.py`: `User.PROMOTION_MIN_ACCOUNT_AGE`, `User.promotion_status`; `horde/classes/base/kudos.py`: `get_kudos_trust_threshold`. The age gives moderators time to notice new accounts; suspicious and anonymous accounts are excluded |
| Suspicious account threshold | 5 recorded suspicions; trusted users exempt | `User.SUSPICION_THRESHOLD`, `User.is_suspicious`; affects eligibility and moderation-retention exemption. Exact count unrecorded |
| Worker abort maintenance | More than 20 aborts in the counting window; threshold 10 in raid mode, 100 for interrogation workers and a legacy owner exception | `horde/classes/base/worker.py`: `WorkerTemplate.log_aborted_job`. The counter resets on an abort more than 1 hour after its window timestamp, and after threshold action. Raid mode suppresses maintenance; nonconformant-reply faults use separate accounting |
| Model-rejection timeout trigger | More than the configured threshold (default 5) in a 1-hour IP-subject window; default action starts on the sixth rejection | `horde/database/prompt_moderation.py`: `DEFAULT_MODEL_REJECTION_TIMEOUT_THRESHOLD`; `horde/countermeasures.py`: `REJECTION_WINDOW`, `CounterMeasures.count_model_rejection`; `horde/apis/v2/base.py`: `GenerateTemplate._handle_prompt_rejection`. Bounds rejection floods; raid mode acts on each rejection, moderators exempt |
| Moderation IP subject | IPv4 address or IPv6 /64 | `horde/countermeasures.py`: `IPV6_SUBJECT_PREFIX_LENGTH`, `CounterMeasures.ip_subject`; rotating IPv6 addresses remain one subject |
| Evidence text | 16,000 characters per prompt stage; notes at most 2,000 characters | `horde/database/prompt_moderation.py`: `MAX_EVIDENCE_CHARACTERS`; `horde/classes/base/prompt_moderation.py`: `MAX_NOTE_CHARACTERS`. Bounds capture and review objects; evidence clipping does not change the generation prompt |
| Evidence address/text retention | Address default 30 days; text default `none` until the ceiling | `horde/database/prompt_moderation.py`: `DEFAULT_IPADDR_RETENTION_DAYS`, `DEFAULT_TEXT_RETENTION_DAYS`, `RetentionPolicy`; configurable `HORDE_MODERATION_*_RETENTION_DAYS`. Recent addresses aid review; unreviewed text stays available through the evidence period |
| Evidence anonymization ceiling | Default and maximum 365 days; cannot be disabled | `DEFAULT_EVIDENCE_CEILING_DAYS`, `MAX_EVIDENCE_CEILING_DAYS`; events under moderation action are exempt from every retention step while the condition holds |
| Evidence read URL | Fixed 600 seconds | `horde/r2.py`: `EVIDENCE_TEXT_URL_SECONDS`, `evidence_text_url`; bounds bearer-link access, with locally generated signatures |
| Exact accounting history | At least 30 days online per dataset; default 30 | `horde/database/kudos_retention.py`: `DEFAULT_WINDOW_DAYS`, `MINIMUM_WINDOW_DAYS`, `KudosRetentionPolicy`; preserves a directly queryable incident-investigation window |
| Transfer/repair key lifetime | Shorter of the two configured accounting windows | `KudosRetentionPolicy.key_lifetime`; retries after expiry create a new transfer, and repair refuses an older snapshot. Prevents retired history from being needed for idempotency |

Moderation action can retain evidence indefinitely; account wipes do not erase it. Accounting currency is archived
without a fixed end date, while statistics become daily rollups. These are separate policies. See
[moderation retention](moderation_operations.md#retention), [accounting retention](kudos_accounting.md#event-retention)
and [retention execution controls](operational_constants.md#accounting-retention-and-archives).

## Verification map

These existing suites cover the mechanisms and boundaries. Their presence does not establish a historical reason
for an otherwise unrecorded number.

| Contract | Tests |
| --- | --- |
| Cancellation, expiry cleanup and fault races | `tests/integration/test_generation_lifecycle_races.py`, `tests/integration/test_maintenance_threads.py` |
| Text lease, renewal caps and capacity preference | `tests/unit/test_text_job_ttl.py`, `tests/unit/test_text_worker_eligibility.py` |
| Image lease formula and work estimates | `tests/unit/test_trajectory_and_cost_steps.py`, `tests/unit/test_sampler_work_policy.py` |
| Free allowance and text settlement | `tests/unit/test_kobold_text_wp.py`, `tests/unit/test_kudos_settlement.py`, `tests/integration/test_chat.py` |
| Payload, image and tool bounds | `tests/unit/test_chat_request.py`, `tests/unit/oai/test_request_screening.py`, `tests/integration/test_chat_images.py` |
| Style declarations and pricing | `tests/unit/test_style_contract.py`, `tests/unit/test_style_application.py`, `tests/integration/test_text_style_application.py` |
| Front budgets, limits and recovery | `tests/unit/oai/test_settings.py`, `tests/unit/oai/test_lifecycle.py`, `tests/unit/oai/test_submit_recovery.py`, `tests/unit/test_chat_rate_limit.py` |
| Stream caps and webhook delivery | `tests/unit/test_chat_progress.py`, `tests/integration/test_chat_progress.py`, `tests/unit/test_generation_webhook.py` |
| Image URL bounds | `tests/unit/test_image_url_fetch.py`, `tests/unit/test_outbound.py` |
| Trust promotion and moderation | `tests/unit/test_kudos_balance.py`, `tests/unit/test_moderation_retention_policy.py`, `tests/integration/test_rejection_countermeasures.py`, `tests/integration/test_prompt_moderation.py` |
| Accounting windows and key expiry | `tests/unit/test_kudos_retention.py`, `tests/integration/test_kudos_retention_end_to_end.py` |
