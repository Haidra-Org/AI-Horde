---
title: "Style contract reference"
summary: "What an image or text style may declare about the params a request may set and the placeholders its prompt accepts, and how a text request is sized against the prompt it ends up sending."
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

A style carries a prompt template, a params body and a model list, and a request that uses one
generates under all three. A style of either type may also declare two things: a `parameter_policy`,
setting out which params a request may still choose and the limits it may not exceed, and
`template_fields`, declaring the placeholders its prompt accepts beyond `{p}`. A text request can
separately use `context_fit` to have its `max_context_length` sized against the prompt the style
produces, and a dry run of either type returns the request as the horde resolved it. Both style
declarations are nullable, and a style declaring neither behaves exactly as every style did before
they existed.

## Applying a style to a request

`GenerateTemplate.apply_style` resolves the style by UUID or name and adopts its shared key when it
carries a valid one. Each gentype then rejects a style of the other type (`StyleMismatch`), picks one
member at random when the style is a `StyleCollection`, and:

- replaces the request's models with the style's;
- fills the style's prompt template, which must contain `{p}` for the request's own prompt, and for an
  image style `{np}` for its negative prompt;
- merges the request's params into the style's under the style's `parameter_policy`;
- replaces the request's `nsfw` flag with the style's;
- increments the style's use count.

The style's params are the starting point and the request's params are discarded, with one exception:
`n`, the number of takes requested, always comes from the request and defaults to 1. `n` is listed in
`NON_OVERRIDABLE_PARAMETER_NAMES`, so a policy cannot cover it either.

