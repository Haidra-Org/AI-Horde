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

`GenerateTemplate.apply_style` resolves the style by UUID or name. When that is a `StyleCollection`,
`GenerateTemplate.draw_collection_style` draws one of its styles at random and counts a use of the
collection, and the request runs under the drawn style from there on. The draw is only among the styles
that declare every field the request supplies and require no field it leaves out
(`template_fields_fit`). A collection's styles can declare different fields, and drawing from all of
them would make the same request succeed or fail by chance. A style whose stored declaration no longer
validates is left out of the draw. When no style fits, the request is refused with
`TemplateFieldsMatchNoCollectionStyle`. A collection carries no prompt, params or shared key, so
`apply_style` then adopts the drawn style's shared key when it carries a valid one. Each gentype rejects a style of the other type (`StyleMismatch`), and:

- replaces the request's models with the style's;
- fills the style's prompt template, which must contain `{p}` for the request's prompt, and for an
  image style `{np}` for its negative prompt;
- merges the request's params into the style's under the style's `parameter_policy`;
- replaces the request's `nsfw` flag with the style's;
- increments the style's use count.

The style's params are the starting point and the request's params are discarded, with one exception:
`n`, the number of takes requested, always comes from the request and defaults to 1. `n` is listed in
`NON_OVERRIDABLE_PARAMETER_NAMES`, so a policy cannot cover it either.

