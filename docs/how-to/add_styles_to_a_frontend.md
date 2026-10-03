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

This procedure assumes the frontend already submits text or image requests and generates API types
from `/api/swagger.json` or maintains equivalent request types. It adds:

- authoring controls for `parameter_policy` and `template_fields`, built from the published contract
- request inputs built from the declarations of the selected style or collection
- a preview of the resolved request before the user spends kudos
- recovery from style errors by return code

A frontend that already integrates styles should first apply [the update for existing
integrations](update_a_style_integration.md). The [style contract
reference](../reference/style_contract.md) holds the rules, and [using the style
endpoints](use_style_endpoints.md) shows each endpoint with commands.

Build controls from the contract at `/api/v2/status/style_contract` and from the selected style's
details, and leave validation to the server. The frontend then keeps no copy of the rules that could
drift from the server's.

In this guide, `apiOrigin` is the URL origin without an API path, such as `https://aihorde.net` or
`http://localhost:7001`, and `apiV2BaseUrl` is `${apiOrigin}/api/v2`.

## Fetch the style contract

Fetch the contract once per API origin at startup and cache it per origin:

```http
GET <apiV2BaseUrl>/status/style_contract
Client-Agent: <frontend-name>:<version>:<project-url>
```

The endpoint is anonymous and has no rate limit. The body carries `schema_version` and one section per
style type, `text` and `image`, each with:

| Key | Drives |
| --- | --- |
| `overridable_parameters` | The params a policy may hand to the request |
| `ceiling_parameters` | The params a policy may cap, each with the `minimum` and `maximum` a ceiling may take |
| `placeholders` | The placeholders the horde fills itself (`p`, and `np` for image), which a style cannot declare |
| `protected_patterns` | Regular expressions for template text left exactly as written |
| `brace_handling` | A short statement of what braces in the template mean, suitable as help text |
| `context_fit_modes` | The `context_fit` values a text request accepts, null for image |
| `style_create_rate_limits`, `style_modify_rate_limits` | The limits style writes are held to |

Pin the `schema_version` the frontend was written against. On a higher version, keep the controls the
frontend already renders and do not drive new ones from the new document until the frontend is
updated. Ignore keys the frontend does not recognise at every level.

Verification: the cached contract has `schema_version` and both sections, and a second origin
configured in the frontend gets a separate entry.

## Build style authoring controls

A style editor posts to `/api/v2/styles/<type>` to create and patches `/api/v2/styles/<type>/<style-id>`
to change. Only trusted users and users with the customizer role can create styles. Other users get
`StylesRequiresCustomizer`, and anonymous users get `StylesAnonForbidden`.

### Parameter policy

`parameter_policy` has three parts:

- `override` is `none` (the request's params are ignored), `listed` (only the params in `overridable`
  come from the request) or `all` (every request param does). `n` always comes from the request.
- `overridable` is shown only under `listed`, as a choice from `overridable_parameters`. Sending it
  under another mode is refused with `StylePolicyOverridableMisplaced`, and `n` cannot be listed.
- `ceilings` maps a param to the largest value a request may set. Offer a ceiling only for a param in
  `ceiling_parameters` that the selected mode hands to the request, or for `n` under every mode, since
  the server refuses a ceiling the mode would never check. Bound each ceiling input by the param's
  published `minimum` and `maximum`.

### Template fields

`template_fields` is a list of placeholders the template fills from the request, each with `name`,
`description` and `required`. Names are lowercase snake case, and `p` and `np` are reserved. The
reference lists the length and count limits. Check that each declared name appears in the template as
`{name}`, since an unused field is accepted but does nothing.

### Template text

- **Text:** the template is a Python format string. `{{` and `}}` are literal braces, and a
  placeholder is a bare name with no format spec, conversion, attribute or index access. Text matching
  a `protected_patterns` expression, such as `{{[INPUT]}}`, is sent as written, so match those
  expressions before formatting a preview. Show `brace_handling` as the help text.
- **Image:** every brace other than `{p}`, `{np}` and declared fields is literal, so wildcards and
  workflow strings pass through. `{np}` is required, and `###` separates the positive and negative
  halves.

### Saving

Send only the fields the user changed on `PATCH`. `parameter_policy` and `template_fields` are each
replaced whole when sent and cannot be cleared back to null, so send the full object built from the
editor state. Pace writes against `style_create_rate_limits` and `style_modify_rate_limits`, and on 429
show the wait instead of retrying at once.

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
with a default, and under `listed` the unlisted ones are ignored without an error.

Verification: a dry run of the form's request returns 200, and `resolved.params` holds the values the
form sent for the params the policy lets through.

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

The response carries `kudos` and `resolved`. `resolved` holds the prompt after templating, the params
after the policy, the models and the style that applied. A text request adds `estimated_prompt_tokens`
and the `context_fit` applied, and an image request adds `negative_prompt`. Under someone else's
style the quote includes a 2-kudo surcharge for the style's author.

A styled dry run is computed fresh each time and counts against the generate rate limit, so trigger a
preview from an explicit action or once the inputs settle.

Verification: `resolved.prompt` shows each template field's value in place, and a request under a
style the user authored quotes the same as the unstyled request with the style's settings.

## Offer collections

A collection groups styles of one type. A request with a collection's id in `style` draws one of its
styles at random and runs under that style's declarations. The draw is only among the styles that
declare every field the request sends and require no field it leaves out.

`/api/v2/collections/<collection-id>` lists the collection's style ids. To build the request form:

1. Read each listed style's details.
2. Offer the union of their template fields, each marked required only where every style requires it.
3. Before sending, check that at least one style declares every field the user filled in and requires
   none they left empty. When no style qualifies, the server refuses the request with
   `TemplateFieldsMatchNoCollectionStyle`.
4. Show `resolved.style` from the dry run, since the style drawn can differ between requests.

A collection mixing text and image styles is refused with `StyleMismatch` on create and on `PATCH`.

Verification: with a collection of one style that requires a field and one that declares none, a dry
run without the field resolves to the second style every time, and with the field to the first.

## Show a style's shared key to its owner

A style can carry a shared key that requests under the style run on. Its details are served only on the
single-style routes, and only when the request's `apikey` header holds the style owner's key. Read it
that way in the owner's editor, and keep that response out of shared caches, as its
`Cache-Control: private, no-store` header requires. Everyone else, and every listing, gets
`shared_key` as null.

Verification: the owner's editor shows the key, and the same style viewed by another account or
anonymously shows none.

## Check integration invariants

Before considering the integration complete, verify that:

- The contract is cached per API origin, and an unsupported `schema_version` never drives new controls.
- Authoring choices come from `overridable_parameters` and `ceiling_parameters` as served.
- A ceiling input is offered only where the selected mode hands the param to the request, or for `n`.
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
| A required template field left empty | The form blocks sending, since the server would return `TemplateFieldMissing` |
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

## Recover from validation errors

API errors carry a stable `rc` and display text in `message`. Branch on `rc` and show `message` without
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
| `StyleDeclarationInvalid` | The style's stored declaration no longer validates. Tell the user to pick another style or ask its author to edit it. |
| `PromptExceedsContext` | Shorten the prompt, raise `max_context_length`, or choose `context_fit: grow`. |
| `TooManyStyleExamples`, `ExampleURLAlreadyInUse` | Remove an example, or choose another URL. |

Verify each recovery with one manual resubmission. If it is refused again, keep the error visible with
its `rc` and keep the user's other inputs.