The two gentypes differ in how the template is filled, covered under [prompt template
rules](#prompt-template-rules) below. An image request's prompt is split at `###` into the positive
and negative halves before formatting, and a template carrying `{np}` without a `###` of its own gets
one in front of the negative half.

Using someone else's style credits its owner with 2 kudos (`User.record_style`) and adds the same 2
to the request's quote, through the `kudos_adjustment` argument that `GenerateTemplate` passes to
`WaitingPrompt.activate` and to `extrapolate_dry_run_kudos`. A request running under its own author's
style is neither charged the surcharge nor credits the author.

The comparison needs both the style and the requesting user, and `apply_style` runs before the user is
resolved, so it is made separately in `credit_style_owner` on each gentype, after the shared
`super().validate()`. The two rows are compared by id, since the shared validation resolves the user
inside an app context of its own.

A style may carry a shared key. When it does and the key is valid, `GenerateTemplate.apply_style`
adopts it as the request's key, so the style's owner pays. The per-job limits on that key are then not
enforced against the request, since its owner chose to attach the key to the style;
`TextAsyncGenerate.initiate_waiting_prompt` skips the check only when the key in use is the style's own.

`apply_style` runs inside each gentype's `validate`, before the shared `super().validate()` that
resolves the user. For a text request `apply_context_fit` runs immediately after it.

## Prompt template rules

Both types fill their template with `str.format_map` over a `defaultdict(str)`, so `{p}` and the
placeholders the style declares are format specifiers and a placeholder nothing fills becomes the
empty string. What differs is how much of the rest of the template is left alone.

| | Text | Image |
| --- | --- | --- |
| Filled by the horde | `{p}` | `{p}`, `{np}` |
| Declared placeholders filled | Yes | Yes |
| A brace outside those | A format specifier: `{{` and `}}` are one literal brace each, and `{anything}` is a placeholder that empties when nothing fills it | Literal: every brace is doubled before formatting and only the filled placeholders are put back |
| Protected patterns | `\{\{\[[A-Z_]+\]\}\}` | None |

A text template is formatted directly, so a template written for a Python format string keeps working:
`{{` is one literal brace. An image template has every brace doubled first and only `{p}`, `{np}` and
its declared placeholders put back, so a wildcard or a workflow string in braces reaches the worker as
written.

### The protected pattern

`{{[NAME]}}`, with uppercase letters and underscores between the brackets, is an instruct placeholder
a text backend such as koboldcpp fills in when it builds its own prompt. Formatting a text template
would turn `{{[INPUT]}}` into `{[INPUT]}` and strip it out of a template written against that backend.
`format_text_style_prompt` therefore replaces each match of `PROTECTED_TEXT_PLACEHOLDER_PATTERN`
(`\{\{\[[A-Z_]+\]\}\}`) with a marker before formatting and puts the original text back after it, so
the token reaches the worker exactly as written.

The marker carries a random part drawn per call, so a value supplied through `template_fields` cannot
be written to land on one and be turned into a placeholder on the way out.

The pattern is exactly as wide as the regular expression. A lowercase name (`{{[input]}}`) or a name
with anything but uppercase letters and underscores in it (`{{[IN PUT]}}`) is ordinary template text
and formats under the usual rule, collapsing to one brace each side. A doubled brace anywhere else in
the template still becomes one brace.

An image template protects nothing, because it needs nothing: every brace outside a filled placeholder
is already literal.

The rule is stated in three places that have to move together: `format_text_style_prompt`, the
published contract document, and this page. Changing it means raising the document's
`SCHEMA_VERSION` and rewriting all three. [ADR
17](../decisions/0017-protect-koboldcpp-placeholders-and-publish-the-style-contract.md) records why
the pattern is protected rather than the text rule being replaced with the image one.

## `parameter_policy`

A policy is a JSON object on the style row with three optional keys. `StyleParameterPolicy` in
`horde/classes/base/style_contract.py` defines it once, for the endpoints that accept it and the
request path that applies it.

| Key | Meaning |
| --- | --- |
| `override` | `none`, `listed` or `all`. Defaults to `none`. |
| `overridable` | The params a request may set. Read only under `listed`. |
| `ceilings` | The largest value a request may set, per param. |

The three modes:

- `none`: the style's params are used and the request's params body is ignored, with no error. A style
  with no policy at all behaves the same way.
- `listed`: only the params listed in `overridable` may come from the request. Any other param the
  request sets is ignored, as under `none`, with no error. Clients commonly send every param that has
  a default, so refusing unlisted ones would refuse nearly every request.
- `all`: every param may come from the request, except `n`, which comes from it regardless.

A policy is validated against the params model of its own style type rather than against a list
restated here. `text_style_contract_vocabulary` in `horde/apis/v2/kobold_styles.py` and
`image_style_contract_vocabulary` in `horde/apis/v2/stable_styles.py` build that vocabulary and reach
the pydantic models through the validation context, so a stored policy can be read back without the
API layer. A text style may cap `max_length`, `max_context_length` and `n`; an image style may cap
`width`, `height`, `steps` and `n`.

Rejections at declaration time, all 400 with the return code shown:

| Declaration | Return code |
| --- | --- |
| `overridable` sent under `none` or `all` | `StylePolicyOverridableMisplaced` |
| `listed` with no list, or an empty one | `BadRequest` |
| An entry the params model does not have, or `n` | `BadRequest` |
| A ceiling on a param this style type cannot cap | `BadRequest` |
| A ceiling on a param the mode does not let the request set | `BadRequest` |
| A ceiling outside the bounds of the param it caps | `BadRequest` |
| An unknown key, an unknown mode, or a malformed value | `BadRequest` |

Repeated entries in `overridable` are collapsed and the given order is kept. An explicit `null` counts
as absence, so it is accepted under any mode.

A ceiling applies to every value the request ends up controlling, and to the request's value only, so
a ceiling is only accepted on a param the mode hands to the request. `n` comes from the request under
every mode and may be capped under every mode. Under `all` any param the style type can cap may carry
a ceiling; under `listed` only the params also in `overridable` may; under `none` only `n` may. Any
other ceiling would never be measured against anything, so it is rejected when the style is written,
with a message naming the param and the mode. A value above a ceiling is rejected with
`StyleParameterAboveCeiling` rather than trimmed, because trimming would change what the request costs
without reporting it. A value exactly at the ceiling is accepted, and the style's own params are never
checked against a ceiling.

An image style that sets neither `width` nor `height` takes both from the request. That fallback
applies only to a style with no policy; once a policy is declared, the policy settles every param.

## `template_fields`

A style declares up to 16 placeholders, each an object of `name`, `description` (1 to 200 characters)
and `required` (default false). A placeholder is 1 to 32 characters of lowercase letters, digits and
underscores starting with a letter, so `str.format_map` can address it. `p` and `np` are filled by the
horde and cannot be declared, and the same placeholder may not be declared twice.

A request supplies values as a flat object of strings keyed by placeholder. Each value, and all of
them together, are limited to the 7500 words a prompt may be (`MAX_TEMPLATE_FIELD_WORDS`, the same
limit `ParamValidator` places on a prompt).

| Request | Return code |
| --- | --- |
| `template_fields` sent without a `style` | `TemplateFieldsRequireStyle` |
| A placeholder the style does not declare | `TemplateFieldUnknown` |
| A required placeholder left out | `TemplateFieldMissing` |
| A value that is not a string, or an object that is not a mapping | `BadRequest` |
| One value, or all of them together, longer than a prompt may be | `InvalidPromptSize` |

Both gentypes format with `str.format_map` over a `defaultdict(str)`, so a placeholder in the style's
prompt that nothing fills formats to the empty string instead of failing the request. A declared
placeholder need not appear in the prompt either: a prompt can be rewritten without its declarations
being rewritten alongside.

## `context_fit`

`context_fit` is a text request setting; an image request has no equivalent. A text request estimates
its prompt at `ceil(len(prompt) / 3)` tokens (`CHARS_PER_TOKEN_ESTIMATE`), after any style templating.
A prompt fits when `estimated_prompt_tokens + max_length <= max_context_length`, so the tokens to be
generated count against the context alongside the prompt. `max_length` defaults to 80 and
`max_context_length` to 2048, mirroring `TextWaitingPrompt`.

| `context_fit` | Behaviour when the prompt does not fit |
| --- | --- |
| `ignore` (default) | The request is sent as it is and the worker cuts the front of the prompt away. |
| `reject` | The request is refused with `PromptExceedsContext`, carrying the three figures that did not add up. |
| `grow` | `max_context_length` is raised until the prompt fits, and the request is charged for the larger context. |

`ignore` is the default because it is the behaviour every text request has had: a request accepted
before these fields existed is accepted now, unchanged, and the worker goes on cutting the front of an
over-long prompt away.

Growth rounds up to the smallest power of two that fits, because powers of two are what workers
advertise and what the kudos curve is written against. Growth is limited to the smallest of three
figures (`context_growth_upper_bound`):

- `MAX_CONTEXT_LENGTH_LIMIT`, 1048576, the largest `max_context_length` the params model accepts;
- the style's `max_context_length` ceiling, when one applies;
- the largest `max_context_length` advertised by an online text worker that serves one of the request's
  models (`get_highest_text_worker_max_context_length`), where online is the 300-second check-in window
  the active-worker queries use.

When the next power of two is past the bound but the request still fits underneath it, the bound is
used instead of the power of two. When nothing within the bound fits, the request is refused with
`PromptExceedsContext` quoting the bound. Under `reject` the bound is `MAX_CONTEXT_LENGTH_LIMIT`, since
nothing grows.

## The dry run body

A dry run of `/v2/generate/async` or `/v2/generate/text/async` returns 200 with `kudos` and `resolved`,
the request as the horde turned it into: `prompt` after templating, `params` after the style, its
ceilings and any growth, `models`, and `style` (the applied style's `id` and `name`, or null). A text
request adds `estimated_prompt_tokens` and the `context_fit` that was applied. An image request adds
`negative_prompt`, the half of the prompt after the `###`, or null when the prompt carries no `###`. A
null inside `resolved` is part of the answer, so the nesting does not skip nulls. The queued 202
response marshals through the same model and leaves `resolved` unset, where the endpoint's `skip_none`
drops it.

Dry-run quotes are cached in Redis against a hash of the params, the model list, the extra-source-image
count and whether a style surcharge applied. `GenerateTemplate.dry_run_answerable_from_cache` bypasses
that cache whenever the request specifies a style or supplies template fields, and the text override
bypasses it for a `context_fit` other than `ignore`, since the quote and the resolved body then depend
on the prompt and on the outcome of resolution.

## What a request costs

`TextWaitingPrompt.calculate_kudos` prices a text request from the two sizes this contract changes,
`max_context_length` and `max_length`. The context multiplier is
`1.2 + 2.2 ** log2(max_context_length / 1024)`, clamped to between 0.1 and 30, so each doubling of the
context multiplies the factor by roughly 2.2. The rest is linear in `max_length`: a known model charges
`max_length * parameter_bonus * model_multiplier * context_multiplier / 100`, where `parameter_bonus`
is `(max(model_multiplier, 13) / 13) ** 0.20`; an unknown model charges
`max_length * 0.027 * context_multiplier`; an empty model list is priced as a 13B model. Growing a
context therefore costs more kudos, which is why growth is bounded and why a ceiling rejects rather
than trims.

`ImageWaitingPrompt.calculate_kudos` prices an image request from its resolution, its sampler work and
its post-processing, so the params an image policy may cap (`width`, `height`, `steps`) are the ones
that move the price.

## What the style endpoints serve

Style routes of both types marshal without `skip_none`, so `parameter_policy` and `template_fields` are
always present on a served style, null when the style declares neither. The `params` object keeps
`skip_none`, so it carries only the params the style actually sets; for a text style that includes
`max_length` and `max_context_length`.

The patch parser's arguments have no defaults, so a key a `PATCH` omits leaves the stored value alone.
A key that is present replaces the whole JSON column.

## The published contract

`GET /api/v2/status/style_contract` serves everything above that a client has to know before it can
offer style authoring or send a request under someone else's style. It needs no API key and is exempt
from the rate limiter. `horde/style_contract_document.py` compiles it from the same
`StyleContractVocabulary` objects the style endpoints validate against and the same formatting
constants the request path uses, so the published rule and the enforced one come from one place. The
document is a pure function of the installed code, so it is compiled once per process and handed to
every caller rather than cached between processes.

The body has three keys: `schema_version`, and one section each for `text` and `image`.

| Field | Meaning |
| --- | --- |
| `schema_version` | Which version of the contract is being served. Starts at 1 and is raised whenever what is published changes meaning: a key added, removed or renamed, a rule stated differently, or a rule the horde enforces differently than before. |
| `text`, `image` | What a style of that type may declare, and what becomes of the braces in its prompt. |
| `placeholders` | The placeholders the horde fills itself, written without braces. `["p"]` for text, `["p", "np"]` for image. Neither can be declared in `template_fields`. |
| `declared_fields_are_placeholders` | Whether a placeholder declared in `template_fields` is filled from the request's own `template_fields` object. True for both types. |
| `protected_patterns` | Template text formatting leaves exactly as written, each an object of `pattern` (a regular expression) and `description`. Empty for a type that protects nothing. |
| `brace_handling` | One sentence stating what the braces in this type's prompt template mean. |
| `overridable_parameters` | Every param a policy of this type may put in `overridable`, sorted. Anything outside this list is refused at declaration time. |
| `ceiling_parameters` | The params a policy of this type may cap, each an object of `minimum` and `maximum` giving the range that ceiling may be set to. A null bound means the param has none. |
| `context_fit_modes` | How a request of this type may be sized against its prompt. Null for image, which cannot be sized. |
| `style_write_rate_limits` | Every limit a client writing a style of this type is held to. All of them apply at once, and a whitelisted service address is allowed more. |

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
    "brace_handling": "A doubled brace is one literal brace, '{p}' and the placeholders this style declares are filled in, and a placeholder nothing fills becomes the empty string. The protected patterns below are the exception and are left exactly as written.",
    "overridable_parameters": [
      "dynatemp_exponent", "dynatemp_range", "frmtadsnsp", "frmtrmblln", "frmtrmspch",
      "frmttriminc", "max_context_length", "max_length", "min_p", "rep_pen", "rep_pen_range",
      "rep_pen_slope", "sampler_order", "singleline", "smoothing_factor", "stop_sequence",
      "temperature", "tfs", "top_a", "top_k", "top_p", "typical", "use_default_badwordsids"
    ],
    "ceiling_parameters": {
      "max_context_length": { "minimum": 80, "maximum": 1048576 },
      "max_length": { "minimum": 16, "maximum": 4096 },
      "n": { "minimum": 1, "maximum": 20 }
    },
    "context_fit_modes": [
      "ignore",
      "reject",
      "grow"
    ],
    "style_write_rate_limits": [
      "20/hour",
      "2/second"
    ]
  }
}
```

The `image` section has the same keys with different values: `placeholders` is `["p", "np"]`,
`protected_patterns` is empty, `brace_handling` states that every brace is literal except the filled
placeholders, `context_fit_modes` is null, `style_write_rate_limits` is `["90/minute", "2/second"]`,
`ceiling_parameters` covers `width` and `height` (64 to 3072), `steps` (1 to 500) and `n` (1 to 20),
and `overridable_parameters` is the 34 params of the image payload model, from `cfg_scale` through
`workflow`.

A client pins `schema_version` and re-reads the document when it moves. A version rise can add fields
and can change what an existing field means, so a client that cannot make sense of a version it
receives falls back to its own defaults rather than applying the document.

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Policy and placeholder declarations | `horde/classes/base/style_contract.py` | `StyleParameterPolicy`, `StyleTemplateFields` |
| Declaration validation and storage | `horde/classes/base/style_contract.py` | `parse_parameter_policy`, `parse_template_fields`, `serialize_parameter_policy`, `load_parameter_policy` |
| Param merge and ceilings | `horde/classes/base/style_application.py` | `merge_client_parameters` |
| Placeholder resolution | `horde/classes/base/style_application.py` | `resolve_template_field_values` |
| Text template formatting and the protected pattern | `horde/classes/base/style_application.py` | `format_text_style_prompt`, `PROTECTED_TEXT_PLACEHOLDER_PATTERN` |
| The published contract | `horde/style_contract_document.py` | `compile_style_contract`, `published_style_contract`, `SCHEMA_VERSION` |
| The endpoint that serves it | `horde/apis/v2/styles.py` | `StyleContract.get` |
| The contract's response shape | `horde/apis/models/v2.py` | `response_model_style_contract`, `model_style_contract_type` |
| The style surcharge and the owner comparison | `horde/apis/v2/kobold.py`, `horde/apis/v2/stable.py` | `TextAsyncGenerate.credit_style_owner`, `ImageAsyncGenerate.credit_style_owner` |
| Token estimate and fit rule | `horde/classes/kobold/request_fit.py` | `estimate_prompt_tokens`, `prompt_fits_context` |
| Context sizing and its bound | `horde/classes/kobold/request_fit.py` | `fit_context_length`, `context_growth_upper_bound` |
| Shared merge, resolution and dry-run body | `horde/apis/v2/base.py` | `GenerateTemplate.apply_style_contract`, `GenerateTemplate.get_resolved_request`, `GenerateTemplate.dry_run_answerable_from_cache` |
| Style application on a text request | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.apply_style`, `TextAsyncGenerate.apply_context_fit` |
| Style application on an image request | `horde/apis/v2/stable.py` | `ImageAsyncGenerate.apply_style`, `ImageAsyncGenerate.get_resolved_request` |
| Declaration vocabulary and ceiling bounds | `horde/apis/v2/kobold_styles.py`, `horde/apis/v2/stable_styles.py` | `text_style_contract_vocabulary`, `image_style_contract_vocabulary` |
| Type-specific hooks the shared endpoints call | `horde/apis/v2/styles.py` | `StyleContractArgs.parse_type_specific_args`, `StyleContractArgs.apply_type_specific_args` |
| Shared request and response shapes | `horde/apis/models/v2.py` | `model_style_parameter_policy`, `model_style_template_field`, `response_model_resolved_request` |
| Per-type response shapes | `horde/apis/models/kobold_v2.py`, `horde/apis/models/stable_v2.py` | `response_model_async`, `response_model_resolved_request` |
| Worker context ceiling | `horde/database/functions.py` | `get_highest_text_worker_max_context_length` |
| Text pricing | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.calculate_kudos` |
| Style columns | `horde/classes/base/style.py` | `Style.parameter_policy`, `Style.template_fields` |

## Tests

| Contract | Test |
| --- | --- |
| Override modes, `overridable` placement and ceiling bounds | `tests/unit/test_style_contract.py`, `TestParameterPolicyOverride`, `TestParameterPolicyOverridable`, `TestParameterPolicyCeilings` |
| A ceiling only on a param the mode hands to the request | `tests/unit/test_style_contract.py`, `TestParameterPolicyCeilingMode` |
| Which params each mode accepts from the request | `tests/unit/test_style_contract.py`, `TestParameterPolicyApplication` |
| Placeholder names, descriptions, limit and uniqueness | `tests/unit/test_style_contract.py`, `TestTemplateFields` |
| Declarations are stored and read back unchanged | `tests/unit/test_style_contract.py`, `TestRoundTrip` |
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
| A text style with neither declaration behaves as before | `tests/integration/test_text_style_application.py`, `TestTextStyleApplicationCompatibility` |
| The text brace rule, the protected pattern and the forms it does not cover | `tests/unit/test_style_application.py`, `TestTextPromptFormatting` |
| An instruct placeholder reaching a live text request unchanged | `tests/integration/test_text_style_application.py`, `TestTextStyleInstructPlaceholders` |
| The surcharge on someone else's style, and none on your own | `tests/integration/test_text_style_application.py`, `TestTextStyleQuote` |
| The published contract matches the vocabulary the endpoints validate against | `tests/integration/test_style_contract_endpoint.py` |
| A policy applied to a live image request, including the size fallback | `tests/integration/test_image_style_application.py`, `TestImageStyleParameterPolicy` |
| Placeholders applied to a live image request | `tests/integration/test_image_style_application.py`, `TestImageStyleTemplateFields` |
| The image dry-run `resolved` body and compatibility | `tests/integration/test_image_style_application.py`, `TestImageDryRunResolvedRequest` |

## Sharp edges

- A style does not keep the order of its models. `Style.parse_models` returns a `set` after trimming
  the list to five, and `set_models` writes the rows from that set, so the order a style is created
  with is not the order `get_model_names` reads back. A request that uses the style has its own model
  list replaced by that unordered one, and text pricing is based on whichever model is first, so the
  quote for a multi-model text style depends on an order the author did not choose.
- Style writes are rate limited per address, not per account. A text style write is held to 20 an hour
  and 2 a second, an image style write to 90 a minute and 2 a second; both limits are keyed on the
  request path and the address. A client creating several styles in a loop hits the per-second limit
  first, and the published `style_write_rate_limits` is what it should pace against.
- The token estimate runs no tokenizer. `ceil(len(prompt) / 3)` is a conservative character count, so
  `reject` can refuse a prompt a real tokenizer would have fitted, and `grow` can buy context a request
  does not need. Per-model tokenization would make the estimate exact.
- Growth with no online worker for the request's models is limited only by the params model. When
  `get_highest_text_worker_max_context_length` finds no online worker serving those models it returns
  null and contributes no limit, so growth can reach 1048576 for a request nothing can serve. The
  response already warns that no worker can fulfil such a request.
- A patch replaces a JSON column whole. Sending `parameter_policy` or `template_fields` on a `PATCH`
  overwrites the stored value; there is no merge, and no way to clear one back to null through the
  endpoint.

## Schema

`sql_statements/5.1.13.txt` adds `styles.parameter_policy` and `styles.template_fields`, both nullable
`JSONB` with no default, along with column comments. Files at the `sql_statements/` level are run by
hand with psql rather than by the application; run it in autocommit with `-v ON_ERROR_STOP=1`. Adding a
nullable column with no default is a catalogue-only change in PostgreSQL, so the migration takes only a
brief lock and can be applied before the code that reads the columns is deployed.
