---
title: "Style contract reference"
summary: "Style parameter overrides, prompt placeholders and text context sizing, with endpoint validation, dry-run responses and implementation references."
topics: [generation, requests]
order: 90
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Style contract reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

A style supplies the prompt template, parameters and model list for a request. Optional `parameter_policy`
and `template_fields` declarations control parameter overrides and additional prompt placeholders.
Text requests can use `context_fit` to size context for the rendered prompt. A dry run returns the
resolved request for either generation type.

The contract includes write limits, shared-key visibility and partial `PATCH` behaviour. See [Use the style
endpoints](../how-to/use_style_endpoints.md) for examples and [Add styles to a
frontend](../how-to/add_styles_to_a_frontend.md) for client integration.

## Applying a style to a request

Set a generate request's `style` field to the UUID or name of a style or collection. Applying a style:

- replaces the request's models with the style's;
- fills the style's prompt template, which must contain `{p}` for the request's prompt, and for an
  image style `{np}` for its negative prompt;
- merges the request's params into the style's under the style's `parameter_policy`;
- replaces the request's `nsfw` flag with the style's;
- increments the style's use count.

A style of the other generation type is refused with `StyleMismatch`.

The style's params are the starting point and the request's params are discarded, with one exception:
`n`, the number of takes requested, always comes from the request and defaults to 1. A policy cannot
cover `n` either.

