---
title: "Chat front reference"
summary: "The horde.oai package by concern, the metrics the front records with their labels, and the fields of the line it logs per finished request."
topics: [operations, requests]
order: 110
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Chat front reference

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [operations](../topics.md#operations), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

The chat front is the `horde.oai` package, run as `python -m horde.oai`. It serves the OpenAI chat completions and
model list routes over the horde's native `/v2/generate/chat` routes (see
[Chat requests reference](chat_requests.md)) and does not import anything from the Flask application. Running it is covered in
[Run the OpenAI-compatible chat front](../how-to/run_openai_chat_front.md).

## Code map

| Concept | File | Symbol |
| --- | --- | --- |
| Entry point and telemetry setup | `horde/oai/__main__.py` | `main`, `configure_telemetry`, `FRONT_SERVICE_NAME` |
| Settings | `horde/oai/settings.py` | `FrontSettings`, `load_settings` |
| Application and routes | `horde/oai/app.py` | `create_app`, `FrontState` |
| Admission caps and gauges' source | `horde/oai/app.py` | `InflightLimiter`, `InflightSlot` |
| Request lifecycle | `horde/oai/app.py` | `_chat_completions`, `_wait_for_completion`, `_stream_completion` |
| Submit and lost-submit recovery | `horde/oai/app.py` | `_submit`, `_recover_submit` |
| Bounded cancels | `horde/oai/app.py` | `_cancel_within_budget`, `_cancel_open_requests` |
| Per-request timing and finished line | `horde/oai/app.py` | `RequestTrace` |
| Error envelope and SSE error | `horde/oai/app.py` | `ChatFrontError` |
| OpenAI request, response and error models | `horde/oai/schema.py` | `ChatCompletionRequest`, `ChatCompletionResponse`, `ErrorMetadata` |
| Refused and no-op parameters | `horde/oai/schema.py` | `find_refused_parameter`, `drop_default_no_ops` |
| Native body, completion, usage estimate | `horde/oai/mapping.py` | `build_native_chat_body`, `build_completion`, `estimate_tokens` |
| Model list entries | `horde/oai/mapping.py` | `build_model_card` |
| Streamed chunks and keepalive | `horde/oai/sse.py` | `build_completion_events`, `build_keepalive_event`, `build_error_events` |
| `Client-Agent` attribution | `horde/oai/attribution.py` | `build_client_agent` |
| Native HTTP client and root rotation | `horde/oai/horde_client.py` | `HordeClient`, `create_http_client` |
| Completion signal subscription and liveness ping | `horde/oai/waiter.py` | `CompletionSignalListener` |
| Per-request wait, poll and deadline | `horde/oai/waiter.py` | `ChatRequestWatcher`, `WaitBudget`, `WakeSource` |
| Instruments and bucket boundaries | `horde/oai/metrics.py` | `FrontMetrics`, `histogram_views` |

## Telemetry configuration

`configure_telemetry` runs before the application is built. It configures Logfire when
`horde.telemetry.telemetry_enabled` is true and `OTEL_SDK_DISABLED` is not `true`, with the same variables, resource
attributes and sampling as the Flask application, the service name `horde-oai` unless `OTEL_SERVICE_NAME` sets
another, and the views from `horde.oai.metrics.histogram_views`. It bridges Loguru records to OpenTelemetry logs. The
Flask application's `init_telemetry_early` is not reused because it instruments a Flask app.

`create_app` takes the meter from the global meter provider unless one is passed as `meter`; tests pass a meter of
a separate `MeterProvider` with an `InMemoryMetricReader`. Unconfigured, the global provider does not record
anything.

## Metrics

Attribute keys carry the `horde.` prefix. Every attribute is bounded: call types, outcomes, wake sources, HTTP
statuses, error codes, booleans and the model id. The model id comes from a delivered completion, so it is a model a
worker served. Client addresses, API keys, prompts, client titles and request ids are never attributes.

