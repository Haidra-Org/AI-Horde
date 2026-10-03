---
title: "Update an existing style integration"
summary: "The changes in AI Horde 5.1.13 that a client already using styles can observe, what to change for each, and how to check it."
topics: [generation, requests]
order: 55
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Update an existing style integration

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

AI Horde 5.1.13 publishes a style contract and fixes several style endpoint defects. A client that
already creates, reads or generates under styles keeps working, with these exceptions to check:

- Image style creation is limited to 20 an hour per address, and reads now fall under the app's default
  limit instead of the write limits.
- A `PATCH` changes only the fields it carries.
- `shared_key` is served only to the style's owner.
- Style errors report their specific `rc` where they used to report `BadRequest`.
- Text templates keep `{{[NAME]}}` placeholders, and a template field that is more than a bare name is
  refused.

An image style that declares neither `parameter_policy` nor `template_fields` resolves a request
exactly as before.

Preconditions: a client that uses `/api/v2/styles/text`, `/api/v2/styles/image` or `/api/v2/collections`,
or sends `style` on `/api/v2/generate/async` or `/api/v2/generate/text/async`. Checking the changes
needs a deployment at 5.1.13 or later.

End state: the client paces style writes against the published limits, sends only changed fields on
`PATCH`, reads `shared_key` as the owner, branches on the new return codes, and writes text templates
that pass the placeholder rule.

Each action below is a client change, and reversing one means restoring the previous client code.
The [style contract reference](../reference/style_contract.md) holds the full rules, and [using the
style endpoints](use_style_endpoints.md) walks through every endpoint with commands. A frontend adding
the new declarations should follow [adding styles to a frontend](add_styles_to_a_frontend.md).

Commands use two shell variables:

```bash
HORDE=https://aihorde.net/api/v2
APIKEY=<your-api-key>
```

## Changes that need action

### Rate limits on style writes and reads

| Operation | Before | Now |
| --- | --- | --- |
| Create a text style | 20 an hour and 2 a second | Unchanged |
| Create an image style | 90 a minute and 2 a second | 20 an hour and 2 a second |
| Patch or delete a text style | 90 a minute and 2 a second | Unchanged |
| Patch or delete an image style | 20 an hour and 2 a second | 90 a minute and 2 a second |
| List styles, read one style, read a collection | Held to the route's write limits | The app default, 90 a minute |

Every limit is per address, method and request path, so the patch and delete limits count each style
separately. A whitelisted service address gets more per second and per minute. The hourly create limit
is the same for every address.

Action: a client that creates image styles in bulk paces creation at 20 an hour, and backs off on 429
for the window the limit covers. Read the limits from the contract instead of hard-coding them:

```bash
curl -s "$HORDE/status/style_contract" | jq '{text: .text | {style_create_rate_limits, style_modify_rate_limits}, image: .image | {style_create_rate_limits, style_modify_rate_limits}}'
```

Check: the response lists `20/hour` and `2/second` for creation and `90/minute` and `2/second` for
modification, for both types.

### Partial `PATCH`

A `PATCH` used to reset every field it left out, so an omitted `prompt`, `name`, `public` or `nsfw`
took the value a new style gets. A text style `PATCH` failed with 500 every time, and a
`sharedkey` on any `PATCH` was refused as unknown.

Action: send only the fields being changed. A client that worked around the reset by sending the whole
style keeps working, since sending a field with its current value changes nothing. `parameter_policy`
and `template_fields` are each replaced whole when sent, and cannot be cleared back to null.

Check: patch one field and read the style back.

```bash
curl -s -X PATCH "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY" \
  -H "Content-Type: application/json" -d '{"info": "<new description>"}'
curl -s "$HORDE/styles/image/<style-id>" | jq '{info, prompt, public, nsfw}'
```

`info` is the new value and the other fields are unchanged.

### Shared key visibility

