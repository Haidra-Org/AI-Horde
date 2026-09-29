---
status: accepted
date: 2026-09-13
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Protect koboldcpp instruct placeholders and publish the style contract

## Context and Problem Statement

A text style's prompt template is filled with `str.format_map`, so every brace in the template is a
format specifier. koboldcpp templates carry instruct placeholders of the form `{{[INPUT]}}` and
`{{[OUTPUT]}}`, which the text backend substitutes when it builds its own prompt. Under `format_map`
those tokens collapse to `{[INPUT]}` and reach the worker as literal text the backend no longer
recognises. Nothing rejects the template, so the style is created, the request succeeds, and the
generation is produced against a prompt the style author did not write.

An image style's template already escapes every brace and restores only `{p}`, `{np}` and the
placeholders the style declares, so a wildcard or workflow string in braces passes through. The two
style types therefore treat the same template text differently, and neither rule is stated anywhere a
client can read.

A client offering style authoring has the same problem one level up. Which params a
`parameter_policy` may hand over, which it may cap and between what bounds, which placeholders the
horde fills, and what happens to braces are all decided in
`horde/classes/base/style_contract.py`, `horde/apis/v2/kobold_styles.py` and
`horde/apis/v2/stable_styles.py`. A browser frontend cannot import any of it, so it hardcodes a copy
and the copy drifts from what the endpoints accept.

Separately, the 2-kudo style surcharge was decided inside `apply_style`, which runs before the shared
validation resolves the requesting user. The owner comparison therefore ran against a user of `None`
and every styled request paid the surcharge, including a request running under its own author's
style.

[ADR 12](0012-publish-versioned-sampler-contracts.md) settled the equivalent question for samplers:
one typed registry in the SDK, versioned projections published over HTTP, and a worker conformance
version. The style contract needs the publication half of that and has different needs for the other
two.

## Decision Drivers

- A template written against a text backend has to reach that backend as it was written.
- A change to template handling must not invalidate templates that already work.
- A client without Python has to discover what a style may declare before it offers the controls.
- What is published has to be what the endpoints enforce, not a second copy of it.
- A style author must not be charged for using their own style.
- Style authoring is a horde concept with no worker-side behaviour, unlike sampler execution.

## Considered Options

For brace handling in a text template:

- Leave text formatting as it is and document the collapse
- Give text the image rule: escape every brace, restore `{p}` and the declared placeholders
- Protect the koboldcpp pattern and leave every other brace under the existing rule
- Add a per-style syntax switch choosing between formatting modes
- Fill only the placeholders a style declares and treat every other brace as literal

For publication:

- Publish nothing and let clients read the reference page
- Publish a document projected from the validators' own objects, carrying a schema version
- Add a worker conformance layer as ADR 12 has
- Hold the registry in the SDK and project from there, as ADR 12 does

## Decision Outcome

Chosen options: "Protect the koboldcpp pattern and leave every other brace under the existing rule",
and "Publish a document projected from the validators' own objects, carrying a schema version".

`PROTECTED_TEXT_PLACEHOLDER_PATTERN` in `horde/classes/base/style_application.py` matches
`{{[NAME]}}` with uppercase letters and underscores between the brackets. `format_text_style_prompt`
replaces each match with a marker carrying a per-call random part, formats the template, and puts the
original text back. A value supplied through `template_fields` cannot be written to land on a marker,
because the marker is drawn after the request arrives. Every other brace behaves as it did: `{p}` and
the declared placeholders are filled, a placeholder nothing fills becomes the empty string, and `{{`
is one literal brace. Image formatting is unchanged.

An anonymous `GET /api/v2/status/style_contract` serves the document that
`horde/style_contract_document.py` compiles from the per-type `StyleContractVocabulary` the style
endpoints validate against and from the formatting constants the request path uses. It carries
`schema_version`, starting at 1, and a section per style type holding the horde-filled placeholders,
whether declared fields are placeholders, the protected patterns as regular expressions with a
description each, one sentence of brace handling, the overridable params, the params a ceiling may
cap with the range each ceiling may take, the context-fitting modes for text and null for image, and
the rate limits a style write is held to. The document is compiled once per process and held, since
it is a pure function of the installed code.

The registry stays in the horde rather than moving to the SDK. This is a difference from ADR 12 and is
intended: a style's vocabulary is derived from the restx params models the horde already defines, it
has no meaning outside the horde's own validation, and a client that reads the document needs no
Python. Moving it to the SDK would add a release ordering constraint to gain nothing.

