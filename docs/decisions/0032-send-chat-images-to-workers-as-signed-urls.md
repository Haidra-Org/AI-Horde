---
status: proposed
date: 2026-10-17
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Send chat images to workers as signed object storage URLs

## Context and Problem Statement

A chat request may carry images. The draft stores each as JPEG in object storage (`horde/r2.py`) and, at pop,
fetches the bytes and inlines them as base64 in the pop response while the candidate row lock from the queue query
is held. How should image bytes reach the worker?

## Decision Drivers

- The pop path must not fetch over the network inside a row lock.
- Pop responses should stay small.
- koboldcpp's maintainer is willing to implement fetching a signed URL in the embedded worker.
- A URL signed at submit can expire before a refreshed request is popped.

## Considered Options

- A URL signed at pop with a lifetime no shorter than the job TTL, fetched by the worker
- Base64 inlined at pop, fetched from object storage under the lock
- Base64 stored in the request row at submit

## Decision Outcome

Chosen option: "A URL signed at pop with a lifetime no shorter than the job TTL, fetched by the worker".

Images are normalised and stored at submit as JPEG: koboldcpp reads images with llama.cpp's
multimodal loader, which decodes PNG and JPEG and does not decode WebP. At pop the Horde signs a download URL per image and places it
in the message part. Workers declare the transport they accept on pop. For chat vision, prefer a
required URL transport. Keep base64 only if a demonstrated deployed-worker need requires it, with an explicit
retirement gate and without an object fetch while holding the queue row lock.

### Consequences

- Good: Image bytes do not pass through the Horde on the pop path, and the pop does not fetch under the lock; pop
  responses stay small.
- Bad: A worker fetches from object storage, so object storage reachability becomes a worker-side concern; a failed
  fetch is a faulted job.

## Pros and Cons of the Options

### Signed URL fetched by the worker

- Good: Matches how image workers already receive sources.
- Bad: Worker change required.

### Base64 at pop

- Good: Works with any worker that parses data URLs.
- Bad: Fetch under the lock; megabytes in a JSON response.

### Base64 in the row

- Good: Does not add pop-time work.
- Bad: Image bytes in Postgres, loaded on every queue scan.

## More Information

Vision follows the first text round trip; image schema/transport columns are not required at launch. Before enabling it,
bound redirects, bytes, dimensions, decompression and fetch time; preserve TLS hostname verification when binding an
address. Clean orphan uploads after validation/activation failure and crashes, as well as when a request row is deleted.
Signed URLs and worker lifetimes must be bounded; lease refresh cannot justify indefinite object access.

Ruling R25 of the review. User-supplied URL ingestion at submit is a separate concern, addressed by binding the
validated address to the connection in `horde/image.py`.
