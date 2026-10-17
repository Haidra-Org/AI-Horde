---
title: "Outbound requests reference"
summary: "Which client-supplied URLs the horde requests, the address check every hop passes, and the timeouts, redirect limits and byte caps of image fetches and webhooks."
topics: [requests, operations]
order: 115
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Outbound requests reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [operations](../topics.md#operations), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

The horde fetches client-supplied source and chat image URLs and sends webhooks through `send_public_request` in
`horde/outbound.py`. The client refuses connections to non-public addresses to prevent server-side request forgery
(SSRF).

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Address-checked request | `horde/outbound.py` | `send_public_request`, `OutboundResponse` |
| Address check | `horde/outbound.py` | `is_public_address`, `resolve_host_addresses` |
| Connection to a checked address | `horde/outbound.py` | `_open_bound_response` |
| Errors | `horde/outbound.py` | `OutboundRequestRefused`, `OutboundRequestFailed`, `OutboundResponseTooLarge` |
| Source image and chat image fetch | `horde/image.py` | `fetch_public_image`, `ensure_source_image_uploaded`, `load_chat_image` |
| Webhook sender | `horde/classes/base/processing_generation.py` | `enqueue_webhook`, `_deliver_webhook`, `WEBHOOK_*` |
| Generation webhook | `horde/classes/base/processing_generation.py` | `ProcessingGeneration.send_webhook` |
| Interrogation webhook | `horde/classes/stable/interrogation.py` | `InterrogationForms.send_webhook` |

## Address restrictions

Each hop resolves its host once and is refused, before any connection, when:

- its scheme is not one the caller allows (http or https for images, https only for webhooks), or it has no host;
- its host does not resolve;
- any resolved address is private, loopback, link-local, multicast, reserved, shared or unspecified. An IPv6 address
  that carries an IPv4 address (IPv4-mapped `::ffff:0:0/96`, IPv4-compatible `::/96` or NAT64 `64:ff9b::/96`) is
  checked as that IPv4 address;
- it is a redirect past the caller's limit.

The connection tries the checked addresses in resolver order, with the URL's host as the `Host` header and, over https,
as the TLS server name the certificate is verified against. Only a connection-establishment failure tries the next
address. A read or protocol failure does not replay a request that may already have been sent. Every attempt shares
the total deadline. The connection uses the checked IP without resolving the hostname again, preventing DNS rebinding
between validation and connection. Each followed redirect passes the same checks and uses the same method and body.

URL credentials supply HTTP Basic authentication, with percent-decoded user names and passwords, and override a
caller-supplied `Authorization` header. Authorization is preserved on same-origin redirects and removed when the
scheme, host or port changes. Credentials in a redirect URL can supply authorization for that target.

## Limits

| Caller | Schemes | Redirects | Connect / low-speed window | Total deadline | Body cap |
| --- | --- | --- | --- | --- | --- |
| Source image URL (`source_image`, `source_mask`, `extra_source_images`, interrogation `source_image`) | http, https | 3 | 2 s | 10 s | 5 MiB |
| Chat image URL | http, https | 3 | 2 s | 10 s | 5 MiB |
| Generation and interrogation webhook | https | 0 | 3 s | 6 s per attempt | 64 KiB |

PycURL delegates connections and transfer deadlines to libcurl. `CONNECT_TO` pins each connection to
a checked numeric address while retaining the original hostname for HTTP, TLS SNI and certificate verification.
Automatic redirects and environment proxies are disabled. `TIMEOUT_MS` bounds each transfer with the remaining
shared deadline, including TLS, status lines, headers, body, chunk framing and trailers.
System DNS resolution is synchronous and cannot be interrupted by this client:
resolver time counts against the deadline, and an overrun is rejected before connection, but the DNS call itself
may exceed it. The ten-second image budget applies per URL, including source images and chat images.

The per-hop values configure the connect timeout and libcurl's low-speed window. A transfer below one byte per
second for that window is aborted. The window is rounded up to whole seconds. The total deadline bounds responses
that continue sending data above the low-speed threshold. A timeout after TCP establishment is not retried against
another address.

The byte cap applies to the declared `Content-Length` and the decoded response body, including gzip/deflate
responses. Non-2xx responses are stopped at the end of the headers without consuming the body.

A refused source or chat image URL returns `400 SourceImageUrlRefused`. A source or chat image URL that returns a
status other than 200 returns `400 SourceImageUrlInvalid`, and a body over the cap returns
`400 SourceImageSizeExceeded`.

A source image fetched from a URL is stored in R2 as `<id>.webp`, the key a base64 source image uses. An interrogation
stores URL sources under the same key convention. Alchemists receive a presigned link to the stored copy.
Interrogation cleanup removes the stored image from R2.

## Webhook delivery

Generation and interrogation webhooks share one bounded queue (`WEBHOOK_QUEUE_MAXSIZE`) and a pool of
`WEBHOOK_SENDER_THREADS` background sender threads per process, so delivery does not block the worker's submit request
and one stalled subscriber does not hold back other deliveries. A delivery is POSTed as JSON, up to
`WEBHOOK_ATTEMPTS` times while the subscriber returns 4xx or 5xx or the exchange fails. Redirects are not followed: a
307 or 308 would re-send the payload to a target the subscriber did not register. A refused URL, including a redirect,
is logged once and not retried. A 2xx response whose body exceeds the cap counts as delivered. The `horde.webhook.outcomes`
counter records `ok`, `giveup`, `refused` or `dropped` for each delivery. The `horde.webhook.queue_wait.duration`
histogram records how long each delivery waited for a sender. A rising wait shows the senders falling behind before the
queue fills and drops begin.

## Dependencies

PycURL requires libcurl. Source builds require libcurl development headers. HTTPS connections use the certifi CA
bundle with certificate and hostname verification enabled.
