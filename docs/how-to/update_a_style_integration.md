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
- A `PATCH` changes only its supplied fields.
- `shared_key` is served only to the style's owner.
- Style errors report their specific `rc` where they used to report `BadRequest`.
- Text templates keep `{{[NAME]}}` placeholders, and a template field that is more than a bare name is
  refused.

An image style that declares neither `parameter_policy` nor `template_fields` resolves a request
exactly as before.

These checks apply if your client uses `/api/v2/styles/text`, `/api/v2/styles/image` or `/api/v2/collections`,
or sends `style` on `/api/v2/generate/async` or `/api/v2/generate/text/async`. Run them against a deployment
at 5.1.13 or later.

After the update, verify that the client respects the published write limits and handles partial updates without
resetting other fields. Also check shared-key access and the revised template validation errors.

Client changes can be reversed by restoring the previous client code. Checks that modify a server-side style
need saved state; use a style owned by your test account.
The [style contract reference](../reference/style_contract.md) holds the full rules, and [using the
style endpoints](use_style_endpoints.md) walks through every endpoint with commands. A frontend adding
the new declarations should follow [adding styles to a frontend](add_styles_to_a_frontend.md).

Commands use Bash, `curl` and `jq`. Replace the quoted placeholders before running them.
Set two shell variables:

```bash
HORDE=https://aihorde.net/api/v2
APIKEY="<your-api-key>"
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

The patch and delete limits apply to each style separately. [Write rate
limits](../reference/style_contract.md#write-rate-limits) in the reference states the full rule.

If your client creates image styles in bulk, limit creation to 20 an hour. After a 429 response, wait
for the limit's window before retrying. Read the limits from the contract so the client can follow changes:

```bash
curl -s "$HORDE/status/style_contract" | jq '{text: .text | {style_create_rate_limits, style_modify_rate_limits}, image: .image | {style_create_rate_limits, style_modify_rate_limits}}'
```

Check: the response lists `20/hour` and `2/second` for creation and `90/minute` and `2/second` for
modification, for both types.

### Partial `PATCH`

A `PATCH` used to reset every field that was left out of it. An omitted `prompt`, `name`, `public` or
`nsfw` was set to the default value for a new style. A text style `PATCH` failed with 500 every time,
and a `sharedkey` on any `PATCH` was refused as unknown.

Action: send only the fields being changed. A client that worked around the reset by sending the whole
style keeps working. The rules for a partial `PATCH`, including how the declarations are replaced, are
under [Partial PATCH](../reference/style_contract.md#partial-patch) in the reference.

Before PATCH, save the current style and extract the field this check replaces:

```bash
curl -fsS "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY" -o style-before.json
jq -e 'has("info") and has("prompt") and has("public") and has("nsfw")' style-before.json
jq '{info}' style-before.json > info-before.json
```

Stop if the read or field check fails. Inspect the saved file, then patch one field and read the style by ID:

```bash
curl -s -X PATCH "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY" \
  -H "Content-Type: application/json" -d '{"info": "<new description>"}'
curl -s "$HORDE/styles/image/<style-id>" | jq '{info, prompt, public, nsfw}'
```

Compare readback with `style-before.json`: `info` has the new value and the other fields are unchanged.
To restore the description, PATCH the saved field, then read the style by ID again:

```bash
curl -fsS -X PATCH "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY" \
  -H "Content-Type: application/json" --data-binary @info-before.json
```

Restoration affects future requests; it does not undo generations already submitted.

### Shared key visibility

Every read of a style used to include its shared key, which let any reader spend the key outside the
style. Now only the single-style routes serve it, and only when the request includes the owner's API
key in the `apikey` header. The list routes serve `shared_key` as null to everyone. [Shared key
visibility](../reference/style_contract.md#shared-key-visibility) in the reference lists the routes and
the cache header.

To display or edit a style's shared key, read the individual style with the owner's API key. Other users can
still generate with the style and use its shared key to pay, but cannot retrieve the key for use outside the style.

Check:

```bash
curl -s "$HORDE/styles/image/<style-id>" -H "apikey: $APIKEY" | jq .shared_key
curl -s "$HORDE/styles/image/<style-id>" | jq .shared_key
```

Use a test style with a valid shared key for this check. Its owner-authenticated read returns the key's details
and the unauthenticated read returns null. Without a configured shared key, both reads can return null.

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

Action: branch on `rc`, and treat an unrecognised `rc` like the generic code that the client already handles.
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
  or an unbalanced brace is refused with `StylePromptFieldInvalid` on create and on `PATCH`. When a
  template stored before this rule has such a field, the field is sent to the worker as written, and any
  later `PATCH` of that style is refused until the prompt is corrected.

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
match the request's `template_fields`. When no style matches, the request is refused with
`TemplateFieldsMatchNoCollectionStyle`. A collection mixing text and image styles is refused with
`StyleMismatch` when it is created or patched. Each request counts as one use of the collection and one
use of the drawn style.

If your client offers collections, use `resolved.style` from a dry run to display the selected style.
Offer the template fields of the collection's styles as described in [adding
styles to a frontend](add_styles_to_a_frontend.md#offer-collections).

Check:

```bash
curl -s -X POST "$HORDE/generate/async" -H "apikey: $APIKEY" -H "Content-Type: application/json" \
  -d '{"prompt": "<prompt>", "style": "<collection-id>", "dry_run": true}' | jq .resolved.style
```

It returns the `id` and `name` of one of the collection's styles.

## Changes that need no action

- **Two more keys on served styles.** `parameter_policy` and `template_fields` are present on every
  served style, null when the style does not declare them. A text style's served `params` now include
  `max_length` and `max_context_length` when the style sets them, where they used to be dropped. A
  client that rejects unknown keys has to accept these. horde_sdk accepts them.
- **`resolved` on dry runs.** Beside `kudos`, a dry run returns the prompt, params, models and style
  that the request resolved to. The queued 202 body is unchanged.
- **Style surcharge and author credit.** A request under a style authored by the requester no longer
  pays the 2-kudo surcharge. A request under someone else's style pays it. Once the request is queued,
  the author is credited the amount collected by the requester's debit. Dry runs and refused requests do
  not credit the author. Neither does a request whose debit is forgiven by the minimum-balance floor,
  such as an anonymous one. The full rule is under [Style surcharge and author
  credit](../reference/style_contract.md#style-surcharge-and-author-credit).
- **Cached reads after a write.** An anonymous read of one style used to serve the old version for
  up to 30 seconds after a `PATCH` or `DELETE`. Reads by id, by name and by owner-qualified name now
  reflect the write at once. Listings and collections can still lag by up to 30 seconds.
- **A primary first example.** Adding the first example with `primary: true` used to fail with 500.
- **The contract endpoint.** `GET /api/v2/status/style_contract` is anonymous and is not rate limited.
  It publishes the rules applied by the style validators.

## What stays the same

For a style that declares neither `parameter_policy` nor `template_fields`, a request resolves as it did:

- The style's params replace the request's, except `n`, which comes from the request.
- An image style that does not set `width` or `height` takes them from the request.
- The request runs on the style's models, with its `nsfw` flag.
- An image template fills `{p}` and `{np}` and leaves every other brace as written, with the request's
  prompt split at `###`.

`TestImageDryRunResolvedRequest` in `tests/integration/test_image_style_application.py` and
`TestTextStyleApplicationCompatibility` in `tests/integration/test_text_style_application.py` check that
a request under a style without declarations is quoted and resolved like an unstyled request with the
style's settings.

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
