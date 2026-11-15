---
title: "Use the style endpoints"
summary: "Read the published style contract, create text and image styles that declare a policy and placeholders, and send requests under them."
topics: [generation, requests]
order: 50
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Use the style endpoints

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

Create styles that control which settings a request can override, then use a dry run to inspect the result before
generating. The text style also includes placeholders filled from the request.

You need an API key for a trusted account or an account with the customizer role, since style creation requires one
of those permissions. Run the commands against a Horde deployment with reachable `/api/v2` endpoints.

The rules for these procedures are in the [style contract reference](../reference/style_contract.md).
A frontend that builds authoring controls and request forms from the contract should follow [adding
styles to a frontend](add_styles_to_a_frontend.md). A client that integrated styles before the contract
should check [the update for existing integrations](update_a_style_integration.md). [ADR
17](../decisions/0017-protect-koboldcpp-placeholders-and-publish-the-style-contract.md) records why
the contract is published and why instruct placeholders in a text template are protected.

The examples use Bash, `curl` and `jq`. Replace angle-bracket placeholders before running commands.
Read the contract first, then choose the text or image example. Dry runs and policy updates are separate tasks.
Every command uses two shell variables:

```bash
HORDE=https://aihorde.net/api/v2
APIKEY="<your-api-key>"
```

## Read the style contract

For each style type, the contract lists the params that a policy may let the request set and the
params that it may cap. It also lists the placeholders filled by the horde in a prompt template, and
states how the other braces in the template are handled. Read it first and build the rest of the
procedure from its response, rather than from values pasted from another deployment.

```bash
curl -s "$HORDE/status/style_contract" | jq .
```

Verification: the body includes `schema_version`, `text` and `image`. `jq '.text.overridable_parameters'`
lists the params that a text policy may let the request set. `jq '.text.ceiling_parameters'` lists the
params that it may cap, with the allowed range for each ceiling. `jq -r '.text.protected_patterns[0].pattern'`
is the regular expression that a protected placeholder in a text template has to match.

This read-only endpoint does not require an API key and is exempt from the rate limiter.

## Create a text style with a policy and placeholders

This style fixes the sampling settings while allowing requests to choose output and context lengths within
specified limits. The `tone` and `subject` placeholders let each request customize the prompt without changing
the template.

```bash
curl -s -X POST "$HORDE/styles/text" \
  -H "apikey: $APIKEY" \
  -H "Client-Agent: <your-client>:<version>:<contact>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<style-name>",
    "info": "An instruct template with a tone and a subject field.",
    "prompt": "{{[INPUT]}}Write in a {tone} tone about {subject}.\n{p}{{[OUTPUT]}}",
    "params": {"temperature": 0.7, "max_length": 200, "max_context_length": 4096},
    "models": ["<model-name>"],
    "public": true,
    "nsfw": false,
    "parameter_policy": {
      "override": "listed",
      "overridable": ["max_length", "max_context_length"],
      "ceilings": {"max_length": 512, "max_context_length": 8192, "n": 4}
    },
    "template_fields": [
      {"name": "tone", "description": "How the answer should read.", "required": true},
      {"name": "subject", "description": "What the answer is about.", "required": false}
    ]
  }'
```