Every read of a style used to include its shared key, which let any reader spend the key outside the
style. Now the list routes serve `shared_key` as null to everyone, and the single-style routes
(`/styles/<type>/<style-id>`, `/styles/<type>_by_name/<style-name>`) serve it only when the request
carries the owner's API key in the `apikey` header. That response carries `Cache-Control: private,
no-store`.

Action: a client that shows or edits a style's shared key reads the single style with the owner's key.
A client that took a shared key from someone else's style can no longer do so. A request under the
style still runs under its shared key without the client seeing it.

Check:

```bash
curl -s "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY" | jq .shared_key
curl -s "$HORDE/styles/image/<style-id>" | jq .shared_key
```

The first returns the key's details for the owner, and the second returns null.

### Style return codes

These errors used to report `rc: BadRequest` or `rc: Forbidden`, and now report the code below.

| Situation | `rc` now |
| --- | --- |
| A style of the other type on a request, or a collection mixing types | `StyleMismatch` |
| A style read through the other type's route | `StyleGetMistmatch` |
| A style prompt without `{p}`, or an image style prompt without `{np}` | `StylePromptMissingVars` |
| A style written with a shared key that does not exist | `SharedKeyInvalid` |
| A style written with a shared key that is empty or expired | `SharedKeyEmpty`, `SharedKeyExpired` |
| A fifth example on an image style | `TooManyStyleExamples` |
| An example URL the style already has | `ExampleURLAlreadyInUse` |

The `message` text is unchanged. New codes for the new declarations are listed in [adding styles to a
frontend](add_styles_to_a_frontend.md#recover-from-validation-errors).

Action: branch on `rc`, and treat an unrecognised `rc` like the generic one the client already handles.
Never parse `message`.

Check: request a text style through the image route and read the code.

```bash
curl -s "$HORDE/styles/image/<text-style-id>" | jq .rc
```

It returns `StyleGetMistmatch`.

### Text template placeholders

A text style's template is a Python format string, and its rules changed in two ways:

- `{{[NAME]}}`, with uppercase letters and underscores between the brackets, now reaches the worker as
  written. It used to be formatted into `{[NAME]}`, which broke instruct templates written for
  koboldcpp.
- A replacement field must be a bare name such as `{p}` or `{tone}`. A format spec (`{p:>20}`), a
  conversion (`{p!r}`), attribute or index access (`{p.x}`, `{p[0]}`), a positional field (`{}`, `{0}`)
  or an unbalanced brace is refused with `StylePromptFieldInvalid` on create and on `PATCH`. A template
  stored before this rule has such a field sent to the worker as written, and any later `PATCH` of that
  style is refused until the prompt is corrected.

Action: write literal braces in a text template as `{{` and `}}`, and keep placeholders bare. Image
templates are unaffected, since every brace in them other than `{p}`, `{np}` and declared fields was
already literal.

Check: create a text style with a literal brace written as `{{` and confirm a dry run shows it as one
brace in `resolved.prompt`.

### Generating under a collection

Every generate request under a collection used to fail with 500. Writes to collections had two
defects as well. A collection built from style ids was created without a type, and a `PATCH` that left
the styles alone was refused.

Now a request with a collection in `style` draws one of the collection's styles at random and runs
under it, including that style's shared key. The draw is only among the styles whose declared fields
match the request's `template_fields`. When none match, the request is refused with
`TemplateFieldsMatchNoCollectionStyle`. A collection mixing text and image styles is refused with
`StyleMismatch` when it is created or patched. Each request counts one use of the collection and one of
the drawn style.

Action: a client offering collections reads `resolved.style` from a dry run to show which style a
request ran under, and offers the template fields of the collection's styles as described in [adding
styles to a frontend](add_styles_to_a_frontend.md#offer-collections).

Check:

```bash
curl -s -X POST "$HORDE/generate/async" -H "apikey: $APIKEY" -H "Content-Type: application/json" \
  -d '{"prompt": "<prompt>", "style": "<collection-id>", "dry_run": true}' | jq .resolved.style
```

It returns the `id` and `name` of one of the collection's styles.

## Changes that need no action

- **Two more keys on served styles.** `parameter_policy` and `template_fields` are present on every
  served style, null when the style declares none. A text style's served `params` now include
  `max_length` and `max_context_length` when the style sets them, where they used to be dropped. A
  client that rejects unknown keys has to accept these. horde_sdk accepts them.
- **`resolved` on dry runs.** Beside `kudos`, a dry run returns the prompt, params, models and style
  the request resolved to. The queued 202 body is unchanged.
- **Style surcharge and author credit.** A request under a style its requester authored no longer pays
  the 2-kudo surcharge. A request under someone else's style pays it, and the author is credited what the
  requester's debit collected, once the request is queued. Dry runs and refused requests credit
  nothing, and neither does a request whose debit the minimum-balance floor forgives, such as an
  anonymous one. The credit appears once the kudos applier folds the debit.
- **Cached reads after a write.** An anonymous read of one style used to serve the old version for
  up to 30 seconds after a `PATCH` or `DELETE`. Reads by id, by name and by owner-qualified name now
  reflect the write at once. Listings and collections can still lag by up to 30 seconds.
- **A primary first example.** Adding the first example with `primary: true` used to fail with 500.
- **The contract endpoint.** `GET /api/v2/status/style_contract` is anonymous and unlimited, and
  publishes what the style validators accept.

## What stays the same

For a style that declares neither `parameter_policy` nor `template_fields`, a request resolves as it did:

- The style's params replace the request's, except `n`, which comes from the request.
- An image style that sets no `width` or `height` takes them from the request.
- The request runs on the style's models, with its `nsfw` flag.
- An image template fills `{p}` and `{np}` and leaves every other brace as written, with the request's
  prompt split at `###`.

`TestImageDryRunResolvedRequest` in `tests/integration/test_image_style_application.py` and
`TestTextStyleApplicationCompatibility` in `tests/integration/test_text_style_application.py` check that
a style without declarations quotes and resolves as an unstyled request with the style's settings.

## Verify the update

Run these against the deployment and compare with the client's behavior:

1. Read the contract and confirm the client's pacing uses `style_create_rate_limits` and
   `style_modify_rate_limits`.
2. Patch one field of a style and confirm the others are unchanged.
3. Read one of the account's styles with and without the API key and confirm `shared_key` appears only
   with it.
4. Trigger `StyleGetMistmatch` and confirm the client branches on `rc`.
5. Dry run a request under one of the client's styles and confirm `resolved` matches what the client
   expects to send.
