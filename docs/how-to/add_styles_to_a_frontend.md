---
title: "Add style authoring and styled requests to a frontend"
summary: "Build style authoring controls from the published style contract, build request inputs from a style's declarations, preview with a dry run, and handle collections and style errors."
topics: [generation, requests]
order: 52
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Add style authoring and styled requests to a frontend

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

Use the published style contract to build an editor and request form that follow the server's rules.
Your frontend should already submit text or image requests and have request types generated from
`/api/swagger.json` or maintained separately. The integration adds:

- authoring controls for `parameter_policy` and `template_fields`, built from the published contract
- request inputs built from the declarations of the selected style or collection
- a preview of the resolved request before the user spends kudos
- recovery from style errors by return code

A frontend that already integrates styles should first apply [the update for existing
integrations](update_a_style_integration.md). The [style contract
reference](../reference/style_contract.md) holds the rules, and [using the style
endpoints](use_style_endpoints.md) shows each endpoint with commands.

The contract at `/api/v2/status/style_contract` supplies the available controls. The selected style's
declarations determine which controls apply to a request. Keep server validation authoritative so a
frontend update is not required for every change to the validation rules.

In this guide, `apiOrigin` is the URL origin without an API path, such as `https://aihorde.net` or
`http://localhost:7001`, and `apiV2BaseUrl` is `${apiOrigin}/api/v2`.

## Fetch the style contract

Fetch the contract once per API origin at startup and cache it per origin:

```http
GET <apiV2BaseUrl>/status/style_contract
Client-Agent: <frontend-name>:<version>:<project-url>
```

The endpoint is anonymous and is not rate limited. The body includes `schema_version` and one section
per style type, `text` and `image`, each with:

| Key | Drives |
| --- | --- |
| `overridable_parameters` | The params that a policy may let the request set |
| `ceiling_parameters` | The params that a policy may cap, each with the allowed `minimum` and `maximum` for a ceiling |
| `placeholders` | The placeholders filled by the horde itself (`p`, and `np` for image), which a style cannot declare |
| `protected_patterns` | Regular expressions for template text that is left exactly as written |
| `brace_handling` | A short statement of what braces in the template mean, suitable as help text |
| `context_fit_modes` | The `context_fit` values accepted on a text request, null for image |
| `style_create_rate_limits`, `style_modify_rate_limits` | The limits on style writes |

Pin the `schema_version` that the frontend was written against. On a higher version, keep the controls
that the frontend already renders, and do not build new controls from the new document until the
frontend is updated. At every level, ignore keys that the frontend does not recognise.

Verification: the cached contract has `schema_version` and both sections, and a second origin
configured in the frontend gets a separate entry.

## Build style authoring controls

Create a style with `POST /api/v2/styles/<type>` and update it with `PATCH /api/v2/styles/<type>/<style-id>`.
Only trusted users and users with the customizer role can create styles. Other users,
including the anonymous user, get `StylesRequiresCustomizer`. The anonymous user gets
`StylesAnonForbidden` when it creates a collection or deletes a style.

### Parameter policy

`parameter_policy` has three parts:

- `override` is `none` (the request's params are ignored), `listed` (only the params in `overridable`
  come from the request) or `all` (every request param does). `n` always comes from the request.
- `overridable` is shown only under `listed`, as a choice from `overridable_parameters`. Sending it
  under another mode is refused with `StylePolicyOverridableMisplaced`, and `n` cannot be listed.
- `ceilings` maps a param to the largest value that a request may set. Offer a ceiling only for a param
  in `ceiling_parameters` that the selected mode lets the request set, or for `n` under every mode,
  since the server refuses a ceiling that the mode would never check. Bound each ceiling input by the
  param's published `minimum` and `maximum`.

### Template fields

