---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Select chat service behaviour through a `horde` request body object

## Context and Problem Statement

A chat client must be able to choose Horde service behaviour, such as trusted workers, validated backends or a
worker allow list, without a Horde-specific client. OpenAI clients expose three places such a choice can travel: the
model id, extra request body fields, and the base URL. Which of these carries service selection?

## Decision Drivers

- Every OpenAI SDK can send extra body fields (`extra_body` in the Python SDK).
- The front and the native chat route should validate the same options with one definition.
- One handler stack, without a profile resolver, revisions or saved configurations.
- The closest comparable service, OpenRouter, uses a body object and `:suffix` model variants, and clients written
  for it send both.
- A path can carry a configuration without any request change, which suits a client that can set only the base URL.

## Considered Options

- A `horde` body object, with the colon reserved in model ids
- A `horde` body object plus `:variant` suffixes on the model id
- Base-URL service profiles resolved by the native service, with revisions
- A `horde` body object with base-URL prefixes as a static table of defaults
- Account-level defaults only

## Decision Outcome

Chosen option: "A `horde` body object, with the colon reserved in model ids".

A chat request carries Horde options in a `horde` object in the body. `horde/chat_contract.py`, `ChatHordeOptions`,
defines it for both the front and the native route, and an unknown key is refused. The fields are:

- `only` and `ignore`: worker ids the request is limited to or excludes; the two cannot be combined.
- `trusted_workers`, `validated_backends` and `slow_workers`: the native worker filters.
- `max_context_length` and `context_fit`: an explicit context length, and what to do when the messages do not fit.
- `conformance`: how closely the serving worker has to follow the OpenAI chat contract
  ([ADR 0036](0036-route-chat-requests-by-declared-guarantees-under-a-conformance-mode.md)).
- `openai_guards`: turns on the request refusals OpenAI applies and the Horde relaxes.
- `availability_mode`: wait for a worker, or refuse at once when the online pool cannot serve the request.
- `max_kudos`: the most the request may cost at creation.
- `include_reasoning`: return the model's reasoning beside the reply.
- `dry_run` and `operation_key`: native callers only. The front refuses both in a public body and supplies the
  operation key itself ([ADR 0026](0026-classify-client-retries-and-recover-submits-by-operation-key.md)).

Every field defaults to the behaviour of a request that omits it. `only` and `ignore` follow OpenRouter's provider
routing names.

The model id is the exact native model name. The front reserves the colon (`horde/oai/app.py`,
`MODEL_VARIANT_SEPARATOR`): a requested model id containing one is refused as unknown with 404 `model_not_found`.
The front does not route by base URL; it serves `/v1/chat/completions` and `/v1/models` only.

### Consequences

- Good: Works through extra body fields in every SDK, with one validated vocabulary on both sides.
- Good: Reserving the colon keeps OpenRouter-style variant suffixes from being read as part of a Horde model name.
- Bad: A client that can set only the base URL, key and model cannot select service behaviour.
- Bad: A model picker built from `/v1/models` shows only the base models; a preset cannot be picked there.
- Bad: The pop route accepts a model name containing a colon, and `/v1/models` lists it. The front then answers a
  request for that model with 404, so such a model is reachable only through the native route.

## Pros and Cons of the Options

### Body object, colon reserved

- Good: Small: one validated object, without a resolver.
- Good: Matches the body fields OpenRouter-aware clients already send.
- Bad: A per-request choice, so an application sends it every time.

### Body object plus model id variants

- Good: Reaches model pickers, so a preset can be chosen in a client that cannot send fields.
- Bad: Variant names are a second public vocabulary to document, keep small and keep in step with the body object.
- Bad: A variant is a set of defaults; a hard constraint for a credential cannot be expressed through it.

### Base-URL service profiles

- Good: A client configured once gets the behaviour for everything it sends, including a client without a picker
  or extra fields.
- Good: A path can carry an authorised private configuration without any request change.
- Bad: Invisible to model pickers, and lost by clients that hardcode `/v1`.
- Bad: Needs a profile resolver, revision pinning per request, native authorisation and a binding into the operation
  key, which is the most infrastructure for the least client reach.

### Body object with static prefix defaults

- Good: Keeps the single-configuration case without a resolver.
- Bad: Two ways to say one thing, with a precedence rule between them.

### Account defaults only

- Good: Does not change the request or the path.
- Bad: One credential cannot serve two applications with different needs.

## Confirmation

`tests/unit/test_chat_request.py` pins the `horde` vocabulary: an unknown key and a key outside the chat vocabulary
are refused, `only` and `ignore` together are refused, and `operation_key` must be a UUID.
`tests/unit/oai/test_request_screening.py` pins the 404 for a model id with a variant suffix, including a listed
model id, the refusal of `horde.dry_run` and `horde.operation_key` in a public body, and the forwarding of the
`horde` options. `docs/reference/chat_front.md` lists the request keys the front
accepts, and `docs/explanation/chat_requests.md` explains service selection.

## More Information

[ADR 0021](0021-select-chat-service-by-base-url-profiles.md) records the base-URL profile option as a rejected
alternative.