No worker conformance layer is added. A worker never sees a style: templating, the policy merge and
context sizing all resolve before the waiting prompt is built, and the worker receives an ordinary
payload. There is no worker behaviour to make a claim about.

The surcharge moves out of `apply_style` into `credit_style_owner` on both gentypes, called after
`super().validate()` has resolved the user. A request under its own author's style is neither charged
the 2 kudos nor credits the author; a request under someone else's style does both.

### Consequences

- Good: A koboldcpp template reaches the backend as it was written, and a template that worked before
  works unchanged, since the protected form previously had no useful meaning.
- Good: A client builds its authoring controls from the same objects the endpoints validate against,
  so an added param or a changed bound reaches the client without a release on either side.
- Good: `schema_version` gives a client one value to pin and to re-read the document on.
- Good: A style author can use their own style at the unstyled price.
- Bad: The two style types still handle braces differently, since the image rule escapes everything
  and the text rule escapes nothing outside the protected pattern; a client has to read the
  per-type `brace_handling` rather than assume one rule.
- Bad: A lowercase or otherwise malformed instruct token is not protected and still collapses, so the
  protection is exactly as wide as the published regular expression.
- Bad: Three places now state the same rule (the formatting function, the published document and the
  reference page), and a change to any of them requires the other two and a schema version bump.
- Bad: A client that caches the document holds a contract that can be older than the deployment it is
  sending to.

## Pros and Cons of the Options

### Leave text formatting as it is and document the collapse

- Good: No code change, and no new rule to publish.
- Bad: The failure is silent. The style is accepted, the request succeeds, and the generation runs
  against text the backend cannot use.
- Bad: Every koboldcpp template has to be rewritten to double its braces, which is invisible in a
  template field and easy to get wrong.

### Give text the image rule: escape every brace, restore `{p}` and the declared placeholders

- Good: One rule for both style types, and the protected tokens pass through as a side effect.
- Bad: It changes what an existing text template does. A template using `{{` for a literal brace
  would start emitting two.
- Bad: It removes the empty-string fallback for an unfilled placeholder, since an unrestored
  placeholder would become literal text in the prompt.

### Add a per-style syntax switch choosing between formatting modes

- Good: An author picks the rule their template was written for.
- Bad: It doubles the behaviour every client, test and reviewer has to hold, for one pattern.
- Bad: The switch itself becomes a published field whose values can never be retired.

### Fill only the placeholders a style declares and treat every other brace as literal

- Good: The rule is short and the protected tokens pass through with nothing special about them.
- Bad: A style that rewrites its prompt without rewriting its declarations silently stops filling a
  placeholder, where the current rule empties it.
- Bad: It is the same break to existing templates as adopting the image rule.

### Publish nothing and let clients read the reference page

- Good: Nothing to version and nothing to serve.
- Bad: Every client hardcodes the vocabulary, and a param added to the params model reaches clients
  only when someone notices.
- Bad: A client cannot check a template or a policy before sending it, so the first sign of a
  mismatch is a rejected write.

### Add a worker conformance layer as ADR 12 has

- Good: It would match the shape of the sampler contract.
- Bad: A worker never receives a style, so there is no worker behaviour for a version string to
  describe.
- Bad: It would add a version every worker has to advertise and the server has to parse, protecting
  nothing.

### Hold the registry in the SDK and project from there, as ADR 12 does

- Good: Python clients could import the vocabulary directly.
- Bad: The vocabulary is derived from the horde's own restx params models, so the SDK copy would have
  to be kept in step with them.
- Bad: It puts the API and the SDK into a release order for a document the horde can serve on its
  own.

## Confirmation

`tests/integration/test_style_contract_endpoint.py` compares the served document against the same
vocabulary objects the style endpoints validate against, and checks anonymous access, the per-type
sections, the protected pattern against a real template, and the published rate limits.
`tests/unit/test_style_application.py::TestTextPromptFormatting` fixes the formatting rule, including
the lowercase and malformed forms that are not protected and a supplied value that resembles a marker.
`tests/integration/test_text_style_application.py::TestTextStyleInstructPlaceholders` checks that an
instruct placeholder reaches the resolved request unchanged, and
`TestTextStyleQuote::test_your_own_style_adds_no_surcharge` fixes the owner case of the surcharge.

## More Information

The published projection follows [ADR 12](0012-publish-versioned-sampler-contracts.md) in shape and
differs from it on registry ownership and worker conformance, for the reasons given above. The
[style contract reference](../reference/style_contract.md) states the enforced rules and the fields of
the published document; [Use the style endpoints](../how-to/use_style_endpoints.md) is the procedure
for declaring and using a style.