The two gentypes differ in how the template is filled, covered under [prompt template
rules](#prompt-template-rules) below. An image request's prompt is split at `###` into the positive
and negative halves before formatting, and a template carrying `{np}` but no `###` gets one in front
of the negative half.

Using someone else's style adds `STYLE_OWNER_REWARD` (2 kudos) to the request's quote, through the
`kudos_adjustment` argument that `GenerateTemplate` passes to `WaitingPrompt.activate` and to
`extrapolate_dry_run_kudos`, and credits the style's owner the part of that amount the requester's
debit collected (`User.record_style`). A request under a style its requester authored is neither
charged the surcharge nor credits the author.

The comparison needs both the style and the requesting user, and `apply_style` runs before the user is
resolved, so `GenerateTemplate.decide_style_surcharge` makes it after the shared `super().validate()`
on each gentype. The two rows are compared by id, since the shared validation resolves the user inside
a separate app context.

The request path does not credit the owner itself. `GenerateTemplate.style_reward` builds a
`StyleReward` (the author's id, the surcharge and the style type), which `WaitingPrompt.activate`
carries onto the requester's activation debit. The minimum-balance floor can forgive part or all of
that debit, as it does for the anonymous user and for any account at its minimum, so the owner is
credited only the surcharge minus whatever part of it the floor forgave, with the forgiven amount
taken from the surcharge first. In ledger mode the floor is applied when the kudos applier folds the
debit, so the applier credits the owner from the debit's detail. In shadow mode the floor is applied
inline, and `project_style_reward` credits the owner during activation. A dry run quotes the surcharge
but credits nothing, and neither does a request refused at any point before activation.

The surcharge is not refunded when the request is cancelled, as the base horde tax is not, so the
owner keeps whatever was collected.

A style may carry a shared key. When it does and the key is valid, `GenerateTemplate.apply_style`
adopts it as the request's key, so the style's owner pays. The per-job limits on that key are then not
enforced against the request, since its owner chose to attach the key to the style;
`TextAsyncGenerate.initiate_waiting_prompt` skips the check only when the key in use is the style's own.

`apply_style` runs inside each gentype's `validate`, before the shared `super().validate()` that
resolves the user. For a text request `apply_context_fit` runs immediately after it.

## Prompt template rules

Both types fill `{p}` and the placeholders the style declares, and a placeholder nothing fills becomes
the empty string. What differs is how much of the rest of the template is left alone.

| | Text | Image |
| --- | --- | --- |
| Filled by the horde | `{p}` | `{p}`, `{np}` |
| Declared placeholders filled | Yes | Yes |
| A brace outside those | Python format string grammar, in which `{{` and `}}` are one literal brace each and any other bare `{name}` is a placeholder that empties when nothing fills it | Literal. Every brace is doubled before formatting and only the filled placeholders are put back |
| A field that is more than a bare name | Refused when the style is written (`StylePromptFieldInvalid`). One already stored is sent as written | Literal, like any other brace |
| Protected patterns | `\{\{\[[A-Z_]+\]\}\}` | None |

A text template is parsed with `string.Formatter`, so a template written for a Python format string
keeps working: `{{` is one literal brace. Only a bare name is filled. A format spec (`{p:>20000}`), a
conversion (`{p!r}`), attribute or index access (`{p.__class__}`, `{p[0]}`) and a positional field
(`{}`, `{0}`) have no use in a prompt, and a format spec would let a template make every request build
an arbitrarily large string. `validate_text_template_fields` refuses a style written with one, or with
a lone brace that does not parse, with `StylePromptFieldInvalid`. A template stored before that check
has such a field sent to the worker exactly as written, and one whose braces do not parse is refused
at request time with `StyleDeclarationInvalid`. Patching any field of a style revalidates its stored
prompt, so a style carrying such a field has to have its prompt corrected on its next edit. An image
template has every brace doubled first and only `{p}`, `{np}` and
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
restated here. `text_style_contract_vocabulary` in `horde/apis/v2/kobold.py` and
`image_style_contract_vocabulary` in `horde/apis/v2/stable.py` build that vocabulary next to the params
model it reads, and reach the pydantic models through the validation context, so
`horde/classes/base/style_contract.py` does not import the API layer. The style endpoints and the
request path validate against the same vocabulary. A text style may cap `max_length`,
`max_context_length` and `n`; an image style may cap `width`, `height`, `steps` and `n`.

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

The request path validates the stored declarations again each time the style is applied, against the
same vocabulary the style endpoints use. A stored declaration can stop validating when the rules or the
params model change after it was written, and the request is then refused with a 400.

Rejections when a request is sent under a style, all 400 with the return code shown:

| Request | Return code |
| --- | --- |
| A value above the ceiling the style puts on it | `StyleParameterAboveCeiling` |
| The style's stored `parameter_policy` or `template_fields` no longer validates | `StyleDeclarationInvalid` |

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

When no online text worker serves any of the request's models, a `grow` request does not grow: the
bound is the `max_context_length` the request set, so a prompt that does not fit it is refused with
`PromptExceedsContext` quoting that size. A prompt that already fits is accepted as it is.

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
count and whether the request names a style (`get_hashed_params_dict`). The hash is computed twice, at
the cache lookup before validation and at the store after it, so its keys read only what the request
sent. The `styled` key reads the `style` argument, since the surcharge
flag is only set during validation, and the text and image overrides read the model list from the
request arguments, since applying a style replaces the resolved models.
`GenerateTemplate.dry_run_answerable_from_cache` bypasses that cache whenever the request specifies a
style or supplies template fields, and the text override bypasses it for a `context_fit` other than
`ignore`, since the quote and the resolved body then depend on the prompt and on the outcome of
resolution.

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

`shared_key` is served only to the style's owner, and only on the single-style routes:
`/v2/styles/text/{style_id}`, `/v2/styles/text_by_name/{style_name}`, `/v2/styles/image/{style_id}` and
`/v2/styles/image_by_name/{style_name}`. These take an optional `apikey` header. When the key resolves
to the style's owner, `shared_key` carries the shared key's details; for every other caller it is null.
A request carrying a key other than the anonymous one bypasses the 30-second response cache those
routes share across
callers, and its response carries `Cache-Control: private, no-store`. The list routes,
`/v2/styles/text` and `/v2/styles/image`, serve `shared_key` as null to every caller, the owner
included. `Style.get_details(include_shared_key=...)` is the switch, and it defaults to off.

A style's PATCH or DELETE, and an image style's example writes, clear the cached responses of its
read by id, by name, and by the name qualified with its owner's alias (`style_read_cache_keys` and
`clear_cached_responses` in `horde/apis/v2/styles.py`), including the name a rename replaced. Any other
spelling of the name, and the list and collection routes, which are cached per query string, can serve
the earlier version for up to 30 seconds.

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
| `brace_handling` | A short statement of what the braces in this type's prompt template mean. |
| `overridable_parameters` | Every param a policy of this type may put in `overridable`, sorted. Anything outside this list is refused at declaration time. |
| `ceiling_parameters` | The params a policy of this type may cap, each an object of `minimum` and `maximum` giving the range that ceiling may be set to. A null bound means the param has none. |
| `context_fit_modes` | How a request of this type may be sized against its prompt. Null for image, which cannot be sized. |
| `style_create_rate_limits` | Every limit a client creating a style of this type is held to. All of them apply at once. A whitelisted service address is allowed more per second, but the hourly limit is the same for every address. |
| `style_modify_rate_limits` | Every limit a client patching or deleting a style of this type is held to. All of them apply at once, counted separately for each style and for PATCH and DELETE, and a whitelisted service address is allowed more. |

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
      "max_length": { "minimum": 16, "maximum": 4096 },
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
placeholders, `context_fit_modes` is null, `ceiling_parameters` covers `width` and `height` (64 to 3072), `steps` (1 to 500) and `n` (1 to 20),
and `overridable_parameters` is the 34 params of the image payload model, from `cfg_scale` through
`workflow`.

A client pins `schema_version` and re-reads the document when it moves. A version rise can add fields
and can change what an existing field means, so a client that cannot make sense of a version it
receives falls back to its own defaults rather than applying the document.

## Logs and metrics

A request under a style, a style or collection write, and a stored declaration that stops validating each
leave a log line. Volume and outcomes are also counted in metrics whose labels stay bounded. The style,
its owner and the requester go only to log lines and span attributes.

### Log lines

| Line starts with | Level | Carries |
| --- | --- | --- |
| `Style applied:` | info for a queued request, debug for a dry run | Style id, name and type, owner, requester, client agent, collection id or `none`, policy mode or `undeclared`, template fields supplied and declared, `literal_template_fields` (fields of a stored text template sent as written), whether the surcharge applied, dry run |
| `Style created:`, `Style patched:`, `Style deleted:` | info | Style id, name, type, policy mode, declared field count, visibility, and the account. A patch adds the fields it carried. |
| `Collection created:`, `Collection patched:`, `Collection deleted:` | info | Collection id, name, type, style count, visibility, and the account |
| `Style example added:`, `Style example patched:`, `Style example deleted:` | info | Style id, example id, the account, and whether the example is primary |
| `Stored style declaration invalid:` | warning | The style and the declaration that no longer validates, with the reasons |
| `Style ... left out of a collection draw` | info | A collection's style skipped because its stored template fields no longer validate |
| `Context grown from` | debug | The requested and the grown context of a text request. The counter below carries the volume |

A dry run is logged at debug because a client previews a styled request repeatedly while its inputs
change. The counter still counts it.

### Metrics

| Metric | Unit | Labels |
| --- | --- | --- |
| `horde.style.applications` | 1 | `horde.gentype`, `horde.style.source` (`style` or `collection`), `horde.style.policy` (`none`, `listed`, `all` or `undeclared`), `horde.style.template_fields` (`declared` or `none`), `horde.style.surcharged`, `horde.dry_run` |
| `horde.style.author_credits` | kudos | `horde.style.type` |
| `horde.style.forgiven_surcharges` | kudos | `horde.style.type`. Counts the part of a surcharge the minimum-balance floor forgave, which is not credited |
| `horde.text.context_fit` | 1 | `horde.context_fit.mode`, `horde.context_fit.outcome` (`fits`, `sent_overlong`, `grown`, `refused`) |
| `horde.api.rejections` | 1 | `horde.rc`, `http.response.status_code`, `http.route`, `http.request.method`. Counts every API refusal, styles included |

`horde.api.rejections` labels a return code missing from `KNOWN_RC` as `Unlisted`, and a refusal raised
outside a matched route as `unmatched`, so neither label grows with input.

### Span attributes

The span that validates a styled generate request carries `horde.style.id`, `horde.style.owner_id`,
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
| Crediting the owner what the surcharge collected | `horde/database/kudos_ledger.py`, `horde/database/kudos_legacy_projection.py`, `horde/classes/base/kudos.py` | `_credit_style_authors`, `project_style_reward`, `StyleReward.collected` |
| Token estimate and fit rule | `horde/classes/kobold/request_fit.py` | `estimate_prompt_tokens`, `prompt_fits_context` |
| Context sizing and its bound | `horde/classes/kobold/request_fit.py` | `fit_context_length`, `context_growth_upper_bound` |
| Shared merge, resolution and dry-run body | `horde/apis/v2/base.py` | `GenerateTemplate.apply_style_contract`, `GenerateTemplate.get_resolved_request`, `GenerateTemplate.dry_run_answerable_from_cache` |
| Vocabulary hook for the request path | `horde/apis/v2/base.py`, `horde/apis/v2/kobold.py`, `horde/apis/v2/stable.py` | `GenerateTemplate.style_contract_vocabulary`, `TextAsyncGenerate.style_contract_vocabulary`, `ImageAsyncGenerate.style_contract_vocabulary` |
| Style application on a text request | `horde/apis/v2/kobold.py` | `TextAsyncGenerate.apply_style`, `TextAsyncGenerate.apply_context_fit` |
| Style application on an image request | `horde/apis/v2/stable.py` | `ImageAsyncGenerate.apply_style`, `ImageAsyncGenerate.get_resolved_request` |
| Declaration vocabulary and ceiling bounds | `horde/apis/v2/kobold.py`, `horde/apis/v2/stable.py` | `text_style_contract_vocabulary`, `image_style_contract_vocabulary`, `TEXT_CEILING_PARAMETER_NAMES`, `IMAGE_CEILING_PARAMETER_NAMES` |
| Type-specific hooks the shared endpoints call | `horde/apis/v2/styles.py` | `StyleContractArgs.style_contract_vocabulary`, `StyleContractArgs.parse_type_specific_args`, `StyleContractArgs.apply_type_specific_args` |
| Shared request and response shapes | `horde/apis/models/v2.py` | `model_style_parameter_policy`, `model_style_template_field`, `response_model_resolved_request` |
| Per-type response shapes | `horde/apis/models/kobold_v2.py`, `horde/apis/models/stable_v2.py` | `response_model_async`, `response_model_resolved_request` |
| Worker context ceiling | `horde/database/functions.py` | `get_highest_text_worker_max_context_length` |
| Text pricing | `horde/classes/kobold/waiting_prompt.py` | `TextWaitingPrompt.calculate_kudos` |
| Style columns | `horde/classes/base/style.py` | `Style.parameter_policy`, `Style.template_fields` |
| The style a generation ran under | `horde/classes/base/waiting_prompt.py`, `horde/classes/stable/genstats.py`, `horde/classes/kobold/genstats.py` | `WaitingPrompt.style_id`, `ImageGenerationStatistic.style_id`, `TextGenerationStatistic.style_id` |
| The owner-only `shared_key` on a served style | `horde/apis/v2/styles.py`, `horde/classes/base/style.py` | `SingleStyleTemplateGet.get_existing_style`, `Style.get_details` |
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
| A ceiling only on a param the mode hands to the request | `tests/unit/test_style_contract.py`, `TestParameterPolicyCeilingMode` |
| Which params each mode accepts from the request | `tests/unit/test_style_contract.py`, `TestParameterPolicyApplication` |
| Placeholder names, descriptions, limit and uniqueness | `tests/unit/test_style_contract.py`, `TestTemplateFields` |
| Declarations are stored and read back unchanged | `tests/unit/test_style_contract.py`, `TestRoundTrip` |
| A stored declaration that no longer validates is refused with `StyleDeclarationInvalid` | `tests/unit/test_style_contract.py`, `TestStoredDeclarationValidation`; `tests/integration/test_text_style_application.py`, `TestTextStyleParameterPolicy` |
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
| Which text templates a style may be written with, and how a stored field that is more than a bare name is sent | `tests/unit/test_style_application.py`, `TestTextTemplateFieldValidation`, `TestTextPromptFormatting`; `tests/integration/test_text_styles.py`, `TestTextStyleWriteReturnCodes` |
| An instruct placeholder reaching a live text request unchanged | `tests/integration/test_text_style_application.py`, `TestTextStyleInstructPlaceholders` |
| The surcharge on someone else's style, and none on your own | `tests/integration/test_text_style_application.py`, `TestTextStyleQuote` |
| Only a queued request credits the style's author, and a dry run or a refused request credits nothing | `tests/integration/test_text_style_application.py`, `TestTextStyleAuthorCredit`; `tests/integration/test_image_style_application.py`, `TestImageStyleAuthorCredit` |
| A request under a collection runs under one of its styles and counts a use of the collection | `tests/integration/test_text_style_application.py`, `TestTextStyleCollection`; `tests/integration/test_image_style_application.py`, `TestImageStyleCollection` |
| A collection draws only a style whose declared fields fit the request's, and refuses fields no style fits | `tests/unit/test_style_application.py`, `TestTemplateFieldsFit`; `tests/integration/test_text_style_application.py`, `TestTextStyleCollectionTemplateFields` |
| A surcharge the floor forgives is not credited, in either mode, and a cancel keeps what was collected | `tests/integration/test_text_style_application.py`, `TestTextStyleAuthorCredit`, `TestTextStyleAuthorCreditInShadowMode`; `tests/unit/test_kudos_ledger.py`, `TestStyleRewardAttribution` |
| The published contract matches the vocabulary the endpoints validate against and the write limits their routes enforce | `tests/integration/test_style_contract_endpoint.py` |
| A policy applied to a live image request, including the size fallback | `tests/integration/test_image_style_application.py`, `TestImageStyleParameterPolicy` |
| Placeholders applied to a live image request | `tests/integration/test_image_style_application.py`, `TestImageStyleTemplateFields` |
| The image dry-run `resolved` body and compatibility | `tests/integration/test_image_style_application.py`, `TestImageDryRunResolvedRequest` |
| The quote cache key tells a styled request apart and is stable across validation | `tests/unit/test_dry_run_kudos_quote.py`, `TestQuoteCacheKey`, `TestTextQuoteCacheKey` |
| `shared_key` served to the owner only, and never on the list | `tests/integration/test_text_styles.py`, `TestTextStyleSharedKeyVisibility`; `tests/integration/test_image_styles.py`, `TestImageStyleSharedKeyVisibility` |
| A styled request is logged with its owner, requester and client agent, and counted with bounded labels | `tests/integration/test_text_style_application.py`, `TestStyleApplicationTelemetry` |
| Context fit outcomes are counted | `tests/integration/test_text_style_application.py`, `TestContextFitTelemetry` |
| Style writes are logged | `tests/integration/test_text_styles.py`, `TestTextStyleWriteLogs` |
| Author credits and forgiven surcharges are counted | `tests/unit/test_kudos_ledger.py`, `TestStyleRewardOutcomeCounters` |
| A queued request records its style, and its generation statistic copies it | `tests/integration/test_text_style_application.py`, `TestTextStyleAttribution`; `tests/integration/test_image_style_application.py`, `TestImageStyleAttribution`; `tests/unit/test_text_genstats.py`, `TestStatisticStyle` |
| API refusals are counted by return code and route | `tests/integration/test_malformed_requests.py`, `test_a_refusal_is_counted_by_return_code_and_route` |
| A write to a style is served by its single-style reads straight away | `tests/integration/test_text_styles.py`, `TestTextStyleReadCache`; `tests/integration/test_image_styles.py`, `TestImageStyleReadCache` |

## Sharp edges

- A style does not keep the order of its models. `Style.parse_models` returns a `set` after trimming
  the list to five, and `set_models` writes the rows from that set, so the order a style is created
  with is not the order `get_model_names` reads back. A request that uses the style has its model list
  replaced by that unordered one, and text pricing is based on whichever model is first, so the
  quote for a multi-model text style depends on an order the author did not choose.
- Style write limits are kept per address, with no per-account limit. Creating a style of either type is held
  to 20 an hour and 2 a second, and patching or deleting one to 90 a minute and 2 a second. Every limit
  is keyed on the address, the method and the request path, so the modify limits count each style
  separately. A client creating several styles in a loop hits the per-second limit first, and the
  published `style_create_rate_limits` and `style_modify_rate_limits` are what it should pace against.
  The limits are declared for the write methods only (`STYLE_CREATE_METHODS`, `STYLE_MODIFY_METHODS`), so
  reading styles and collections falls under the app's default limit of 90 a minute per address.
- The token estimate runs no tokenizer. `ceil(len(prompt) / 3)` is a conservative character count, so
  `reject` can refuse a prompt a real tokenizer would have fitted, and `grow` can buy context a request
  does not need. Per-model tokenization would make the estimate exact.
- Growth with no online worker for the request's models is refused. When
  `get_highest_text_worker_max_context_length` finds no online worker serving those models it returns
  null, and `context_growth_upper_bound` then returns the requested `max_context_length`, so a `grow`
  request whose prompt does not fit is rejected with `PromptExceedsContext` at the requested size. The
  same request under `ignore` would be queued and wait for a worker to come online;
  under `grow` the client has to retry once one has checked in within the 300-second window.
- A patch replaces a JSON column whole. Sending `parameter_policy` or `template_fields` on a `PATCH`
  overwrites the stored value; there is no merge, and no way to clear one back to null through the
  endpoint.

## Schema

`sql_statements/5.1.13.txt` adds `styles.parameter_policy` and `styles.template_fields`, both nullable
`JSONB` with no default, along with column comments. It also adds a nullable `style_id` UUID to
`waiting_prompts`, `image_gen_stats` and `text_gen_stats`. A queued request records the style it runs
under, the style drawn from a collection when the request specified one, and the generation statistic
copies it on submit, so style use can be grouped per style. The column has no foreign key, so a deleted
style leaves its statistics in place, and no index.

Files at the `sql_statements/` level are run by hand with psql rather than by the application. Run it in
autocommit with `-v ON_ERROR_STOP=1`. Adding a nullable column with no default is a catalogue-only change
in PostgreSQL, so the migration takes only a brief lock on each table and can be applied before the code
that reads the columns is deployed.
