---
title: "Add extended image controls to a frontend"
summary: "Integrate sampler discovery, schedules, solver controls, expanded ControlNet types, and control-map annotations into an existing image frontend."
topics: [generation]
order: 20
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Add extended image controls to a frontend

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation)
<!-- END GENERATED: topics -->

This procedure assumes the frontend already submits image requests, polls request status, has current
model-reference metadata, and generates API types from `/api/swagger.json` or maintains equivalent
request types.

The [samplers and schedulers reference](../reference/samplers_and_schedulers.md) covers behavior and
cost. This guide concentrates on request construction and UI state.

The Haidra [AiHordeFrontpage](https://github.com/Haidra-Org/AiHordeFrontpage) is a reference
implementation of this flow. Its generation request builder performs the final sampler filtering, and
its persistent Alchemy queue captures annotation images before their result links expire. The examples
below remain framework-neutral so other frontends can adopt the same boundaries.

The integration has four sources of truth: `/api/swagger.json` supplies field and enum types, the
sampler-constraints endpoint supplies sampler-specific rules, model-reference records supply the
selected models' baselines, and the server validates the assembled request. Keeping those jobs
separate avoids baking a second compatibility matrix into the frontend.

## Add sampler discovery

Fetch the anonymous contract once per API origin during application startup:

```http
GET <horde-base-url>/api/v2/status/sampler_constraints
Client-Agent: <frontend-name>:<version>:<project-url>
```

Version 1.0 has this shape. The values shown are just a representative subset, so get the response
rather than copying the following:

```json
{
  "schema_version": "1.0",
  "samplers": {
    "dpmpp_2m_sde": {
      "name": "dpmpp_2m_sde",
      "presentation_tier": "recommended",
      "solver_type_choices": ["midpoint", "heun"],
      "accepted_settings": {
        "sampler_eta": {
          "minimum": 0.0,
          "maximum": 100.0,
          "default": 1.0,
          "integer_only": false
        },
        "sampler_s_noise": {
          "minimum": 0.0,
          "maximum": 100.0,
          "default": 1.0,
          "integer_only": false
        }
      }
    }
  },
  "hard_constraints": {
    "rejected_sampler_scheduler_pairings": [
      {"sampler": "dpmpp_3m_sde", "scheduler": "normal"}
    ],
    "scheduler_baseline_applicability": {
      "align_your_steps": ["stable_diffusion_1", "stable_diffusion_xl"],
      "gits": ["stable_diffusion_1", "stable_diffusion_xl"]
    }
  },
  "presentation_tiers": {
    "recommended": ["DDIM", "dpmpp_2m_sde", "k_dpmpp_2m"]
  }
}
```

Keep the generated API model when one is available. A hand-written adapter only needs the fields used
by the UI:

```ts
type KnobRange = {
  minimum: number;
  maximum: number | null;
  default: number | null;
  integer_only: boolean;
};

type SamplerRecord = {
  presentation_tier: "recommended" | "advanced";
  solver_type_choices: string[];
  accepted_settings: Record<string, KnobRange>;
};

type SamplerContractV1 = {
  schema_version: string;
  samplers: Record<string, SamplerRecord>;
  hard_constraints: {
    rejected_sampler_scheduler_pairings: Array<{
      sampler: string;
      scheduler: string;
    }>;
    scheduler_baseline_applicability: Record<string, string[]>;
  };
  presentation_tiers: {recommended: string[]};
};
```

Accept a document only when its major schema version is supported. Cache each accepted response by API
origin and full `schema_version`; the origin matters because self-hosted deployments can differ.

```ts
const supportedSamplerContractMajor = "1";

function samplerContractCacheKey(apiOrigin: string, suffix: string): string {
  return `aihorde:sampler-constraints:${apiOrigin}:${suffix}`;
}

function readCachedSamplerContract(apiOrigin: string): SamplerContractV1 | null {
  const cached = localStorage.getItem(
    samplerContractCacheKey(apiOrigin, `latest-v${supportedSamplerContractMajor}`),
  );
  if (!cached) return null;

  try {
    const document = JSON.parse(cached) as SamplerContractV1;
    return document.schema_version.split(".")[0] === supportedSamplerContractMajor
      ? document
      : null;
  } catch {
    return null;
  }
}

async function loadSamplerContract(apiOrigin: string): Promise<SamplerContractV1 | null> {
  try {
    const response = await fetch(`${apiOrigin}/api/v2/status/sampler_constraints`, {
      headers: {"Client-Agent": CLIENT_AGENT},
    });
    if (!response.ok) return readCachedSamplerContract(apiOrigin);

    const document = (await response.json()) as SamplerContractV1;
    if (document.schema_version.split(".")[0] !== supportedSamplerContractMajor) {
      return readCachedSamplerContract(apiOrigin);
    }
    if (!document.samplers || !document.hard_constraints) {
      return readCachedSamplerContract(apiOrigin);
    }

    localStorage.setItem(
      samplerContractCacheKey(apiOrigin, document.schema_version),
      JSON.stringify(document),
    );
    localStorage.setItem(
      samplerContractCacheKey(apiOrigin, `latest-v${supportedSamplerContractMajor}`),
      JSON.stringify(document),
    );
    return document;
  } catch {
    return readCachedSamplerContract(apiOrigin);
  }
}
```

If no compatible cache exists, retain the frontend's existing sampler picker and omit `scheduler`,
`flow_shift`, and every `sampler_*` field.

Verify this stage in browser developer tools: the request succeeds without an `apikey`, the chosen
document has a supported `schema_version`, and the sampler picker contains the keys of `samplers`.

## Render controls from the selected sampler

Show `presentation_tiers.recommended` first and put records marked `advanced` behind the frontend's
advanced-controls affordance. A tier controls placement only. Every record in `samplers` is accepted.

Render one numeric input for each entry in `accepted_settings`:

- `minimum` and a non-null `maximum` become input bounds.
- `integer_only: true` uses an integer input or `step="1"`.
- `maximum: null` means that the setting has no upper bound.
- `default: null` means that leaving the field absent delegates the limit or value to the solver.
- `solver_type_choices` becomes a select only when the array is non-empty.

Use frontend labels as presentation data while retaining the API field as the form-state key:

```ts
const samplerSettingLabels: Record<string, string> = {
  sampler_eta: "Eta",
  sampler_s_noise: "Noise multiplier",
  sampler_s_churn: "Churn",
  sampler_s_tmin: "Churn start sigma",
  sampler_s_tmax: "Churn end sigma",
  sampler_order: "Solver order",
};
```

Do not serialize a displayed default until the user changes it. This lets the backend retain ownership
of defaults and gives Reset a simple meaning: delete the value from form state. Keep drafts per sampler
if restoring a user's previous tuning is useful, but filter the draft at submission time. A user can
select `k_euler`, set churn, and then select `dpmpp_2m_sde`; the hidden churn value must not survive that
change.

```ts
type SamplerDraft = Record<string, number | string | undefined>;

function serializeSamplerSettings(
  contract: SamplerContractV1,
  samplerName: string,
  draft: SamplerDraft,
): Record<string, number | string> {
  const sampler = contract.samplers[samplerName];
  if (!sampler) return {};

  const serialized: Record<string, number | string> = {};
  for (const [field, range] of Object.entries(sampler.accepted_settings)) {
    const value = draft[field];
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    if (value < range.minimum) continue;
    if (range.maximum !== null && value > range.maximum) continue;
    if (range.integer_only && !Number.isInteger(value)) continue;
    serialized[field] = value;
  }

  const solverType = draft.sampler_solver_type;
  if (typeof solverType === "string" && sampler.solver_type_choices.includes(solverType)) {
    serialized.sampler_solver_type = solverType;
  }
  return serialized;
}
```

Verify by changing between samplers with different `accepted_settings` and inspecting the outgoing
JSON. Its `sampler_*` keys must be a subset of the newly selected sampler's settings, plus
`sampler_solver_type` only when its value occurs in `solver_type_choices`. Hiding the advanced section
and returning `{}` from this serializer reverses the UI addition.

## Filter schedules and flow shift

Take the scheduler vocabulary from the generated generation-request schema. The sampler contract adds
the combination rules. A scheduler is unavailable when the selected sampler explicitly rejects it, or
when it has a baseline allowlist that does not contain every effective model baseline.

```ts
function availableSchedulers(
  allSchedulers: string[],
  contract: SamplerContractV1,
  sampler: string,
  effectiveBaselines: string[],
): string[] {
  const rejected = new Set(
    contract.hard_constraints.rejected_sampler_scheduler_pairings
      .filter((pair) => pair.sampler === sampler)
      .map((pair) => pair.scheduler),
  );

  return allSchedulers.filter((scheduler) => {
    if (rejected.has(scheduler)) return false;
    const allowedBaselines =
      contract.hard_constraints.scheduler_baseline_applicability[scheduler];
    return !allowedBaselines || (
      effectiveBaselines.length > 0 &&
      effectiveBaselines.every((baseline) => allowedBaselines.includes(baseline))
    );
  });
}
```

Derive `effectiveBaselines` from the existing model-reference records after applying any selected
style. A style can replace models or parameters. Treat an unknown or custom baseline as incompatible
with a baseline-restricted scheduler, and let the server remain the final authority.

The current flow-matching baselines are `flux_1`, `flux_dev`, `flux_schnell`, and `qwen_image`. Show
`flow_shift` only when every effective model has one of those baselines. Generate its numeric bounds
from the generation request schema (currently 0 through 100), and omit it when the control is hidden.
Keep this baseline set with the frontend's versioned model-feature data so an API update can change it
in one place.

An explicit `scheduler` takes precedence over the legacy `karras` boolean. Preserve old saved settings
as follows:

```ts
if (userExplicitlySelectedScheduler) {
  params.scheduler = selectedScheduler;
  delete params.karras;
} else {
  params.karras = existingKarrasValue;
}
```

Verify that `dpmpp_3m_sde` removes `normal`, and that `align_your_steps` and `gits` disappear when any
effective model baseline falls outside their published allowlists. Remove the scheduler and flow-shift
controls, omit both fields, and continue sending `karras` to reverse this stage.

## Submit a generation request

Merge only the serialized controls into the existing `params`. This complete example selects the
`heun` correction supported by `dpmpp_2m_sde`:

```json
{
  "prompt": "a glass greenhouse in winter",
  "models": ["<image-model-name>"],
  "params": {
    "width": 1024,
    "height": 1024,
    "steps": 24,
    "sampler_name": "dpmpp_2m_sde",
    "scheduler": "beta",
    "sampler_eta": 1.0,
    "sampler_s_noise": 1.0,
    "sampler_solver_type": "heun"
  }
}
```

```http
POST <horde-base-url>/api/v2/generate/async
apikey: <api-key>
Client-Agent: <frontend-name>:<version>:<project-url>
Content-Type: application/json
```

A successful response contains the existing request identifier:

```json
{"id": "<request-uuid>", "kudos": 12.34}
```

Continue polling `GET /api/v2/generate/status/<request-uuid>` and use the existing cancellation UI.
New schedules and solver controls can wait for a compatible worker, so a successful submission does
not imply immediate dispatch.

Verify that submission returns an `id` and that the final status reaches `done`. To reverse all sampler
additions, remove `scheduler`, `flow_shift`, and every `sampler_*` key while retaining the existing
`sampler_name` and `karras` behavior.

## Add expanded ControlNet choices

Generate the picker from the `control_type` enum on the image generation request in
`/api/swagger.json`. This includes classic values such as `canny`, `depth`, and `openpose`, plus newer
detectors such as `standard_lineart`, `depth_anything_v2`, and `oneformer_ade20k`.

The request rules are unchanged:

- Put `control_type`, `image_is_control`, and `return_control_map` in `params`.
- Put `source_image` at the request root as a public URL or base64-encoded image.
- Set `image_is_control: true` only when the source is already a prepared control map.
- Keep `image_is_control: false` when the worker must run the selected detector.
- Do not combine ControlNet with `source_processing: "inpainting"`.
- Retain the frontend's model-compatibility checks and handle server validation because model support
  can change independently of the frontend.

```json
{
  "prompt": "a city street drawn in ink",
  "models": ["<controlnet-compatible-model>"],
  "source_image": "<image-url-or-base64>",
  "source_processing": "img2img",
  "params": {
    "width": 512,
    "height": 512,
    "steps": 24,
    "sampler_name": "k_dpmpp_2m",
    "scheduler": "karras",
    "control_type": "standard_lineart",
    "image_is_control": false,
    "return_control_map": false
  }
}
```

Submit through `POST /api/v2/generate/async` and poll the ordinary generation status endpoint. Less
common detectors require newer workers and can wait longer in the queue. Verify that the async response
contains an `id` and the request completes. Restrict the picker to the previous enum subset to reverse
expanded choices; remove `control_type`, `image_is_control`, `return_control_map`, and `source_image` to
return to text-to-image.

## Add direct control-map generation

Use the interrogation API when the user wants the prepared control map itself. This path does not need
a generation prompt, sampler, or model. Generate its choices from the `control_type` enum on
`ModelInterrogationFormPayloadStable` in `/api/swagger.json`.

The two control-type enums are intentionally almost identical. Image generation retains the legacy
`hough` spelling; direct annotation uses the detector's `mlsd` name. Do not feed the generation enum
unchanged into the annotation form.

```json
{
  "forms": [
    {
      "name": "annotation",
      "payload": {"control_type": "canny"}
    }
  ],
  "source_image": "<public-image-url-or-base64>"
}
```

```http
POST <horde-base-url>/api/v2/interrogate/async
apikey: <api-key>
Client-Agent: <frontend-name>:<version>:<project-url>
Content-Type: application/json
```

Store the returned `id`, then poll the interrogation endpoint rather than the generation endpoint:

```http
GET <horde-base-url>/api/v2/interrogate/status/<request-uuid>
Client-Agent: <frontend-name>:<version>:<project-url>
```

The completed payload identifies the form independently and returns a temporary image URL:

```json
{
  "state": "done",
  "forms": [
    {
      "form": "annotation",
      "state": "done",
      "result": {"annotation": "<temporary-image-url>"},
      "payload": {"control_type": "canny"}
    }
  ]
}
```

Match the result by `form === "annotation"`; do not assume it is the first entry when the request also
contains caption or post-processing forms. Display or download `result.annotation` promptly because it
is a presigned result URL rather than permanent frontend storage.

One request may carry several `annotation` forms, one per `control_type`, and each comes back as its own
entry. A form's status entry echoes the `payload` it was requested with, so match a map to its detector
by `payload.control_type` rather than by position. Identical name and payload pairs are queued once. A
server older than this guide omits `payload`; when a request carried a single annotation form the
detector is the one you sent, so fall back to that rather than failing.

Cancel an unfinished request with:

```http
DELETE <horde-base-url>/api/v2/interrogate/status/<request-uuid>
apikey: <api-key>
Client-Agent: <frontend-name>:<version>:<project-url>
```

Verify the returned URL can be loaded as an image, not merely that the key exists. Removing
`annotation` from the form picker reverses this feature and leaves all existing interrogation forms
unchanged.

## Verify against a local stack

Point the frontend's local configuration at the API branch being tested. Include `/api/v2` exactly
once in the configured base URL. A local frontend that still points at `https://aihorde.net/api/v2`
can look correct while reading the production contract and submitting production work.

For this repository's Docker Compose stack, create the ignored `.env_docker` described in
`README_docker.md`, then start the API and its stores:

```console
docker compose up --build -d
curl http://localhost:7001/api/v2/status/sampler_constraints
```

```console
docker compose -p aihorde-extended-frontend-test up --build -d
```

You should change the production endpoint from `https://aihorde.net/api/v2` to
`http://localhost:7001/api/v2` in the frontend's local configuration.

## Recover from validation errors

API errors have a stable machine-readable `rc` and display text in `message`:

```json
{
  "message": "k_euler does not accept eta.",
  "rc": "SamplerKnobInapplicable"
}
```

Branch on `rc`; show `message` to the user without parsing it. On a sampler-related rejection, refresh
the sampler contract once, rebuild the affected controls, and require an explicit second submission.
An automatic retry can silently alter an image request and can loop when cached model metadata is also
stale.

| `rc` | State change before resubmission |
| --- | --- |
| `SamplerKnobInapplicable` | Delete the field and rebuild from `accepted_settings`. |
| `SamplerKnobOutOfRange` | Delete the value or reset it to the published default. |
| `SamplerSolverTypeUnsupported` | Delete the value and rebuild from `solver_type_choices`. |
| `SamplerSchedulerMismatch` | Clear the scheduler and apply the rejected-pairing rules. |
| `SchedulerBaselineMismatch` | Clear the scheduler and refresh effective model baselines. |
| `FlowShiftInapplicable` | Hide and delete `flow_shift` for the effective models. |
| `FlowShiftOutOfRange` | Delete it or reset it within the generated schema range. |
| `ControlNetSourceMissing` | Keep the form open and require a source image. |
| `ControlNetInpaintingMismatch` | Change `source_processing` or disable ControlNet. |
| `ControlNetMismatch` or `ControlNetUnsupported` | Disable ControlNet for the selected models. |

Verify each recovery with one manual resubmission that returns an `id`. If the second submission is
rejected, keep the error visible with its `rc` and preserve the user's other settings so they can make
the next choice.
