---
title: "Add chat completions to a web frontend"
summary: "Call the chat front from a browser, parse streamed replies, run tool calls and display model limits and errors."
topics: [chat, requests]
order: 61
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Add chat completions to a web frontend

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [chat](../topics.md#chat), [requests](../topics.md#requests)
<!-- END GENERATED: topics -->

Use this guide to add Horde chat completions to a browser frontend, including streamed replies and tool calls.
You need:

- A browser or app frontend that can send `fetch` requests and read a streamed response body.
- A Horde chat front's base URL, used below as `OAI_BASE_URL`. The public service is `https://oai.aihorde.net/v1`.
- The user's Horde API key, or the anonymous key `0000000000`.

The integration checks below confirm that the frontend displays replies and errors, handles tool results, and
keeps the requested output length within the selected model's advertised limits.

The steps apply to any browser client. The changes needed by KoboldAI Lite's OpenAI mode are collected in
[KoboldAI Lite notes](#koboldai-lite-notes). The request contract itself is in
[Chat front reference](../reference/chat_front.md#request-keys).

## Call the front from the browser

The front responds to CORS preflights for every origin and sets `Access-Control-Allow-Origin: *` on every response.
`Retry-After` and `x-should-retry` are exposed to scripts. A frontend calls it directly with the user's key, without a
proxy.

```javascript
const response = await fetch(`${OAI_BASE_URL}/chat/completions`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${apiKey}`,
    "X-Title": "<your-frontend>",
  },
  body: JSON.stringify(payload),
});
```

`X-Title` attributes the traffic to your frontend in Horde statistics. Without it the front uses the host of the
browser's `Referer` header.

Fetch the model list the same way, from `GET ${OAI_BASE_URL}/models`. Each object in its `data` array is a model
card: `id` identifies the model, `supported_parameters` lists declared options, and `top_provider` holds declared
limits. Read the selected card before building a request. The card combines declarations from online workers;
it does not reserve a worker or guarantee that one worker supports every listed option together. See
[Model card fields](../reference/chat_front.md#model-card-fields).

Verification: in the browser's network panel, the `OPTIONS` preflight returns 200 with
`access-control-allow-origin: *`, and the `POST` that follows includes the same header. If either fails, inspect the response status and
confirm that `OAI_BASE_URL` points at the chat front rather than the native API.

## Build the request from the user's settings

Map the frontend's settings onto OpenAI keys only:

| Frontend setting | Request key |
| --- | --- |
| Output length | `max_tokens`, clamped as in [Keep max_tokens within what is served](#keep-max_tokens-within-what-is-served) |
| Temperature, top-p | `temperature`, `top_p` |
| Stop strings | `stop`, at most four |
| Repetition or presence penalty | `presence_penalty`, -2 to 2, sent only when non-zero and only when the card lists it |
| Seed | `seed`, only when the card lists it |
| Streaming | `stream: true`, and `stream_options: {"include_usage": true}` to receive token counts |

Kobold sampler keys such as `rep_pen`, `top_k` and `min_p` are refused with 400 `UnsupportedChatParameter`,
with the first refused key in `error.param`. Disable options that add these keys when using the Horde endpoint.
Omitted sampling settings receive fixed chat defaults: `temperature` and `top_p` are 1, and presence and frequency
penalties are 0. The worker payload also disables repetition, top-k and min-p filtering through the
[chat sampling defaults](../reference/chat_requests.md#message-content-and-parameter-defaults), so worker-local
sampling defaults do not change the request.

Verification: a request built from the default settings returns 200. Turning on a sampler extra returns 400 with that
key in `error.param`.

## Parse the stream

Check `response.ok` before parsing a stream. Refusals before the stream opens use an HTTP error status and a JSON
body. After the stream opens, failures are SSE events under status 200. Both paths must display the error.

A streamed reply is server-sent events: keepalive comment lines starting with `:` while the request waits, then
`data:` lines with JSON chunks, ending with `data: [DONE]`. For each `data:` payload, check for:

- A top-level `error`, `{"error": {"message", "type", "param", "code", "metadata"}}`. Show `error.message` to the user
  and end the turn. This is how the front reports a failure after the 200 headers went out, including a request that
  could not be served by any worker.
- `choices[0].delta.content`, text to append.
- `choices[0].delta.tool_calls`, tool calls to collect by `index`.

The front sends content and tool calls in separate chunks. A parser should still read both fields from every chunk,
because other OpenAI-compatible services send both in one.

```javascript
async function readChatStream(response) {
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error?.message ?? `Chat request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let served = null;
  const calls = [];

  for (;;) {
    const { value, done } = await reader.read();
    if (done) throw new Error("Chat stream ended before [DONE]");
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop();
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue; // keepalive comments and blank lines
      const data = line.slice(6);
      if (data === "[DONE]") return { text, calls, served };
      const event = JSON.parse(data);
      if (event.error) throw new Error(event.error.message);
      if (event.model) served = { model: event.model, horde: event.x_horde ?? served?.horde };
      const delta = event.choices?.[0]?.delta ?? {};
      if (delta.content) text += delta.content;
      for (const call of delta.tool_calls ?? []) {
        const slot = (calls[call.index] ??= { id: "", type: "function", function: { name: "", arguments: "" } });
        if (call.id) slot.id = call.id;
        if (call.function?.name) slot.function.name += call.function.name;
        if (call.function?.arguments) slot.function.arguments += call.function.arguments;
      }
    }
  }
}
```

This parser handles the front's single-line SSE data format and returns the assembled result after `[DONE]`.
To display text during generation, update the UI as each `delta.content` arrives. Honor `x-should-retry: false`
on HTTP errors as well as streams. The [retry rules](../reference/chat_front.md#errors-the-front-returns) explain when
a resend can create a second charged request.

Verification: a plain turn displays text, or `x_horde.notice` for an empty reply. For a turn that produces tool
calls, collect each name and JSON argument string. A model can answer in text under automatic tool selection.
When the selected model has no online worker, display the error message; an abrupt stream end before `[DONE]`
is a failed turn, not a completed reply.

## Implement the tool loop

1. Offer the tools in `tools`. An MCP server's tool list maps one to one:

   ```javascript
   const tools = mcpTools.map((tool) => ({
     type: "function",
     function: { name: tool.name, description: tool.description, parameters: tool.inputSchema },
   }));
   ```

   Send `tools` only to a model whose card lists `tools` in `supported_parameters`.

2. After a successful stream ends, inspect the collected calls. A forced choice can finish with `stop`, so
   use the calls themselves to decide whether to run tools. Parse their `arguments` and run the offered functions.

3. Append the assistant turn and one result per call, then send the history again with the same `tools`:

   ```javascript
   messages.push({ role: "assistant", content: null, tool_calls: calls });
   for (const call of calls) {
     messages.push({ role: "tool", tool_call_id: call.id, content: resultFor(call) });
   }
   ```

4. Repeat until the reply contains text without further tool calls, then show that text.

A `tool` message includes `role`, `tool_call_id` and `content`. The result is a string, so serialise structured results
as JSON. Any other key on it, such as `name`, is dropped and not forwarded, as OpenAI ignores it. The `tool` messages
must immediately follow the assistant message, with one result for each tool call ID. A history that breaks this rule gets
400 (see [Tool history](../reference/chat_requests.md#tool-history)).

Verification: the follow-up accepts the replayed history. If it returns more calls, continue the loop; if it
returns text without calls, display it. A 400 history error needs corrected call IDs and message ordering; inspect
`error.param` before resending. Keep tool-execution failures visible rather than dropping a required result.

## Keep max_tokens within what is served

See [free-output thresholds](../reference/service_limits.md#free-output-and-image-work) and
[text/chat payload limits](../reference/service_limits.md#text-and-chat) for the service defaults behind the model
declarations and the refusal messages.

A chat request goes only to a worker whose declared output length is at least the request's `max_tokens`. When the
model's online workers do not declare enough, the Horde lowers `max_tokens` to the largest declared value. The
change is reported in `x_horde.context_adjustments` as a `worker_max_length` entry. A reply can then end with
`finish_reason` `length` before it reaches the `max_tokens` sent by the client.

To tell the user before sending, read the chosen model's card from `GET ${OAI_BASE_URL}/models` and clamp:

```javascript
const card = models.data.find((entry) => entry.id === selectedModel);
const servedMax = card.top_provider.max_completion_tokens;
const maxTokens = Math.max(16, Math.min(userMaxTokens, servedMax));
if (maxTokens < userMaxTokens) notify(`This model's workers serve at most ${servedMax} tokens per reply.`);
```

`max_completion_tokens` is the largest limit declared by any of the model's workers. A value at the maximum can
therefore still wait for the one worker that serves it. A model absent from the list does not have a worker online.
Without the clamp, the user does not see why a reply stopped short unless the frontend reads
`x_horde.context_adjustments`.

When the user selects images or tools, also check `architecture.input_modalities` for `image` and
`supported_parameters` for `tools`, and tell the user before sending a request that cannot be served by any worker.

Verification: with the output length set above the card's maximum, the frontend sends the card's maximum and shows the
notice. If availability changes before submission, handle the resulting error through the same error display.

## Show the served model and the Horde details

Display the response's `model` as the model that generated the reply. In a stream, this field appears on every chunk.
Using the selected model for this status can be misleading when the request allows more than one model.

The finish chunk of a stream, and the body of a non-streamed reply, include `x_horde`:

| Field | Show as |
| --- | --- |
| `worker_name`, `worker_id` | The worker that served the reply. |
| `kudos` | What the request cost. |
| `queue_wait_seconds` | Time spent queued, when present. |
| `estimated_usage` | When true, label the token counts as estimates. |
| `notice` | Present when the worker did not return visible text. Show it in place of an empty reply. |

Verification: the status line shows the response's `model` and the worker name. A reply the worker left empty shows
the notice text.

## Check the integration

| Case | Expected behavior |
| --- | --- |
| Plain streamed turn | Text appears. `[DONE]` ends the turn. |
| Output length above the card's maximum | The request is clamped and the user is told. |
| Model with no online worker | The error message from the stream or the 503 body is shown. |
| A sampler extra in the request | 400. The frontend sends OpenAI keys only. |
| Turn that produces MCP tool calls | Calls collected by `index`, run, replayed with the assistant message, and followed by one result each. A text-only reply skips execution. |
| Tool result message | Includes only `role`, `tool_call_id` and `content`. |
| Empty reply | `x_horde.notice` is shown. |
| Error with `x-should-retry: false` | Not retried automatically. The user decides, since a resend is a new charged request. |

## KoboldAI Lite notes

KoboldAI Lite's OpenAI mode, used with an MCP tool server, needs these changes to work with the front:

| Area | Change |
| --- | --- |
| Sampler extras | Lite sends `rep_pen`, `top_k`, `min_p` and similar keys only when its "send non-standard params" option is on. Leave it off for the Horde's base URL. |
| Stream errors | The parser has to show `data: {"error": ...}` events. Without that change, Lite does not show anything for a request that cannot be served by any worker. |
| Tool calls | The parser has to read `delta.tool_calls` from chunks that also have `delta.content`. |
| Tool messages | Lite adds `name` to tool messages. The front drops it, so this needs no change. |
| Output length | Lite's default output length is 768 tokens. On a model whose workers declare 512, every request from an unadjusted Lite is lowered to 512. Clamp it as in [Keep max_tokens within what is served](#keep-max_tokens-within-what-is-served). |
| Status line | The status line has to read the response's `model` to show the served model. |

## Related

- [Send chat completions through the OpenAI-compatible front](send_chat_completions.md): client setup and examples.
- [Chat front reference](../reference/chat_front.md): the response, stream chunk and model card shapes.
- The [chat requests explanation](../explanation/chat_requests.md): why the front refuses unknown keys and streams
  the reply as deltas for plain requests and whole for tools or structured output.
