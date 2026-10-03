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

Preconditions: an account that is trusted or holds the customizer role, since only those may create a
style; that account's API key; and network access to a horde deployment's `/api/v2` endpoints. The
procedures below use `/api/v2/status/style_contract`, `/api/v2/styles/text`, `/api/v2/styles/image`,
`/api/v2/generate/text/async` and `/api/v2/generate/async`.

End state: a text style and an image style that each declare a `parameter_policy` and, for the text
style, `template_fields`; a dry run under each that returns the request as the horde resolved it; and
both styles either kept or deleted again.

The rules these procedures work within are in the [style contract
reference](../reference/style_contract.md). A frontend building authoring controls and request forms
from the contract follows [adding styles to a frontend](add_styles_to_a_frontend.md), and a client that
integrated styles before the contract checks [the update for existing
integrations](update_a_style_integration.md). [ADR
17](../decisions/0017-protect-koboldcpp-placeholders-and-publish-the-style-contract.md) records why
the contract is published and why a text template protects instruct placeholders.

Every command below uses two shell variables:

```bash
HORDE=https://aihorde.net/api/v2
APIKEY=<your-api-key>
```

## 1. Read the contract before declaring anything

The contract states which params a policy of each type may hand over and cap, what the horde fills in
a prompt template, and what becomes of the braces around it. Read it first and build the rest of the
procedure from what it returns, rather than from values pasted from another deployment.

```bash
curl -s "$HORDE/status/style_contract" | jq .
```

Verification: the body carries `schema_version`, `text` and `image`. `jq '.text.overridable_parameters'`
lists the params a text policy may hand over and `jq '.text.ceiling_parameters'` the params it may cap
with the range each ceiling may take. `jq -r '.text.protected_patterns[0].pattern'` is the regular
expression a text template's protected placeholders have to match.

Reversal: none. The endpoint is read-only, needs no API key, and is exempt from the rate limiter.

## 2. Create a text style with a policy and placeholders

This style pins the sampler settings, hands `max_length` and `max_context_length` to the request under
ceilings, and declares two placeholders its template fills from the request.

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

Verification: the response carries the new style's `id`. Read it back and confirm both declarations
were stored, since a style that declares neither returns them as null:

```bash
curl -s "$HORDE/styles/text/<style-id>" | jq '{parameter_policy, template_fields, prompt}'
```

The `prompt` comes back with `{{[INPUT]}}` and `{{[OUTPUT]}}` exactly as sent.

Reversal: `curl -s -X DELETE "$HORDE/styles/text/<style-id>" -H "apikey: $APIKEY"`, which returns 200
with an `OK` message. Deleting a style is final; there is no undelete.

## 3. Create an image style with a policy

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

`template_fields` is null, since this style declares none. The `{dramatic|soft}` in the prompt is
stored and sent as written.

Reversal: `curl -s -X DELETE "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY"`.

## 4. Dry run a request under the style and read `resolved`

A dry run prices the request and returns the request as the horde resolved it, without queueing
anything. It is the way to confirm what a style does to a request before spending kudos on it.

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
- `style` carries the applied style's `id` and `name`.
- `estimated_prompt_tokens` and `context_fit` report how the prompt was sized.

The image equivalent posts the same `dry_run: true` to `$HORDE/generate/async`, and its `resolved`
adds `negative_prompt`, the half of the prompt after the `###`.

Reversal: none. A dry run creates nothing, charges nothing, and increments nothing except the style's
use count.

## 5. Size a text request against its prompt

`context_fit` decides what happens when the prompt the style produces does not fit inside
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
bounded by the style's ceiling and by the largest context an online worker serving the request's
models advertises, and `kudos` is higher than the same request under `ignore`. Under `reject` an
over-long prompt returns 400 with `PromptExceedsContext` and the three figures that did not add up.
Under `ignore`, the default, the request goes as it is and the worker cuts the front of the prompt
away.

Reversal: none for a dry run. For a queued request, `DELETE /api/v2/generate/text/status/<request-id>`
cancels it.

## 6. Change a style with PATCH

A `PATCH` changes only the fields the request carries. Anything left out keeps its stored value.

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

Reversal: `PATCH` again with the previous `parameter_policy` object, so read the style and keep its
current declarations before changing them. A `PATCH` replaces a JSON column whole rather than merging into it, and
there is no way to clear `parameter_policy` or `template_fields` back to null through the endpoint;
that takes a new style.

## Traps

- Two style writes a second per address and route, and on top of that 20 creations an hour, or 90
  patches or deletions of one style a minute. A client creating several styles in a loop hits the
  per-second limit first and gets 429. Pace the writes against the `style_create_rate_limits` and
  `style_modify_rate_limits` the contract publishes. Leave at least half a second between writes, and on
  a 429 back off for the window the limit covers instead of retrying at once.
- A style does not keep the order of its models. The list is trimmed to five and stored as a set, so
  the order read back is not the order sent. Text pricing uses whichever model comes first, so a
  multi-model text style can quote differently from one run to the next. Use one model per style where
  the price has to be stable.
- An optional placeholder the request leaves out becomes the empty string, so a template reading
  `about {subject}` produces the bare label `about ` when nothing fills it. Write the label into the
  placeholder's value, or make the field required.
- A ceiling is only accepted on a param the request can set. Under `none`, and under `listed` for a
  param not in `overridable`, the style's value is always used, so a ceiling there is rejected with
  400 when the style is written. The one exception is `n`, which comes from the request under every
  mode and may be capped under every mode.
- Under `listed`, a param the request sets that is not in `overridable` is ignored without an error,
  the same as under `none`. A dry run's `resolved.params` shows which values the request kept.
- `n` can never be handed over by a policy. It always comes from the request, defaults to 1, and
  listing it in `overridable` is rejected.
- Using someone else's style adds 2 kudos to the quote and credits that style's owner the part of
  those 2 the request paid, once it is queued. A requester at their minimum balance, such as
  the anonymous user, has the charge forgiven and credits the owner nothing. A dry run or a refused
  request credits nothing, and cancelling a queued request does not refund the 2. A style you authored
  costs nothing extra to use.
- A text template's braces are format specifiers. `{{` is one literal brace, an unknown `{word}`
  becomes the empty string, a placeholder with a format spec, a conversion, attribute or index access
  or no name at all is refused with `StylePromptFieldInvalid`, and only `{{[NAME]}}` with uppercase letters and underscores between the
  brackets is kept as written. `{{[input]}}` and `{{[IN PUT]}}` are not protected and collapse to one
  brace each side. Check a template against the published `protected_patterns` regular expression
  before sending it.

## Handling a `schema_version` change

Pin the `schema_version` the client was written against and compare it on every read of the contract.

- A higher version can add fields and can change what an existing field means. Read the reference page
  and the decision records for what moved before applying a version the client has not been updated
  for; until then, fall back to the client's own defaults rather than applying the new document.
- Ignore keys the client does not recognise, at every level of the document. New keys are added inside
  the existing sections, and a client that rejects unknown keys breaks on an additive change it could
  have ignored.
- The lists are the authority on what is accepted. Build authoring controls from
  `overridable_parameters` and `ceiling_parameters` as served rather than from a copy, so a param
  added to a params model reaches the client with no release on either side.