Text and image styles differ in how the template is filled, covered under [Prompt template
rules](#prompt-template-rules). An image request's prompt is split at the first `###` into the
positive and negative halves before formatting. When the template does not contain `###` and the
request has a negative half, a `###` is put in front of that half.

### Collections

When `style` resolves to a collection, one of its styles is drawn at random, and the request runs
under the drawn style from then on. The request counts one use of the collection and one use of the
drawn style. A collection does not have a prompt, parameters or shared key of its own.

The draw includes only styles that declare every supplied field and do not require an omitted field.
A collection's styles can declare different fields, and drawing from all of them would make the same
request succeed or fail by chance. A style whose stored `template_fields` fails validation is left
out of the draw. When no style fits, the request is refused with
`TemplateFieldsMatchNoCollectionStyle`.

### Style surcharge and author credit

Using someone else's style adds 2 kudos (`STYLE_OWNER_REWARD`) to the request's quote. A request under
a style authored by the requester is not charged the surcharge, and the author is not credited.

Once the request is queued, the style's owner is credited the part of the surcharge that the
requester's debit collected. The minimum-balance floor can forgive part or all of that debit, as it
does for the anonymous user and for any account at its minimum. The forgiven amount is taken from the
surcharge first, so the owner is credited only the surcharge minus the forgiven part.

A dry run quotes the surcharge without crediting the author. A request refused before activation
does not credit the author either. The surcharge is not refunded when the request is cancelled, the
same as the base horde tax. The owner keeps the amount that was collected.

### Shared keys

A style may include a shared key. When the key is valid, applying the style adopts it as the
request's key, so the style's owner pays. A collection adopts the shared key of the drawn style. The
per-job limits on that key are then not enforced against the request, since its owner chose to
attach the key to the style. Who can read a style's shared key is covered under [Shared key
visibility](#shared-key-visibility).

## Prompt template rules

Both types fill `{p}` and the placeholders declared by the style. A placeholder that is not filled
becomes the empty string. The types differ in how much of the rest of the template is left unchanged.

| | Text | Image |
| --- | --- | --- |
| Filled by the horde | `{p}` | `{p}`, `{np}` |
| Declared placeholders filled | Yes | Yes |
| A brace outside those | Python format string grammar, in which `{{` and `}}` are one literal brace each and any other bare `{name}` is a placeholder that becomes empty when it is not filled | Literal. Every brace is doubled before formatting and only the filled placeholders are put back |
| A field that is more than a bare name | Refused when the style is written (`StylePromptFieldInvalid`). One already stored is sent as written | Literal, like any other brace |
| Protected patterns | `\{\{\[[A-Z_]+\]\}\}` | None |

A text template is parsed with `string.Formatter`, so a template written for a Python format string
keeps working: `{{` is one literal brace. Only a bare name is filled. A format spec (`{p:>20000}`), a
conversion (`{p!r}`), attribute or index access (`{p.__class__}`, `{p[0]}`) and a positional field
(`{}`, `{0}`) have no use in a prompt. A format spec would also let a template make every request build
an arbitrarily large string. A style written with one of these fields, or with a lone brace that does
not parse, is refused with `StylePromptFieldInvalid`.

When a template stored before that check has such a field, the field is sent to the worker exactly as
written. A stored template whose braces do not parse is refused at request time with
`StyleDeclarationInvalid`. Patching any field of a style revalidates the stored prompt, so a style with
such a field must have its prompt corrected on its next edit.

An image template has every brace doubled first, and only `{p}`, `{np}` and its declared placeholders
are put back. A wildcard or a workflow string in braces therefore reaches the worker as written.

### The protected pattern

`{{[NAME]}}`, with uppercase letters and underscores between the brackets, is an instruct placeholder
that a text backend such as koboldcpp fills in when it builds its prompt. Formatting a text template
would turn `{{[INPUT]}}` into `{[INPUT]}` and strip it out of a template written for that backend.
Each match of `\{\{\[[A-Z_]+\]\}\}` is therefore replaced with a marker before formatting, and the
original text is put back afterwards. The token reaches the worker exactly as written.

The marker includes a random part drawn on each call. A value supplied through `template_fields`
therefore cannot be written to match a marker and be turned into a placeholder when the original text
is put back.

The pattern covers exactly what the regular expression matches. A lowercase name (`{{[input]}}`) or a
name with anything but uppercase letters and underscores in it (`{{[IN PUT]}}`) is ordinary template
text. It is formatted under the usual rule and collapses to one brace each side. A doubled brace
anywhere else in the template still becomes one brace.

An image template does not need a protected pattern. Every brace outside a filled placeholder is already literal.

[ADR 17](../decisions/0017-protect-koboldcpp-placeholders-and-publish-the-style-contract.md) records
why the pattern is protected, instead of replacing the text rule with the image rule.

## `parameter_policy`

A policy is a JSON object on the style with three optional keys.

| Key | Meaning |
| --- | --- |
| `override` | `none`, `listed` or `all`. Defaults to `none`. |
| `overridable` | The params a request may set. Read only under `listed`. |
| `ceilings` | The largest value a request may set, per param. |

The three modes:

- `none`: the style's params are used, and the request's params body is ignored without an error. A
  style without a policy behaves the same way.
- `listed`: only the params listed in `overridable` may come from the request. Any other param set by
  the request is ignored without an error, as under `none`. Clients commonly send every param that has
  a default, so refusing unlisted ones would refuse nearly every request.
- `all`: every param may come from the request, except `n`, which comes from it regardless.

`overridable` may list any param of the style type's params model except `n`. The published contract
lists them as `overridable_parameters`. A text style may cap `max_length`, `max_context_length` and
`n`. An image style may cap `width`, `height`, `steps` and `n`. Each ceiling must lie within the bounds
of the param it caps, published as `ceiling_parameters`.

Rejections at declaration time, all 400 with the return code shown:

| Declaration | Return code |
| --- | --- |
| `overridable` sent under `none` or `all` | `StylePolicyOverridableMisplaced` |
| `listed` without a list, or with an empty one | `BadRequest` |
| An entry that is not in the params model, or `n` | `BadRequest` |
| A ceiling on a param that this style type cannot cap | `BadRequest` |
| A ceiling on a param that the mode does not let the request set | `BadRequest` |
| A ceiling outside the bounds of the param it caps | `BadRequest` |
| An unknown key, an unknown mode, or a malformed value | `BadRequest` |

Repeated entries in `overridable` are collapsed and the given order is kept. An explicit `null` counts
as absence, so it is accepted under any mode.

### Ceilings

A ceiling applies only to parameters controlled by the request. It is accepted only where the policy permits
an override. `n` always comes from the request and can be capped in every mode.

| Mode | Accepted ceilings |
| --- | --- |
| `all` | Any parameter supported for ceilings by the style type |
| `listed` | `n` and supported parameters listed in `overridable` |
| `none` | `n` only |

Other ceilings are rejected when the style is written, with the parameter and mode in the message.
A value above a ceiling is rejected with `StyleParameterAboveCeiling` and is not trimmed, because
trimming would change what the request costs without reporting it. A value exactly at the ceiling is
accepted, and the style's params are never checked against a ceiling.

### Rejections at request time

The stored declarations are validated again each time the style is applied, against the same rules as
the style endpoints. A stored declaration can fail validation when the rules or the params model
change after it was written. The request is then refused with a 400.

| Request | Return code |
| --- | --- |
| A value above the style's ceiling for that param | `StyleParameterAboveCeiling` |
| The style's stored `parameter_policy` or `template_fields` fails validation | `StyleDeclarationInvalid` |

An image style that does not set `width` or `height` takes each missing one from the request. That
fallback applies only to a style without a policy. When a policy is declared, the policy controls
every param.

## `template_fields`

See [payload and template limits](service_limits.md#images-and-styles) for field-count, name-length and supplied-word
bounds, and [charging constants](service_limits.md#accounting-units-floors-and-surcharges) for the style surcharge.

A style declares up to 16 placeholders, each an object with `name`, `description` (1 to 200 characters)
and `required` (default false). A placeholder is 1 to 32 characters of lowercase letters, digits and
underscores starting with a letter, so `str.format_map` can address it. `p` and `np` are filled by the
horde and cannot be declared, and the same placeholder may not be declared twice.

A request supplies values as a flat object of strings keyed by placeholder. Each value, and all of the
values together, are limited to 7500 words, the maximum length of a prompt.

| Request | Return code |
| --- | --- |
| `template_fields` sent without a `style` | `TemplateFieldsRequireStyle` |
| A placeholder that the style does not declare | `TemplateFieldUnknown` |
| A required placeholder left out | `TemplateFieldMissing` |
| A value that is not a string, or an object that is not a mapping | `BadRequest` |
| One value, or all of them together, longer than a prompt may be | `InvalidPromptSize` |

Both generation types format with `str.format_map` over a `defaultdict(str)`. An unfilled placeholder in the
style's prompt therefore formats to the empty string, and the request does not fail. A declared
placeholder does not have to appear in the prompt either. This lets a prompt be rewritten without
rewriting its declarations.

## `context_fit`

`context_fit` is a text request setting. Image requests do not have an equivalent. A text request's
prompt is estimated at `ceil(len(prompt) / 3)` tokens, after any style templating. A prompt fits when
`estimated_prompt_tokens + max_length <= max_context_length`, so the tokens to be generated count
against the context alongside the prompt. `max_length` defaults to 80 and `max_context_length` to
2048.

| `context_fit` | Behaviour when the prompt does not fit |
| --- | --- |
| `ignore` (default) | The request is sent as it is, and the worker cuts away the front of the prompt. |
| `reject` | The request is refused with `PromptExceedsContext`. The error includes the estimated prompt size, requested output length and context length. |
| `grow` | `max_context_length` is raised until the prompt fits, and the request is charged for the larger context. |

`ignore` is the default because it keeps text requests compatible. A request sent without these fields
is accepted unchanged, and the worker cuts away the front of an over-long prompt.

Growth rounds up to the smallest power of two that fits, because workers advertise powers of two and
the kudos curve is written against them. Growth is limited to the smallest of three figures:

- 1048576, the largest `max_context_length` accepted by the params model;
- the style's `max_context_length` ceiling, when one applies;
- the largest `max_context_length` advertised by an online text worker that serves one of the
  request's models. Online means a check-in within the last 300 seconds.

When the next power of two is past the bound but the request still fits underneath it, the bound is
used instead of the power of two. When no size within the bound fits, the request is refused with
`PromptExceedsContext` quoting the bound. Under `reject` the bound is 1048576, since the context does
not grow.

When no online text worker serves any of the request's models, the context of a `grow` request does not
grow. The bound is the `max_context_length` set by the request, so a prompt that does not fit within it
is refused with `PromptExceedsContext` quoting that size. A prompt that already fits is accepted as it
is.

## The dry run body

A dry run of `/v2/generate/async` or `/v2/generate/text/async` returns 200 with `kudos` and `resolved`.
`resolved` describes the request after applying the style and sizing the context:

| Key | Value |
| --- | --- |
| `prompt` | The prompt after templating |
| `params` | The params after the style, its ceilings and any growth |
| `models` | The models the request runs on |
| `style` | The applied style's `id` and `name`, or null. Under a collection, the drawn style |
| `estimated_prompt_tokens` | Text only. The token estimate used by `context_fit` |
| `context_fit` | Text only. The mode that was applied |
| `negative_prompt` | Image only. The half of the prompt after the `###`, or null when the prompt does not contain `###` |

A null inside `resolved` is part of the response. The queued 202 response does not include
`resolved`.

## What a request costs

A text request is priced from the two sizes that this contract can change, `max_context_length` and
`max_length`. The context multiplier is `1.2 + 2.2 ** log2(max_context_length / 1024)`, clamped to
between 0.1 and 30, so each doubling of the context multiplies the factor by roughly 2.2. The rest is
linear in `max_length`.

A known model charges
`max_length * parameter_bonus * model_multiplier * context_multiplier / 100`, where `parameter_bonus`
is `(max(model_multiplier, 13) / 13) ** 0.20`. An unknown model charges
`max_length * 0.027 * context_multiplier`. An empty model list is priced as a 13B model.

Growing a context therefore costs more kudos. This is why growth is bounded, and why a value above a ceiling is
rejected and not trimmed.

An image request is priced from its resolution, its sampler work and its post-processing, so the
params that an image policy may cap (`width`, `height`, `steps`) are the ones that change the price.

## Style endpoints

### Served fields

Style routes of both types always include `parameter_policy` and `template_fields` on a served style,
null when the style does not declare them. The `params` object includes only the style's explicitly
set parameters. For a text style, these include `max_length` and `max_context_length` when the style
sets them.

### Shared key visibility

`shared_key` is served only to the style's owner, and only on the single-style routes:
`/v2/styles/text/{style_id}`, `/v2/styles/text_by_name/{style_name}`, `/v2/styles/image/{style_id}` and
`/v2/styles/image_by_name/{style_name}`. These take an optional `apikey` header. When the key resolves
to the style's owner, `shared_key` includes the shared key's details. For every other caller it is null.
A request with a key other than the anonymous key bypasses the 30-second response cache that these
routes share across callers. Its response includes `Cache-Control: private, no-store`.

The list routes, `/v2/styles/text` and `/v2/styles/image`, serve `shared_key` as null to every caller,
including the owner.

### Partial PATCH

A `PATCH` changes only the fields it includes. The stored value of an omitted key is not changed.
Sending a field with its current value does not change it.

A supplied `params`, `parameter_policy` or `template_fields` replaces the complete stored value. Nested
settings and list entries are not merged with existing contents. The endpoint cannot clear
`parameter_policy` or `template_fields` back to null.

### Write rate limits

Style write limits are kept per address. They are not kept per account.

| Operation | Limits, all applied at once |
| --- | --- |
| Create a text or image style | 20 an hour and 2 a second |
| Patch or delete a text or image style | 90 a minute and 2 a second |
| Create a collection | 90 a minute and 2 a second |
| Patch or delete a collection | 90 a minute and 2 a second |
| Read styles and collections | The app default, 90 a minute |

Every write limit is keyed on the address, the method and the request path, so the patch and delete
limits apply to each style separately. A whitelisted service address is allowed 10 a second and 300 a
minute. The hourly create limit is the same for every address. Exceeding a limit returns 429.

A client creating several styles in a loop hits the per-second limit first. It should pace its writes
against the published `style_create_rate_limits` and `style_modify_rate_limits`, and on a 429 back off
for the length of the limit's window.

### Cached reads after a write

A style's PATCH or DELETE, and an image style's example writes, clear the cached responses for reads
of the style by id, by name, and by the name qualified with its owner's alias. This includes the old
name replaced by a rename. Any other spelling of the name can serve the earlier version for up to 30
seconds. The list and collection routes are not cleared after a write. The image style list and the
collection routes can lag by up to 30 seconds, and the text style list by up to 1 second.

## The published contract

`GET /api/v2/status/style_contract` serves the rules needed for style authoring and styled requests.
It does not require an API key and is exempt from the rate limiter. The document is compiled from the
same rules that the style endpoints and the request path enforce, so the published rules and the
enforced rules agree.

### Response fields

The body has three keys: `schema_version`, and one section each for `text` and `image`.

| Field | Meaning |
| --- | --- |
| `schema_version` | Which version of the contract is being served. Starts at 1 and is raised whenever the meaning of the published document changes: a key is added, removed or renamed, a rule is stated differently, or the horde enforces a rule differently. |
| `text`, `image` | What a style of that type may declare, and how the braces in its prompt are handled. |
| `placeholders` | The placeholders filled by the horde itself, written without braces. `["p"]` for text, `["p", "np"]` for image. Neither can be declared in `template_fields`. |
| `declared_fields_are_placeholders` | Whether a placeholder declared in `template_fields` is filled from the request's `template_fields` object. True for both types. |
| `protected_patterns` | Template text that formatting leaves exactly as written, each an object with `pattern` (a regular expression) and `description`. Empty for a type without protected patterns. |
| `brace_handling` | A short statement of what the braces in this type's prompt template mean. |
| `overridable_parameters` | Every param a policy of this type may put in `overridable`, sorted. Anything outside this list is refused at declaration time. |
| `ceiling_parameters` | The params that a policy of this type may cap, each an object with `minimum` and `maximum` giving the allowed range for that ceiling. A null bound means that the param does not have that bound. |
| `context_fit_modes` | How a request of this type may be sized against its prompt. Null for image, since image requests cannot be sized. |
| `style_create_rate_limits` | Every limit on creating a style of this type, as listed under [Write rate limits](#write-rate-limits). |
| `style_modify_rate_limits` | Every limit on patching or deleting a style of this type, as listed under [Write rate limits](#write-rate-limits). |

### Text contract example

The `text` section as served:

```json
{
  "schema_version": 1,
  "text": {
    "placeholders": [
      "p"
    ],
    "declared_fields_are_placeholders": true,
    "protected_patterns": [
      {
        "pattern": "\\{\\{\\[[A-Z_]+\\]\\}\\}",
        "description": "An instruct placeholder a text backend fills in when it builds its own prompt, such as '{{[INPUT]}}' or '{{[OUTPUT]}}'."
      }
    ],
    "brace_handling": "A doubled brace is one literal brace, '{p}' and the placeholders this style declares are filled in, and a placeholder nothing fills becomes the empty string. A placeholder is a bare name, and one with a format spec, a conversion, attribute or index access, or no name is refused. The protected patterns below are the exception and are left exactly as written.",
    "overridable_parameters": [
      "dynatemp_exponent", "dynatemp_range", "frmtadsnsp", "frmtrmblln", "frmtrmspch",
      "frmttriminc", "max_context_length", "max_length", "min_p", "rep_pen", "rep_pen_range",
      "rep_pen_slope", "sampler_order", "singleline", "smoothing_factor", "stop_sequence",
      "temperature", "tfs", "top_a", "top_k", "top_p", "typical", "use_default_badwordsids"
    ],
    "ceiling_parameters": {
      "max_context_length": { "minimum": 80, "maximum": 1048576 },
      "max_length": { "minimum": 16, "maximum": 16384 },
      "n": { "minimum": 1, "maximum": 20 }
    },
    "context_fit_modes": [
      "ignore",
      "reject",
      "grow"
    ],
    "style_create_rate_limits": [
      "20/hour",
      "2/second"
    ],
    "style_modify_rate_limits": [
      "90/minute",
      "2/second"
    ]
  }
}
```

The `image` section has the same keys with different values: `placeholders` is `["p", "np"]`,
`protected_patterns` is empty, `brace_handling` states that every brace is literal except the filled
placeholders, `context_fit_modes` is null, `ceiling_parameters` covers `width` and `height` (64 to
3072), `steps` (1 to 500) and `n` (1 to 20), and `overridable_parameters` is the 34 params of the
image payload model, from `cfg_scale` through `workflow`.

### Client version handling

A client pins `schema_version` and re-reads the document when it changes. A version increase can add
fields and can change what an existing field means. A client that does not support a version it
receives should fall back to its defaults rather than apply the document.

## Sharp edges

- A style does not keep the order of its models. `Style.parse_models` returns a `set` after trimming
  the list to five, and `set_models` writes the rows from that set. The order given when the style is
  created is therefore not the order returned by `get_model_names`. A request that uses the style has
  its model list replaced by that unordered list. Text pricing is based on whichever model is first,
  so the quote for a multi-model text style depends on an order that was not chosen by the author.
- The token estimate does not use a tokenizer. `ceil(len(prompt) / 3)` is a conservative character
  count, so `reject` can refuse a prompt that would fit by a real tokenizer's count, and `grow` can buy
  context that a request does not need. Per-model tokenization would make the estimate exact.
- Growth is refused when no online worker serves the request's models. In that case the
  `max_context_length` returned by `get_highest_text_worker_limits` is null, and
  `context_growth_upper_bound` then returns the requested `max_context_length`. A `grow` request whose
  prompt does not fit is therefore rejected with `PromptExceedsContext` at the requested size. The same
  request under `ignore` would be queued and wait for a worker to come online. Under `grow`, the client
  has to retry after a worker has checked in within the 300-second window.
- A patch replaces the complete value of a supplied JSON field. Include unchanged settings when sending
  `parameter_policy` or `template_fields`, as described under [Partial PATCH](#partial-patch).

## Implementation notes

### Request path order

`GenerateTemplate.apply_style` resolves the style by UUID, then by name. When the result is a
`StyleCollection`, `GenerateTemplate.draw_collection_style` draws the style and counts the
collection's use, filtering with `template_fields_fit`. `apply_style` adopts a valid shared key from
the style it ends up with.

`apply_style` runs inside each generation type's `validate`, before the shared `super().validate()` that
resolves the user. For a text request `apply_context_fit` runs immediately after it. Each generation type then
calls `GenerateTemplate.decide_style_surcharge` after `super().validate()`, since the surcharge
comparison needs both the style and the user. The two rows are compared by id.

`TextAsyncGenerate.initiate_waiting_prompt` and `ImageAsyncGenerate.initiate_waiting_prompt` skip the
shared key's per-job limit check only when the key in use is the style's key.

`n` is listed in `NON_OVERRIDABLE_PARAMETER_NAMES`, which keeps it out of every policy.

### Surcharge crediting

The surcharge is added through the `kudos_adjustment` argument that `GenerateTemplate` passes to
`WaitingPrompt.activate` and to `extrapolate_dry_run_kudos`. The request path does not credit the
owner directly. `GenerateTemplate.style_reward` builds a `StyleReward` (the author's id, the surcharge
and the style type), which `WaitingPrompt.activate` attaches to the requester's activation debit.
`StyleReward.collected` computes the credited amount after the floor.

In ledger mode the floor is applied when the kudos applier folds the debit, so the applier credits the
owner from the debit's detail (`_credit_style_authors`). In shadow mode the floor is applied inline,
and `project_style_reward` credits the owner during activation.

### Declaration vocabulary

`StyleParameterPolicy` in `horde/classes/base/style_contract.py` defines a policy once, for the
endpoints that accept it and the request path that applies it. A policy is validated against the
params model of its style type rather than against a restated list. `text_style_contract_vocabulary`
in `horde/apis/v2/kobold.py` and `image_style_contract_vocabulary` in `horde/apis/v2/stable.py` build
that vocabulary next to the params model that it is built from, from `TEXT_CEILING_PARAMETER_NAMES`
and `IMAGE_CEILING_PARAMETER_NAMES`. They reach the pydantic models through the validation context,
so `horde/classes/base/style_contract.py` does not import the API layer. The style endpoints and the
request path validate against the same vocabulary.

`validate_text_template_fields` refuses a text template field that is more than a bare name.
`format_text_style_prompt` applies `PROTECTED_TEXT_PLACEHOLDER_PATTERN`. Placeholder value limits use
`MAX_TEMPLATE_FIELD_WORDS`, the same limit that `ParamValidator` places on a prompt.

### Changing the protected pattern

The rule is stated in three places that have to change together: `format_text_style_prompt`, the
published contract document, and this page. Changing it means raising the document's
`SCHEMA_VERSION` and rewriting all three.

### Context sizing

`estimate_prompt_tokens` divides by `CHARS_PER_TOKEN_ESTIMATE`. The defaults are
`DEFAULT_MAX_LENGTH` and `DEFAULT_MAX_CONTEXT_LENGTH`, the same defaults as `TextWaitingPrompt`.
`context_growth_upper_bound` takes the smallest of `MAX_CONTEXT_LENGTH_LIMIT`, the style ceiling and
the worker figure from `get_highest_text_worker_limits`. `TextAsyncGenerate.get_context_growth_upper_bound`
returns `MAX_CONTEXT_LENGTH_LIMIT` for any mode other than `grow`.

### Dry-run response and quote cache

`resolved` is built by `GenerateTemplate.get_resolved_request` and its per-type overrides. The nesting
does not skip nulls. The queued 202 response is marshalled through the same model with `resolved`
unset, and the endpoint's `skip_none` drops it.

Dry-run quotes are cached in Redis against a hash of the params, the model list, the extra-source-image
count and whether the request specifies a style (`get_hashed_params_dict`). The hash is computed twice,
at the cache lookup before validation and at the store after it, so its keys use only the values sent
in the request. The `styled` key comes from the `style` argument, since the surcharge flag is only set
during validation. The text and image overrides take the model list from the request arguments, since
applying a style replaces the resolved models.

`GenerateTemplate.dry_run_quote_unknown` bypasses that cache whenever the request specifies a
style or supplies template fields, since the quote and the resolved body then depend on the prompt and
on the outcome of resolution. The text override also bypasses it for a `context_fit` other than
`ignore`. `TextAsyncGenerate.dry_run_admission_dependent` bypasses it for a request that could be refused for want
of upfront kudos (`TextAsyncGenerate.may_require_upfront_kudos`). The `horde.generate.dry_run_cache_lookups`
counter records each dry run's lookup with both reasons.

### Served styles

Style routes of both types marshal without `skip_none`, which keeps `parameter_policy` and
`template_fields` present when null. The `params` object keeps `skip_none`.
`Style.get_details(include_shared_key=...)` controls whether `shared_key` is filled, and it defaults to
off. `SingleStyleTemplateGet.get_existing_style` turns it on for the owner.

The patch parser's arguments do not have defaults, which is what leaves omitted keys unchanged.
`style_read_cache_keys` and `clear_cached_responses` in `horde/apis/v2/styles.py` clear the
single-style reads after a write. The write limits are declared for the write methods only
(`STYLE_CREATE_METHODS`, `STYLE_MODIFY_METHODS`), with `STYLE_CREATE_WINDOW_RATE_LIMIT` for the hourly
create limit.

### Contract compilation

`horde/style_contract_document.py` compiles the contract from the same `StyleContractVocabulary`
objects that are used for validation by the style endpoints, and from the same formatting constants
and rate limit constants that are used by the request path and the routes. The document is a pure
function of the installed code, so it is compiled once per process and returned to every caller. It is
not cached between processes.

## Logs and metrics

Each of these leaves a log line: a request under a style, a style or collection write, and a stored
declaration that fails validation. Volume and outcomes are also counted in metrics with bounded labels.
The style, its owner and the requester are recorded only in log lines and span attributes.

### Log lines

| Line starts with | Level | Includes |
| --- | --- | --- |
| `Style applied:` | info for a queued request, debug for a dry run | Style id, name and type, owner, requester, client agent, collection id or `none`, policy mode or `undeclared`, template fields supplied and declared, `literal_template_fields` (fields of a stored text template sent as written), whether the surcharge applied, dry run |
| `Style created:`, `Style patched:`, `Style deleted:` | info | Style id, name, type, policy mode, declared field count, visibility, and the account. A patch adds the supplied fields. |
| `Collection created:`, `Collection patched:`, `Collection deleted:` | info | Collection id, name, type, style count, visibility, and the account |
| `Style example added:`, `Style example patched:`, `Style example deleted:` | info | Style id, example id, the account, and whether the example is primary |
| `Stored style declaration invalid:` | warning | The style and the declaration that fails validation, with the reasons |
| `Style ... left out of a collection draw` | info | A collection's style skipped because its stored template fields fail validation |
| `Context grown from` | debug | The requested and the grown context of a text request. The counter below records the volume |

A dry run is logged at debug because a client previews a styled request repeatedly while the user
changes its inputs. The counter still counts it.

### Metrics

| Metric | Unit | Labels |
| --- | --- | --- |
| `horde.style.applications` | 1 | `horde.gentype`, `horde.style.source` (`style` or `collection`), `horde.style.policy` (`none`, `listed`, `all` or `undeclared`), `horde.style.template_fields` (`declared` or `none`), `horde.style.surcharged`, `horde.dry_run` |
| `horde.style.author_credits` | kudos | `horde.style.type` |
| `horde.style.forgiven_surcharges` | kudos | `horde.style.type`. Counts the part of a surcharge that was forgiven by the minimum-balance floor and not credited |
| `horde.text.context_fit` | 1 | `horde.context_fit.mode`, `horde.context_fit.outcome` (`fits`, `sent_overlong`, `grown`, `refused`) |
| `horde.api.rejections` | 1 | `horde.rc`, `http.response.status_code`, `http.route`, `http.request.method`. Counts every API refusal, styles included |

`horde.api.rejections` labels a return code missing from `KNOWN_RC` as `Unlisted`, and a refusal raised
outside a matched route as `unmatched`, so the set of values for these labels does not grow with input.

### Span attributes

The span that validates a styled generate request includes `horde.style.id`, `horde.style.owner_id`,
`horde.style.collection_id`, `horde.style.policy` and `horde.style.surcharged`.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Policy and placeholder declarations | `horde/classes/base/style_contract.py` | `StyleParameterPolicy`, `StyleTemplateFields` |
| Declaration validation and storage | `horde/classes/base/style_contract.py` | `parse_parameter_policy`, `parse_template_fields`, `serialize_parameter_policy`, `load_parameter_policy`, `load_template_fields` |
| Param merge and ceilings | `horde/classes/base/style_application.py` | `merge_client_parameters` |
| Placeholder resolution | `horde/classes/base/style_application.py` | `resolve_template_field_values` |
| Text template formatting and the protected pattern | `horde/classes/base/style_application.py` | `format_text_style_prompt`, `PROTECTED_TEXT_PLACEHOLDER_PATTERN` |
| The published contract | `horde/style_contract_document.py` | `compile_style_contract`, `published_style_contract`, `SCHEMA_VERSION` |
| The endpoint that serves it | `horde/apis/v2/styles.py` | `StyleContract.get` |
| The contract's response shape | `horde/apis/models/v2.py` | `response_model_style_contract`, `model_style_contract_type` |
| The style surcharge and the owner comparison | `horde/apis/v2/base.py` | `GenerateTemplate.decide_style_surcharge`, `GenerateTemplate.style_reward` |
| Crediting the owner the amount collected by the surcharge | `horde/database/kudos_ledger.py`, `horde/database/kudos_legacy_projection.py`, `horde/classes/base/kudos.py` | `_credit_style_authors`, `project_style_reward`, `StyleReward.collected` |
| Token estimate and fit rule | `horde/classes/kobold/request_fit.py` | `estimate_prompt_tokens`, `prompt_fits_context` |
| Context sizing and its bound | `horde/classes/kobold/request_fit.py` | `fit_context_length`, `context_growth_upper_bound` |
| Shared merge, resolution and dry-run body | `horde/apis/v2/base.py` | `GenerateTemplate.apply_style_contract`, `GenerateTemplate.get_resolved_request`, `GenerateTemplate.dry_run_quote_unknown` |
| Vocabulary hook for the request path | `horde/apis/v2/base.py`, `horde/apis/v2/kobold.py`, `horde/apis/v2/stable.py` | `GenerateTemplate.style_contract_vocabulary`, `TextAsyncGenerate.style_contract_vocabulary`, `ImageAsyncGenerate.style_contract_vocabulary` |
| Style application on a text request | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.apply_style`, `TextAsyncGenerate.apply_context_fit` |
| Style application on an image request | `horde/apis/v2/stable.py` | `ImageAsyncGenerate.apply_style`, `ImageAsyncGenerate.get_resolved_request` |
| Declaration vocabulary and ceiling bounds | `horde/apis/v2/kobold.py`, `horde/apis/v2/stable.py` | `text_style_contract_vocabulary`, `image_style_contract_vocabulary`, `TEXT_CEILING_PARAMETER_NAMES`, `IMAGE_CEILING_PARAMETER_NAMES` |
| Type-specific hooks called by the shared endpoints | `horde/apis/v2/styles.py` | `StyleContractArgs.style_contract_vocabulary`, `StyleContractArgs.parse_type_specific_args`, `StyleContractArgs.apply_type_specific_args` |
| Shared request and response shapes | `horde/apis/models/v2.py` | `model_style_parameter_policy`, `model_style_template_field`, `response_model_resolved_request` |
| Per-type response shapes | `horde/apis/models/kobold_v2.py`, `horde/apis/models/stable_v2.py` | `response_model_async`, `response_model_resolved_request` |
| Worker context ceiling | `horde/database/functions.py` | `get_highest_text_worker_limits`, `TextWorkerLimits` |
| Text pricing | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.calculate_kudos` |
| Style columns | `horde/classes/base/style.py` | `Style.parameter_policy`, `Style.template_fields` |
| The style a generation ran under | `horde/classes/base/waiting_prompt.py`, `horde/classes/stable/genstats.py`, `horde/classes/kobold/genstats.py` | `WaitingPrompt.style_id`, `ImageGenerationStatistic.style_id`, `TextGenerationStatistic.style_id` |
| The owner-only `shared_key` on a served style | `horde/apis/v2/styles.py`, `horde/classes/base/style.py` | `SingleStyleTemplateGet.get_existing_style`, `Style.get_details` |
| Style write rate limits | `horde/apis/v2/styles.py`, `horde/apis/limiter_api.py` | `STYLE_CREATE_WINDOW_RATE_LIMIT`, `STYLE_CREATE_METHODS`, `STYLE_MODIFY_METHODS`, `get_request_90min_limit_per_ip`, `get_request_2sec_limit_per_ip` |
| Style application log line, counter and span attributes | `horde/apis/v2/base.py` | `GenerateTemplate.report_style_application` |
| Style and collection write log lines | `horde/apis/v2/styles.py` | `describe_style`, `describe_collection`, `STYLE_PATCH_FIELDS` |
| Author credit and forgiven surcharge counters | `horde/classes/base/kudos.py` | `StyleReward.count_outcome` |
| Context fit counter | `horde/apis/v2/kobold.py`, `horde/classes/kobold/request_fit.py` | `TextAsyncGenerate.count_context_fit`, `ContextFitOutcome` |
| Literal fields of a stored text template | `horde/classes/base/style_application.py` | `count_literal_text_template_fields` |
| API refusal counter | `horde/exceptions.py`, `horde/metrics.py` | `handle_bad_requests`, `rejection_attributes`, `api_rejections` |
| The dry-run quote cache key | `horde/apis/v2/base.py`, `horde/apis/v2/stable.py`, `horde/apis/v2/kobold.py` | `GenerateTemplate.get_hashed_params_dict`, `ImageAsyncGenerate.get_hashed_params_dict`, `TextAsyncGenerate.get_hashed_params_dict` |

## Tests

| Contract | Test |
| --- | --- |
| Override modes, `overridable` placement and ceiling bounds | `tests/unit/test_style_contract.py`, `TestParameterPolicyOverride`, `TestParameterPolicyOverridable`, `TestParameterPolicyCeilings` |
| A ceiling only on a param that the mode lets the request set | `tests/unit/test_style_contract.py`, `TestParameterPolicyCeilingMode` |
| Which params each mode accepts from the request | `tests/unit/test_style_contract.py`, `TestParameterPolicyApplication` |
| Placeholder names, descriptions, limit and uniqueness | `tests/unit/test_style_contract.py`, `TestTemplateFields` |
| Declarations are stored and read back unchanged | `tests/unit/test_style_contract.py`, `TestRoundTrip` |
| A stored declaration that fails validation is refused with `StyleDeclarationInvalid` | `tests/unit/test_style_contract.py`, `TestStoredDeclarationValidation`; `tests/integration/test_text_style_application.py`, `TestTextStyleParameterPolicy` |
| Param merge, unlisted params ignored, and `n` coming from the request | `tests/unit/test_style_application.py`, `TestParameterMerge` |
| A value above a ceiling is refused, not trimmed | `tests/unit/test_style_application.py`, `TestParameterCeilings` |
| Supplied placeholders, required ones and prompt-length bounds | `tests/unit/test_style_application.py`, `TestTemplateFields` |
| The chars-per-token estimate and the fit rule | `tests/unit/test_text_request_fit.py`, `TestPromptTokenEstimate`, `TestFitRule` |
| `context_fit` parsing, growth rounding and the bound | `tests/unit/test_text_request_fit.py`, `TestContextFitParsing`, `TestContextFitting`, `TestGrowthBound` |
| Text declarations are stored, served and patched | `tests/integration/test_text_styles.py`, `TestTextStyleContract`, `TestTextStylePartialPatch`, `TestTextStyleContractRejections` |
| Image declarations are stored, served and patched | `tests/integration/test_image_styles.py`, `TestImageStyleContract`, `TestImageStyleContractRejections` |
| A policy applied to a live text request | `tests/integration/test_text_style_application.py`, `TestTextStyleParameterPolicy` |
| Placeholders applied to a live text request | `tests/integration/test_text_style_application.py`, `TestTextStyleTemplateFields` |
| `context_fit` end to end | `tests/integration/test_text_style_application.py`, `TestContextFit` |
| The text dry-run `resolved` body and what growth costs | `tests/integration/test_text_style_application.py`, `TestTextDryRunResolvedRequest` |
| A text style with neither declaration stays compatible | `tests/integration/test_text_style_application.py`, `TestTextStyleApplicationCompatibility` |
| The text brace rule, the protected pattern and the forms it does not cover | `tests/unit/test_style_application.py`, `TestTextPromptFormatting` |
| Which text templates a style may be written with, and how a stored field that is more than a bare name is sent | `tests/unit/test_style_application.py`, `TestTextTemplateFieldValidation`, `TestTextPromptFormatting`; `tests/integration/test_text_styles.py`, `TestTextStyleWriteReturnCodes` |
| An instruct placeholder reaching a live text request unchanged | `tests/integration/test_text_style_application.py`, `TestTextStyleInstructPlaceholders` |
| The surcharge on someone else's style, and no surcharge on your own | `tests/integration/test_text_style_application.py`, `TestTextStyleQuote` |
| Only a queued request credits the style's author, and a dry run or a refused request does not credit the author | `tests/integration/test_text_style_application.py`, `TestTextStyleAuthorCredit`; `tests/integration/test_image_style_application.py`, `TestImageStyleAuthorCredit` |
| A request under a collection runs under one of its styles and counts a use of the collection | `tests/integration/test_text_style_application.py`, `TestTextStyleCollection`; `tests/integration/test_image_style_application.py`, `TestImageStyleCollection` |
| A collection draw picks only a style whose declared fields fit the request, and a request whose fields do not fit any style is refused | `tests/unit/test_style_application.py`, `TestTemplateFieldsFit`; `tests/integration/test_text_style_application.py`, `TestTextStyleCollectionTemplateFields` |
| A surcharge forgiven by the floor is not credited, in either mode, and the owner keeps the collected amount after a cancel | `tests/integration/test_text_style_application.py`, `TestTextStyleAuthorCredit`, `TestTextStyleAuthorCreditInShadowMode`; `tests/unit/test_kudos_ledger.py`, `TestStyleRewardAttribution` |
| The published contract matches the vocabulary used for endpoint validation and the write limits on the style routes | `tests/integration/test_style_contract_endpoint.py` |
| A policy applied to a live image request, including the size fallback | `tests/integration/test_image_style_application.py`, `TestImageStyleParameterPolicy` |
| Placeholders applied to a live image request | `tests/integration/test_image_style_application.py`, `TestImageStyleTemplateFields` |
| The image dry-run `resolved` body and compatibility | `tests/integration/test_image_style_application.py`, `TestImageDryRunResolvedRequest` |
| The quote cache key distinguishes styled requests and is stable across validation | `tests/unit/test_dry_run_kudos_quote.py`, `TestQuoteCacheKey`, `TestTextQuoteCacheKey` |
| `shared_key` served to the owner only, and never on the list | `tests/integration/test_text_styles.py`, `TestTextStyleSharedKeyVisibility`; `tests/integration/test_image_styles.py`, `TestImageStyleSharedKeyVisibility` |
| A styled request is logged with its owner, requester and client agent, and counted with bounded labels | `tests/integration/test_text_style_application.py`, `TestStyleApplicationTelemetry` |
| Context fit outcomes are counted | `tests/integration/test_text_style_application.py`, `TestContextFitTelemetry` |
| Style writes are logged | `tests/integration/test_text_styles.py`, `TestTextStyleWriteLogs` |
| Author credits and forgiven surcharges are counted | `tests/unit/test_kudos_ledger.py`, `TestStyleRewardOutcomeCounters` |
| A queued request records its style, and its generation statistic copies it | `tests/integration/test_text_style_application.py`, `TestTextStyleAttribution`; `tests/integration/test_image_style_application.py`, `TestImageStyleAttribution`; `tests/unit/test_text_genstats.py`, `TestStatisticStyle` |
| API refusals are counted by return code and route | `tests/integration/test_malformed_requests.py`, `test_a_refusal_is_counted_by_return_code_and_route` |
| A write to a style is served by its single-style reads straight away | `tests/integration/test_text_styles.py`, `TestTextStyleReadCache`; `tests/integration/test_image_styles.py`, `TestImageStyleReadCache` |

## Schema

`sql_statements/5.1.13.txt` adds `styles.parameter_policy` and `styles.template_fields`, both nullable
`JSONB` without a default, along with column comments. It also adds a nullable `style_id` UUID to
`waiting_prompts`, `image_gen_stats` and `text_gen_stats`. A queued request records the style that it
runs under. When the request specified a collection, this is the style drawn from it. The generation
statistic copies the style on submit, so style use can be grouped per style. The column does not have a
foreign key, so deleting a style leaves its statistics in place. The column is not indexed.

Files at the `sql_statements/` level are run by hand with psql rather than by the application. Run this
file in autocommit with `-v ON_ERROR_STOP=1`. Adding a nullable column without a default is a catalogue-only change
in PostgreSQL, so the migration takes only a brief lock on each table and can be applied before the code
that reads the columns is deployed.