| Name | Type | Attributes | Meaning |
| --- | --- | --- | --- |
| `horde.oai.requests.admitted` | counter | `streamed` | Chat requests that took an in-flight slot. |
| `horde.oai.requests.refused` | counter | `status_code`, `code` | Chat requests refused before an in-flight slot: body size (413), body read timeout (408 `RequestBodyTimeout`), validation (400, 404), authorization (401), `FrontAtCapacity` (503). |
| `horde.oai.requests.errors` | counter | `status_code`, `code`, `streamed` | Admitted requests answered with an error status before any header: horde refusals at submit, 408 `ChatRequestTimeout`, 503 `no_eligible_worker`, 502 `ChatRequestFaulted` and `HordeUnavailable`. |
| `horde.oai.stream.failures` | counter | `code` | Streamed requests whose 200 headers were sent and which ended in an SSE error event. |
| `horde.oai.inflight` | observable gauge | none | Requests holding an in-flight slot. |
| `horde.oai.inflight.sources` | observable gauge | none | Distinct client addresses holding at least one slot. |
| `horde.oai.inflight.source_max` | observable gauge | none | The most slots one client address holds; compare with `HORDE_OAI_MAX_INFLIGHT_PER_SOURCE`. |
| `horde.oai.request.body_bytes` | histogram, bytes | none | Size of each request body received whole; oversized bodies are counted as refusals instead. |
| `horde.oai.native.duration` | histogram, seconds | `call_type`, `outcome` | Latency of each native call from taking a call slot to the answer. The histogram count is the call count. `call_type` is `submit`, `recovery`, `status`, `cancel` or `models`; `outcome` is `ok`, `refused` (horde status 400 or above), `unreachable` or `abandoned` (the request's budget ended first). |
| `horde.oai.queue_wait` | histogram, seconds | `model` | The horde's `queue_wait_seconds` of each delivered generation: request creation to assignment. |
| `horde.oai.first_content.duration` | histogram, seconds | `streamed` | From the submit acknowledgement, when a stream's headers go out, to the first content sent: the first completion chunk, or the non-streamed body. |
| `horde.oai.request.duration` | histogram, seconds | `streamed`, `status_code`, `code` | From an admitted request's arrival to its end. A stream records 200 whatever its outcome; `code` is the error code, `ok` for a delivered completion or `client_closed` for a client that disconnected before delivery. |
| `horde.oai.wakes` | counter | `wake_source`, `found_finished` | Each status read of a waiting request, by what caused it: `immediate` (the read after the submit), `signal`, `recheck`, `poll`, `deadline`; and `disconnect`, recorded without a read when the client disconnects. |
| `horde.oai.submit.outcome_unknown` | counter | none | Requests answered with `submit_outcome: "unknown"`. |
| `horde.oai.submit.recoveries` | counter | `existing_claim` | Lost submits recovered by re-sending; `existing_claim` true is a claim match (horde answered 200). |
| `horde.oai.client_retry_hints` | counter | none | Chat requests carrying `x-stainless-retry-count` above zero. |
| `horde.oai.completions` | counter | `model`, `estimated_usage` | Delivered completions; `estimated_usage` true when a count was estimated because the worker did not report it. |
| `horde.oai.completions.empty` | counter | `model` | Delivered completions with a generation holding no visible text, the case `x_horde.notice` reports. |
| `horde.oai.cancels` | counter | `outcome` | Cancels sent to the horde: `acknowledged`, `failed`, or `timed_out` past `HORDE_OAI_CANCEL_TIMEOUT_SECONDS`. A failed or timed-out cancel is sent once more by the shutdown drain and counted again; at most `horde.oai.app.MAX_FAILED_CANCELS` answered requests are held for that, oldest dropped first. |

The front does not measure settlement lag between delivery and the native terminal state, since it does not observe
the horde's settlement.

Bucket boundaries: native call latency uses the horde's `BUCKETS_SECONDS`; queue wait, first content and request
duration use `BUCKETS_REQUEST_LIFECYCLE_SECONDS` (both in `horde/metrics.py`); body size uses
`horde.oai.metrics.BUCKETS_BODY_BYTES`.

## Finished-request log line

Every admitted request logs one Loguru `INFO` record when it ends. The message is

```text
Chat request <id> finished: streamed=<bool> submitted_at=<unix> woke_at=<unix|none> woke_by=<source|None> waited=<seconds|none>s status=<status> code=<code> wakes=<count> estimated_usage=<bool|None>
```

and the same values are bound to the record's `extra`, which the OpenTelemetry log bridge exports as attributes. A
request refused at submit has no id; its message starts `Unsubmitted chat request finished:`.
`tests/stress/chat_scale_demo.py` parses the message up to `waited`.

| Field | Meaning |
| --- | --- |
| `request_id` | The horde request id, or null before the submit was acknowledged. |
| `streamed` | Whether the request was streamed. |
| `submitted_at` | Wall-clock time the submit was acknowledged, Unix seconds. |
| `woke_at` | Wall-clock time of the status read that found the request finished; null when none did. |
| `woke_by` | The wake source of that read, or `disconnect`; null for a timeout, fail-fast or horde error. |
| `waited_seconds` | `woke_at` minus `submitted_at`, or the time until the end when the request was not found finished. |
| `status_code` | The HTTP status sent: 200 for every stream whose headers went out, 499 for a non-streamed client that disconnected. |
| `code` | The error code, `ok` or `client_closed`. |
| `wake_count` | How many status reads the wait made. |
| `estimated_usage` | Whether the delivered completion's usage was estimated; null when none was delivered. |

The line does not carry the API key, the messages, the client's title or address, or the operation key.