Use the [template limits](../reference/service_limits.md#images-and-styles) when validating field declarations and
supplied text; they identify the fixed bounds and their enforcement symbols.

`template_fields` is a list of placeholders in the template that are filled from the request, each with
`name`, `description` and `required`. Names are lowercase snake case, and `p` and `np` are reserved.
The reference lists the length and count limits. Check that each declared name appears in the template
as `{name}`, since an unused field is accepted but has no effect.

### Template text

- **Text:** the template is a Python format string. `{{` and `}}` are literal braces, and a
  placeholder is a bare name with no format spec, conversion, attribute or index access. Text matching
  a `protected_patterns` expression, such as `{{[INPUT]}}`, is sent as written, so match those
  expressions before formatting a preview. Show `brace_handling` as the help text.
- **Image:** every brace other than `{p}`, `{np}` and declared fields is literal, so wildcards and
  workflow strings pass through. `{np}` is required, and `###` separates the positive and negative
  halves.

### Saving

Send only changed top-level fields in a `PATCH` request. If you include `parameter_policy` or
`template_fields`, send the complete declaration, including unchanged settings. The server replaces
that declaration rather than merging its contents. See [Partial PATCH](../reference/style_contract.md#partial-patch)
for the replacement and validation rules.

Use `style_create_rate_limits` and `style_modify_rate_limits` to limit how often the editor saves.
After a 429 response, show the user how long to wait before retrying. The limits are listed under [Write rate
limits](../reference/style_contract.md#write-rate-limits).

Verification: save a style, read it back from `/api/v2/styles/<type>/<style-id>`, and confirm the
editor reloads the same `parameter_policy`, `template_fields` and `prompt`.

## Build request inputs from a style

Read the selected style from `/api/v2/styles/<type>/<style-id>` and derive the request form from it:

| Style declares | Request form shows |
| --- | --- |
| No `parameter_policy` | Only `n`, since the style's params replace the rest. An image style without `width` or `height` also takes those from the request. |
| `override: none` | Only `n` |
| `override: listed` | `n` and the params in `overridable` |
| `override: all` | Every param |
| `ceilings` | Each listed param's input bounded by its ceiling |
| `template_fields` | One input per field, marked required where `required` is true, with `description` as help text |

A text request also offers `context_fit`, with the values in `context_fit_modes`. Under `ignore` an
over-long prompt is sent as it is, under `reject` it is refused, and under `grow` the context is raised
to fit and charged for.

Send the values as `params` and `template_fields` on the generate request with `style` set to the
style's id. A param above a ceiling is refused with `StyleParameterAboveCeiling` instead of being
lowered, so clamp the input to the ceiling before sending. Clients typically serialize every param
that has a default. Under `listed`, the unlisted ones are ignored without an error.

Verification: a dry run of the form's request returns 200, and `resolved.params` holds the form's
values for the params that the policy lets through.

## Preview with a dry run

Send the request with `dry_run: true` to show the user what will run and what it costs:

```json
{
  "prompt": "<prompt>",
  "style": "<style-id>",
  "dry_run": true,
  "params": {"max_length": 300, "n": 1},
  "template_fields": {"tone": "plain"}
}
```

The response includes a `kudos` quote and a `resolved` object describing the request after the style
has been applied. It includes the completed prompt, effective parameters, models and selected style.
A text request adds `estimated_prompt_tokens`
and the applied `context_fit`, and an image request adds `negative_prompt`. Under someone else's
style the quote includes a 2-kudo surcharge for the style's author.

A styled dry run is computed fresh each time and counts against the generate rate limit, so trigger a
preview from an explicit action or once the user stops changing the inputs.

Verification: `resolved.prompt` shows each template field's value in place, and a request under a
style authored by the user gets the same quote as the unstyled request with the style's settings.

## Offer collections

A collection groups styles of one type. A request with a collection's id in `style` draws one of its
styles at random and runs under that style's declarations. The draw is only among the styles that
declare every supplied field and do not require an omitted field.

`/api/v2/collections/<collection-id>` lists the collection's style ids. To build the request form:

1. Read each listed style's details.
2. Offer the union of their template fields, each marked required only where every style requires it.
3. Before sending, check that at least one style declares every supplied field and does not require an
   omitted field. If every style fails this check, the server refuses the request with
   `TemplateFieldsMatchNoCollectionStyle`.
4. Show `resolved.style` from the dry run, since the style drawn can differ between requests.

A collection mixing text and image styles is refused with `StyleMismatch` on create and on `PATCH`.

Verification: take a collection of one style that requires a field and one that does not declare any
fields. A dry run without the field resolves to the second style every time, and a dry run with the
field resolves to the first.

## Show a style's shared key to its owner

A style can include a shared key used by requests under the style. In the owner's editor, read the
style from `/api/v2/styles/<type>/<style-id>` with the owner's key in the `apikey` header, and keep
that response out of shared caches, as required by its `Cache-Control: private, no-store` header.
All listings return `shared_key: null`, as do individual reads without the owner's key. See
[Shared key visibility](../reference/style_contract.md#shared-key-visibility) for the routes and cache rules.

Verification: the owner's editor shows the key, and the same style viewed by another account or
anonymously does not show it.

## Recover from validation errors

API errors include a stable `rc` and display text in `message`. Branch on `rc` and show `message` without
parsing it.

| `rc` | State change before resubmission |
| --- | --- |
| `StylesRequiresCustomizer`, `StylesAnonForbidden` | Hide style authoring for the account. |
| `StylePolicyOverridableMisplaced` | Clear `overridable`, or switch the mode to `listed`. |
| `StylePromptMissingVars` | Add `{p}` to the template, and `{np}` for an image style. |
| `StylePromptFieldInvalid` | Mark the offending field in the template. Literal braces are written `{{` and `}}`. |
| `SharedKeyInvalid`, `SharedKeyEmpty`, `SharedKeyExpired` | Ask the owner to choose or fund another shared key. |
| `StyleMismatch` | Offer only styles or collections of the request's type. |
| `StyleGetMistmatch` | Read the style through the route for its type. |
| `StyleParameterAboveCeiling` | Clamp the input to the style's ceiling. |
| `TemplateFieldMissing` | Require the field in the form. |
| `TemplateFieldUnknown` | Drop the field, which the style does not declare. |
| `TemplateFieldsRequireStyle` | Drop `template_fields` from a request without a style. |
| `TemplateFieldsMatchNoCollectionStyle` | Show which fields each of the collection's styles declares and requires, and let the user change the set. |
| `StyleDeclarationInvalid` | The style's stored declaration does not pass the current validation. Tell the user to pick another style or ask its author to edit it. |
| `PromptExceedsContext` | Shorten the prompt, raise `max_context_length`, or choose `context_fit: grow`. |
| `TooManyStyleExamples`, `ExampleURLAlreadyInUse` | Remove an example, or choose another URL. |

Verify each recovery with one manual resubmission. If it is refused again, keep the error visible with
its `rc` and keep the user's other inputs.

## Check the integration

Before considering the integration complete, verify that:

- The contract is cached per API origin, and new controls are never built from an unsupported
  `schema_version`.
- Authoring choices come from `overridable_parameters` and `ceiling_parameters` as served.
- A ceiling input is offered only where the selected mode lets the request set the param, or for `n`.
- `PATCH` sends only changed fields, and declarations are sent whole.
- Request inputs follow the selected style's policy and template fields.
- Collection inputs come from the union of the collection's styles, and `resolved.style` is shown.
- Server errors are handled by `rc`, never by parsing `message`.
- A shared key is read with the owner's key and never cached across users.

Exercise at least these cases:

| Case | Expected behavior |
| --- | --- |
| Policy `listed` with `max_length` overridable and capped at 512 | The request form offers `max_length` up to 512 and `n` |
| Policy `none` | The request form offers only `n` |
| A required template field omitted | The form blocks sending, since the server would return `TemplateFieldMissing`. A supplied empty string is accepted by the server. |
| A text template containing `{{[INPUT]}}` | The preview shows it unchanged |
| A text template with `{p:>20}` | The editor flags it, since the server returns `StylePromptFieldInvalid` |
| A collection whose styles declare different fields | The form offers the union, and the drawn style is shown |
| Contract fetch fails with a cached copy | The cached contract is used |

## Verify against a local stack

Point the frontend's local configuration at a local API so it does not read the production contract or
submit production work. For this repository's Docker Compose stack, create the ignored `.env_docker`
described in `README_docker.md`, then start the API and its stores:

```console
docker compose up --build -d
curl http://localhost:7001/api/v2/status/style_contract
```

Set `apiOrigin` to `http://localhost:7001`. If the frontend stores a versioned base URL instead, set it
to `http://localhost:7001/api/v2` and do not append `/api/v2` again.
