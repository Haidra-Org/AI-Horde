---
title: "Add chat completions to a web frontend"
summary: "Call the Horde's chat front from a browser: bearer key over CORS, OpenAI-only request keys, a stream parser that reads content, tool calls and error events, the tool loop, and model card limits."
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

Preconditions: a browser or app frontend that can send `fetch` requests and read a streamed response body; the base
URL of a Horde chat front, written here as `OAI_BASE_URL` (for the public horde, `https://oai.aihorde.net/v1`); and
the user's Horde API key, or the anonymous key `0000000000`.

End state: the frontend sends chat completions straight from the browser, streams the reply, runs tool calls in a
loop, keeps `max_tokens` within what the chosen model's workers serve, and shows the user the served model, the Horde
details and any error.

The worked example is KoboldAI Lite's OpenAI mode with an MCP tool server. Where Lite needs a change to work with the
front, the section says so. The request contract itself is in
[Send chat completions through the OpenAI-compatible front](send_chat_completions.md).

## Call the front from the browser

The front answers CORS preflights for every origin and marks every response `Access-Control-Allow-Origin: *`, with
`Retry-After` and `x-should-retry` exposed to scripts. A frontend calls it directly with the user's key, without a
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
`Referer` the browser sends.

Fetch the model list the same way, from `GET ${OAI_BASE_URL}/models`.

Verification: in the browser's network panel, the `OPTIONS` preflight returns 200 with
`access-control-allow-origin: *`, and the `POST` that follows carries the same header.

Reversal: remove the endpoint from the frontend's configuration; the Horde does not store anything for it.

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
`param` holding the first refused key. Lite sends them only when its "send non-standard params" option is on, so
leave such options off for this endpoint, or drop the keys when the base URL is the Horde's. The front sends the worker
neutral values for every sampler it does not receive.

Verification: a request built from the default settings returns 200. Turning on a sampler extra returns 400 with that
key in `error.param`.

## Parse the stream

A streamed reply is server-sent events: keepalive comment lines starting with `:` while the request waits, then
`data:` lines carrying JSON chunks, ending with `data: [DONE]`. A parser has to handle three cases in every `data:`
payload:

- A top-level `error`, `{"error": {"message", "type", "param", "code", "metadata"}}`. Show `error.message` to the user
  and end the turn. This is how the front reports a failure after the 200 headers went out, including a request no
  worker could serve.
- `choices[0].delta.content`, text to append.
- `choices[0].delta.tool_calls`, tool calls to collect by `index`.

The front sends content and tool calls in separate chunks, but a parser reads both fields from every chunk, since
other OpenAI-compatible services send both in one.

```javascript
async function readChatStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let served = null;
  const calls = [];

  for (;;) {
    const { value, done } = await reader.read();
    if (done) return { text, calls, served };
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

Lite's parser needs two changes for this: it has to show `data: {"error": ...}` events, without which Lite does not
show anything for an ineligible request, and it has to read `delta.tool_calls` from chunks that also carry
`delta.content`.

Verification: a plain turn yields text and no calls; a turn that needs a tool yields one call with a name and JSON
arguments; a request for a model with no online worker shows the error message.

## Implement the tool loop

1. Offer the tools in `tools`. An MCP server's tool list maps one to one:

   ```javascript
   const tools = mcpTools.map((tool) => ({
     type: "function",
     function: { name: tool.name, description: tool.description, parameters: tool.inputSchema },
   }));
   ```

   Send `tools` only to a model whose card lists `tools` in `supported_parameters`.

2. When the stream ends with calls (`finish_reason` `tool_calls`), run each call with its parsed `arguments`.

3. Append the assistant turn and one result per call, then send the history again with the same `tools`:

   ```javascript
   messages.push({ role: "assistant", content: null, tool_calls: calls });
   for (const call of calls) {
     messages.push({ role: "tool", tool_call_id: call.id, content: resultFor(call) });
   }
   ```

4. Repeat until a reply does not carry calls, and show its text.

A `tool` message carries `role`, `tool_call_id` and `content`; the result is a string, so serialise structured
results as JSON. Any other key on it, such as the `name` Lite adds to tool messages, is dropped and not forwarded, as
OpenAI ignores it. Each `tool_call_id` has to be the `id` of a call in an earlier assistant message of the same
request.

Verification: the follow-up request returns 200 and a reply with text that uses the tool result and no further calls.

## Keep max_tokens within what is served

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

`max_completion_tokens` is the largest any of the model's workers declares, so a value at the maximum can still wait
for the one worker that serves it. A model absent from the list does not have a worker online. Lite's default output
length is 768 tokens. On a model whose workers declare 512, every request from an unadjusted Lite is lowered to
512. The user does not see why unless the frontend reads `x_horde.context_adjustments`.

When the user selects images or tools, also check `architecture.input_modalities` for `image` and
`supported_parameters` for `tools`, and tell the user before sending a request no worker can serve.

Verification: with the output length set above the card's maximum, the frontend sends the card's maximum and shows the
notice; the request is served.

## Show the served model and the Horde details

The response's `model`, on every chunk of a stream, is the model that generated the reply. Show it in place of the
model the user picked, since a status line built from the selection can disagree with what served the request. Lite's
OpenAI mode needs a change to read `model` for its status line.

The finish chunk of a stream, and the body of a non-streamed reply, carry `x_horde`:

| Field | Show as |
| --- | --- |
| `worker_name`, `worker_id` | The worker that served the reply. |
| `kudos` | What the request cost. |
| `queue_wait_seconds` | Time spent queued, when present. |
| `estimated_usage` | When true, label the token counts as estimates. |
| `notice` | Present when the worker returned no visible text; show it in place of an empty reply. |

Verification: the status line shows the response's `model` and the worker name; a reply the worker left empty shows
the notice text.

## Check the integration

| Case | Expected behavior |
| --- | --- |
| Plain streamed turn | Text appears; `[DONE]` ends the turn. |
| Output length above the card's maximum | The request is clamped and the user is told. |
| Model with no online worker | The error message from the stream or the 503 body is shown. |
| A sampler extra in the request | 400; the frontend sends OpenAI keys only. |
| Turn that needs an MCP tool | One call collected by `index`, run, replayed with `content: null`, and answered. |
| Tool result message | Carries only `role`, `tool_call_id` and `content`. |
| Empty reply | `x_horde.notice` is shown. |
| Error with `x-should-retry: false` | Not retried automatically; the user decides, since a resend is a new charged request. |

## Related

- [Send chat completions through the OpenAI-compatible front](send_chat_completions.md): every accepted and refused
  key, the `horde` object, images, errors and limits.
- [Chat front reference](../reference/chat_front.md): the response, stream chunk and model card shapes.
- The [chat requests explanation](../explanation/chat_requests.md): why the front refuses unknown keys and streams
  the reply as deltas for plain requests and whole for tools or structured output.
