---
status: accepted
date: 2026-10-28
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Send chat images to workers by the transport each worker declares

## Context and Problem Statement

A chat request may carry images, as data URLs or as `http(s)` URLs. A worker serving the request needs the image
bytes. Some workers fetch a URL; others parse only inline data URLs. A large request also makes a large pop
response, which the Horde builds on its hot path. How do image bytes reach the worker?

## Decision Drivers

- The pop path must not read object storage while the queue row lock is held.
- Pop responses should stay small.
- A worker may fetch a URL or may parse only an inline data URL.
- A signed URL must stay valid until the worker fetches it, which may follow a wait in the worker's local queue.
- Image bytes must stay out of the request row, which every queue scan loads.

## Considered Options

- Store images as JPEG at submit and send each worker the transport it declares: offloaded messages URL, per-image
  signed URL, or inline base64 read after the row lock is released
- A per-image signed URL for every worker
- Base64 inlined at pop, read from object storage under the row lock
- Base64 stored in the request row at submit

## Decision Outcome

Chosen option: "Store images as JPEG at submit and send each worker the transport it declares".

At submit each image is checked against the caps below, normalised (longest side at most 2048), encoded as JPEG at
quality 90 and stored in the source image bucket under a key the request row references. The row does not hold image
bytes or the submitted URL. JPEG is decoded by every chat worker's image loader: koboldcpp reads images with
llama.cpp's multimodal loader, which decodes PNG and JPEG and does not decode WebP. An `http(s)` image URL is fetched
at submit by `fetch_public_image` in `horde/image.py`, following at most 3 redirects. A store failure deletes the row
and returns 503 `ChatImageStorageUnavailable`, and the request is not charged.

The caps are defined in `horde/chat_contract.py` and shared by the front and the native route:

| Cap                                                                  | Value  | Symbol                                 |
| -------------------------------------------------------------------- | ------ | -------------------------------------- |
| Images per request, for every user                                   | 20     | `MAX_CHAT_IMAGE_COUNT`                 |
| Decoded or fetched bytes per image                                   | 5 MiB  | `MAX_CHAT_IMAGE_BYTES`                 |
| Image bytes per request, trusted users and patrons                   | 25 MiB | `MAX_CHAT_IMAGE_TOTAL_BYTES`           |
| Image bytes per request, other users                                 | 5 MiB  | `MAX_CHAT_IMAGE_TOTAL_BYTES_UNTRUSTED` |
| Prompt tokens each image adds to the fit estimate                    | 512    | `CHAT_IMAGE_PROMPT_TOKENS`             |
| Request body                                                         | 36 MiB | `MAX_CHAT_BODY_BYTES`                  |

The per-request totals apply to the images as sent and again to the JPEGs they are stored as, since storage,
transfer and the worker's download cost the stored bytes. The per-image tokens match what koboldcpp's worker adds in
its own context overflow estimate, so a request with many images is not estimated to fit a context the worker then
refuses. The body cap fits a request at every cap as base64 data URLs plus the text allowance.

A worker declares the transports it accepts in the `image_transports` pop field, stored in the worker's
`image_transports` column. A worker receives a request with images only when it declares the `vision` feature and
the `url` or `base64` transport, since `messages_url` covers only offloaded requests. At pop
`select_chat_transport` in `horde/classes/kobold/chat_request.py` chooses one transport, recorded in the generation's
`image_transport` column:

| Condition                                                     | Transport      | The worker receives                                                                                    |
| ------------------------------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------ |
| The messages were offloaded and the worker declares `messages_url` | `messages_url` | One signed URL to the messages array, with each image inline as a JPEG data URL, valid for 120 seconds |
| Otherwise, the request carries images and the worker declares `url` | `url`          | A signed download URL per image, valid for the job's TTL and at least 600 seconds                      |
| Otherwise, the worker declares `base64`                       | `base64`       | Each image inline as a `data:image/jpeg;base64,` URL read from object storage                          |

Signing is local and does not make a request. The base64 read and the signing run while building the pop response, after
the commits that assign the generation have released the queue row lock. An image URL lives at least as long as the
job's TTL, with a floor of 600 seconds (`MIN_CHAT_IMAGE_URL_SECONDS`), so a worker that queues the job behind another
still finds the URL valid. The messages URL lives 120 seconds (`CHAT_MESSAGES_URL_SECONDS`) because a worker fetches
it right after the pop, and a short lifetime limits how long a leaked URL can be replayed. A worker that cannot fetch
a URL it was sent faults the job.

Messages offload is off by default (`HORDE_CHAT_MESSAGES_OFFLOAD_ENABLED`). When it is on, a request whose messages,
as compact JSON with images inline, are larger than `HORDE_CHAT_MESSAGES_OFFLOAD_BYTES` (256 KiB by default) has them
stored as one object at submit. This applies to text-only requests too. With `HORDE_CHAT_MESSAGES_OFFLOAD_GZIP` set,
the object is stored gzipped with `Content-Encoding: gzip` when that makes it smaller. A worker that does not declare
`messages_url` receives an offloaded request inline by its image transport. One fetch per job, bounded by a JSON
parser, spares the worker managing several partial downloads.

koboldcpp's embedded worker declares `messages_url` and `base64`, so it receives images inline in the pop response or
in the offloaded messages. The `url` transport serves workers that declare it.

Images are deleted after the request row. A key that the store fails to delete is retried from a Redis set by the
cleanup thread.

### Consequences

- Good: The pop does not read object storage under the row lock, and the request row does not hold image bytes.
- Good: A worker receives images by a means it supports.
- Good: With offload on, a worker that declares `messages_url` receives large requests outside the pop response.
- Bad: With offload off, a base64 worker receives megabytes of image data in the pop response.
- Bad: Object storage reachability is a worker requirement for `url` and `messages_url`, and a failed fetch faults
  the job.
- Bad: Each transport is a separate path to maintain and test.

## Pros and Cons of the Options

### Declared transport

- Good: Serves workers that fetch URLs and workers that parse only data URLs.
- Bad: More than one delivery path.

### Per-image signed URL for every worker

- Good: The smallest pop response; image bytes do not pass through the Horde at pop.
- Bad: Excludes workers that take images only inline, and needs one fetch per image.

### Base64 at pop under the lock

- Good: Works with any worker that parses data URLs.
- Bad: An object storage read while the queue row lock is held.

### Base64 in the row

- Good: Does not add pop-time work.
- Bad: Image bytes in Postgres, loaded on every queue scan.

## Confirmation

`tests/unit/test_chat_request.py` (`TestImageTransport`, `TestMessagesOffload`, `TestImageValidation`,
`TestImageTotalByTrust`, `TestChatPromptEstimate`) fixes the transport choice, the offload threshold and gzip, and the
image caps. `tests/integration/test_chat_images.py` fixes the signed URL lifetime, the base64 data URL built after
the lock is released, the offloaded messages fetch and inline delivery to a worker without `messages_url`.
`docs/reference/chat_requests.md` (Images, Messages offload) and `docs/how-to/serve_chat_jobs.md` (Serve images)
record the public behaviour.