If creation returns an error, resolve its `rc` and message before using an ID. For 401/403, check the key and
account permissions; for 429, follow the write-limit guidance in [Traps](#traps).

Verification: the response includes the new style's `id`. Read it back and confirm both declarations
were stored, since both are null on a style that does not declare them:

```bash
curl -s "$HORDE/styles/text/<style-id>" | jq '{parameter_policy, template_fields, prompt}'
```

The `prompt` comes back with `{{[INPUT]}}` and `{{[OUTPUT]}}` exactly as sent.

Reversal: `curl -s -X DELETE "$HORDE/styles/text/<style-id>" -H "apikey: $APIKEY"`, which returns 200
with an `OK` message. Deleting a style is final. A deleted style cannot be restored.

## Create an image style with a policy

An image template's braces are all literal apart from `{p}`, `{np}` and its declared placeholders, so a
wildcard or workflow string in braces passes through untouched.

```bash
curl -s -X POST "$HORDE/styles/image" \
  -H "apikey: $APIKEY" \
  -H "Client-Agent: <your-client>:<version>:<contact>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "<style-name>",
    "info": "A fixed look with the size left to the request.",
    "prompt": "{p}, oil on canvas, {dramatic|soft} light ### {np}, blurry",
    "params": {"sampler_name": "k_euler_a", "steps": 30, "cfg_scale": 7.5, "width": 512, "height": 512},
    "models": ["<model-name>"],
    "public": true,
    "nsfw": false,
    "parameter_policy": {
      "override": "listed",
      "overridable": ["width", "height"],
      "ceilings": {"width": 1024, "height": 1024, "n": 4}
    }
  }'
```

Verification: read the style back and confirm the policy and the template:

```bash
curl -s "$HORDE/styles/image/<style-id>" | jq '{parameter_policy, template_fields, prompt}'
```

`template_fields` is null, since this style does not declare any. The `{dramatic|soft}` in the prompt is
stored and sent as written.

Reversal: `curl -s -X DELETE "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY"`.

## Dry run a request under the style and read `resolved`

Use the text style ID from [Create a text style](#create-a-text-style-with-a-policy-and-placeholders)
for this example. Keep image style IDs for image requests.

A dry run returns a price quote and the request after applying the style. Use it to check the completed prompt and
effective parameters before spending kudos. It does not queue generation work.

```bash
curl -s -X POST "$HORDE/generate/text/async" \
  -H "apikey: $APIKEY" \
  -H "Client-Agent: <your-client>:<version>:<contact>" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "<your prompt>",
    "style": "<style-id>",
    "dry_run": true,
    "params": {"max_length": 300, "n": 2},
    "template_fields": {"tone": "plain"}
  }' | jq .
```

Verification: the response is 200 with `kudos` and `resolved`. In `resolved`:

- `prompt` is the style's template filled in. `{{[INPUT]}}` and `{{[OUTPUT]}}` appear unchanged,
  `{tone}` has become `plain`, and `{subject}`, which was left out and is optional, has become the
  empty string.
- `params.max_length` is 300, taken from the request because the policy lists it.
- `params.temperature` is 0.7 from the style, because the policy does not list it.
- `params.n` is 2, which always comes from the request.
- `models` is the style's model list.
- `style` includes the applied style's `id` and `name`.
- `estimated_prompt_tokens` and `context_fit` report how the prompt was sized.

For the image alternative, use the image style ID, post to `$HORDE/generate/async`, omit `template_fields`,
and use image params such as `{"width": 768, "height": 512, "n": 2}`. Its `resolved` also includes
`negative_prompt`, the half of the prompt after `###`.

A dry run does not create a generation request or charge kudos. It does increment the style's use count.

## Size a text request against its prompt

The [context sizing and charging constants](../reference/service_limits.md#accounting-units-floors-and-surcharges)
explain why the prompt-fit estimate and output-settlement estimate use different character-to-token ratios.

Use the text style created earlier. Replace `<a long prompt>` with enough text to exceed its initial context;
a short literal placeholder does not exercise growth.

`context_fit` sets what happens when the prompt produced by the style does not fit inside
`max_context_length` alongside the tokens to be generated.

```bash
curl -s -X POST "$HORDE/generate/text/async" \
  -H "apikey: $APIKEY" \
  -H "Client-Agent: <your-client>:<version>:<contact>" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "<a long prompt>",
    "style": "<style-id>",
    "dry_run": true,
    "context_fit": "grow",
    "template_fields": {"tone": "plain", "subject": "harbour cranes"}
  }' | jq '{kudos, resolved}'
```

Verification: under `grow`, `resolved.params.max_context_length` is the next power of two that fits,
bounded by the style's ceiling and by the largest context advertised by an online worker that serves
the request's models. If the existing context already fits, no growth is needed. Compare the resolved context and
quote with
the same prompt under `ignore` rather than expecting a price increase for every input. Under `reject`, an
over-long prompt returns 400 with `PromptExceedsContext` and the prompt, output and context sizes involved.
Under `ignore`, the default, the request is sent as it is, and the worker cuts away the front of the
prompt.

If you queue the request instead of using a dry run, cancel it with
`DELETE /api/v2/generate/text/status/<request-id>`.

## Change a style with PATCH

A `PATCH` changes only the supplied top-level fields. Include the complete `parameter_policy` when changing it,
since the server replaces the declaration rather than merging individual settings. See
[Partial PATCH](../reference/style_contract.md#partial-patch) for the validation rules.

Before changing the policy, save its current declaration. Use the style's ID for reads so a cached name lookup
does not hide an update:

```bash
curl -fsS "$HORDE/styles/text/<style-id>" -o style-before.json
jq -e 'has("parameter_policy")' style-before.json
jq '{parameter_policy}' style-before.json > policy-before.json
```

Stop if the read fails or does not contain `parameter_policy`. Inspect `policy-before.json` before continuing.
A previous non-null object can be restored with PATCH. If it is null, adding a declaration cannot be reversed
by resetting it to null; create a separate style to experiment while preserving the original.

```bash
curl -s -X PATCH "$HORDE/styles/text/<style-id>" \
  -H "apikey: $APIKEY" \
  -H "Client-Agent: <your-client>:<version>:<contact>" \
  -H "Content-Type: application/json" \
  -d '{
    "parameter_policy": {
      "override": "listed",
      "overridable": ["max_length"],
      "ceilings": {"max_length": 256, "n": 2}
    }
  }'
```

Verification: read the style back. `parameter_policy` is the new object, and `prompt`, `params`,
`models`, `info` and `template_fields` are unchanged.

```bash
curl -s "$HORDE/styles/text/<style-id>" | jq '{parameter_policy, template_fields, prompt, params}'
```

If the saved policy was non-null, restore it with:

```bash
curl -fsS -X PATCH "$HORDE/styles/text/<style-id>" \
  -H "apikey: $APIKEY" -H "Content-Type: application/json" \
  --data-binary @policy-before.json
```

Read the style by ID again and compare `parameter_policy` with the saved object. Restoration affects future
requests; it does not undo requests already submitted under the replacement policy.

## Traps

- Style writes are rate limited per address, and a client creating several styles in a loop hits the
  per-second limit first and gets 429. Pace the writes against the `style_create_rate_limits` and
  `style_modify_rate_limits` published in the contract, and leave at least half a second between
  writes. On a 429, back off for the length of the limit's window instead of retrying at once. The
  limits are listed under [Write rate limits](../reference/style_contract.md#write-rate-limits).
- A style does not keep the order of its models. The list is trimmed to five and stored as a set, so
  the order read back is not the order sent. Text pricing uses whichever model comes first, so a
  multi-model text style can quote differently from one run to the next. Use one model per style where
  the price has to be stable.
- An optional placeholder that is left out of the request becomes the empty string. A template
  containing `about {subject}` produces the bare label `about ` when the field is not filled. Write the
  label into the placeholder's value, or make the field required.
- A ceiling is only accepted on a param the request can set. Under `none`, and under `listed` for a
  param not in `overridable`, the style's value is always used, so a ceiling there is rejected with
  400 when the style is written. The one exception is `n`, which comes from the request under every
  mode and may be capped under every mode.
- Under `listed`, a request param that is not in `overridable` is ignored without an error, the same as
  under `none`. A dry run's `resolved.params` shows which of the request's values were kept.
- `n` is not controlled by the policy. It always comes from the request and defaults to 1. Listing it
  in `overridable` is rejected.
- Using someone else's style adds 2 kudos to the quote, and cancelling a queued request does not
  refund them. Using a style that you authored does not cost extra. When the style's owner is credited
  is covered under [Style surcharge and author
  credit](../reference/style_contract.md#style-surcharge-and-author-credit).
- A text template's braces are format specifiers. `{{` is one literal brace, and an unknown `{word}`
  becomes the empty string. A placeholder with a format spec, a conversion, attribute or index access,
  or no name at all is refused with `StylePromptFieldInvalid`. Only `{{[NAME]}}`, with uppercase
  letters and underscores between the brackets, is kept as written. `{{[input]}}` and `{{[IN PUT]}}`
  are not protected and collapse to one brace each side. Check a template against the published
  `protected_patterns` regular expression before sending it.

## Handling a `schema_version` change

Pin the `schema_version` that the client was written against, and compare it on every read of the
contract.

- A higher version can add fields and can change what an existing field means. Before applying a
  version that the client has not been updated for, read the reference page and the decision records
  to see what changed. Until then, fall back to the client's defaults rather than applying the new
  document.
- Ignore keys that the client does not recognise, at every level of the document. New keys are added
  inside the existing sections. A client that rejects unknown keys breaks on an additive change that it
  could have ignored.
- The lists define what is accepted. Build authoring controls from `overridable_parameters` and
  `ceiling_parameters` as served rather than from a copy, so a param added to a params model reaches the
  client without a release on either side.
