---
title: "Serve your first chat job with koboldcpp"
summary: "Start koboldcpp as a Horde chat worker, verify registration and complete a chat request followed by a tool request."
topics: [chat, workers]
order: 20
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Serve your first chat job with koboldcpp

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [chat](../topics.md#chat), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

In this tutorial you run koboldcpp as a chat worker on the AI Horde, watch the horde list its model as a chat model,
send it one chat request and one tool request with curl, and read the structured replies submitted by the worker. Each step
ends with the output that shows it worked.

The procedure behind each step, with every option, is [Serve chat jobs from a text worker](../how-to/serve_chat_jobs.md).
To send chat requests through the OpenAI-compatible front instead of the native route, follow
[Your first chat completion through the Horde](first_chat_completion.md).

## What you need

- koboldcpp 1.124 or later. Its embedded Horde worker serves chat jobs beside text jobs, and pops from
  `https://aihorde.net`, so this tutorial runs there.
- An instruction-tuned GGUF model whose file includes a chat template. A small one (a few billion parameters) is
  enough.
- A registered AI Horde API key, written `<api-key>`. The worker needs one, and the same key can send the requests.
- `curl` and `jq`.

Set the values once in your shell:

```bash
export HORDE_URL=https://aihorde.net
export HORDE_API_KEY=<api-key>
export MODEL_NAME=koboldcpp/<model-name>
```

koboldcpp prefixes its registered model name with `koboldcpp/`, so `MODEL_NAME` is `<model-name>` with that prefix.
Choose a `<model-name>` that is not served by any other worker, so the requests below reach your worker. Leave out the colon: the
OpenAI-compatible front reserves it and does not route to a model whose name contains one.

## 1. Start koboldcpp as a chat worker

```bash
koboldcpp --model <model.gguf> --contextsize 4096 --jinja \
  --hordekey "$HORDE_API_KEY" --hordeworkername <worker-name> \
  --hordemodelname <model-name> --hordemaxctx 4096 --hordegenlen 512
```

`--hordekey` and `--hordeworkername` start the embedded worker, which accepts chat jobs and text jobs. `--jinja` formats
each conversation with the model's Jinja chat template, including tool calls. `--hordemaxctx` and `--hordegenlen` set
the worker's largest accepted context and reply.

Once the model has loaded, the log shows:

```text
Worker <n> - Embedded Horde Worker '<worker-name>' is started.
```

Leave koboldcpp running, and do not use its local web interface during the tutorial: the embedded worker pauses
popping for 20 seconds after each local generation request.

## 2. Watch the worker register as a chat worker

Every worker pop includes the chat declaration. Query the Horde's list of models served by chat workers:

```bash
curl -s "$HORDE_URL/api/v2/status/models?type=text&chat=true" \
  | jq --arg name "$MODEL_NAME" '.[] | select(.name == $name) | .chat'
```

The model list refreshes every few seconds. Repeat the command until it prints:

```json
{
  "workers": 1,
  "max_context_length": 4096,
  "max_length": 512,
  "features": ["json_object", "json_schema", "presence_penalty", "prompt_overflow_refused", "reasoning_effort", "seed", "tools"],
  "created": 1760000000
}
```

`created` differs. The worker declared what it serves: up to 4096 tokens of context, up to 512 tokens of reply, the
optional features `seed`, `presence_penalty`, `tools`, `json_object`, `json_schema` and `reasoning_effort`, and the
guarantee `prompt_overflow_refused`, a promise to fault a conversation that does not fit instead of trimming it. A
request that needs a feature or guarantee not declared by the worker waits for another worker.

The model list merges every worker serving the model. The worker's details hold this worker's declaration alone,
including `limits.tools_max_temperature`, which the model list does not show:

```bash
curl -s "$HORDE_URL/api/v2/workers/name/<worker-name>" | jq .chat
```

## 3. Send a chat request

Send a conversation to the native chat route:

```bash
REQUEST_ID=$(curl -s "$HORDE_URL/api/v2/generate/chat/async" \
  -H "apikey: $HORDE_API_KEY" \
  -H "Client-Agent: chat-tutorial:1:unknown" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"$MODEL_NAME\",
    \"messages\": [
      {\"role\": \"system\", \"content\": \"Answer in one short sentence.\"},
      {\"role\": \"user\", \"content\": \"What colour is a clear daytime sky?\"}
    ],
    \"max_tokens\": 64
  }" | tee /dev/stderr | jq -r .id)
```

The Horde returns `202` with the request id and kudos cost:

```json
{"id": "<request-id>", "kudos": <kudos>}
```

`REQUEST_ID` holds the id.

Within a few seconds the koboldcpp log shows the job arriving and the reply going back:

```text
Job <job-id> received from https://aihorde.net for 64 tokens and <context> max context. Starting generation...
Worker <n> - Submitted <job-id> and earned <kudos> kudos
```

The worker received the conversation as `messages`, rendered it with the model's chat template, and submitted the
reply as an assistant message.

## 4. Read the structured reply

Ask for the request's status:

```bash
curl -s "$HORDE_URL/api/v2/generate/chat/status/$REQUEST_ID" -H "apikey: $HORDE_API_KEY" \
  | jq '{done, generations: [.generations[] | {worker_name, message, finish_reason, prompt_tokens, completion_tokens, charge_basis}]}'
```

When `done` is true, the output looks like:

```json
{
  "done": true,
  "generations": [
    {
      "worker_name": "<worker-name>",
      "message": {"role": "assistant", "content": "A clear daytime sky is blue."},
      "finish_reason": "stop",
      "prompt_tokens": 38,
      "completion_tokens": 9,
      "charge_basis": "output_estimate"
    }
  ]
}
```

While `done` is false, run the command again. Each field came from the worker's submit:

- `message` is the reply as an assistant message, the format used by OpenAI clients.
- `finish_reason` is `stop` because the model ended the reply. `length` would mean it used all 64 tokens.
- `prompt_tokens` and `completion_tokens` are the counts koboldcpp reported.
- `charge_basis` `output_estimate` says the kudos were computed from the length of the reply text, as for any text
  request, and the token counts did not enter the charge.

## 5. Send a tool request

Offer the model one function and require it to call a tool:

```bash
TOOL_REQUEST_ID=$(curl -s "$HORDE_URL/api/v2/generate/chat/async" \
  -H "apikey: $HORDE_API_KEY" \
  -H "Client-Agent: chat-tutorial:1:unknown" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"$MODEL_NAME\",
    \"messages\": [{\"role\": \"user\", \"content\": \"What is the weather in Paris?\"}],
    \"tools\": [{
      \"type\": \"function\",
      \"function\": {
        \"name\": \"get_weather\",
        \"description\": \"Current weather for a city\",
        \"parameters\": {\"type\": \"object\", \"properties\": {\"city\": {\"type\": \"string\"}}, \"required\": [\"city\"]}
      }
    }],
    \"tool_choice\": \"required\",
    \"max_tokens\": 128,
    \"horde\": {\"conformance\": \"best_effort\"}
  }" | jq -r .id)
```

The request needs the `tools` feature, which this worker declared in step 2. A request is `strict` by default, and a
strict `tool_choice: "required"` also needs the guarantee `tool_choice_enforced`, which koboldcpp does not declare.
`best_effort` drops the guarantees, so the request reaches your worker. The horde then does not check that the reply
calls a tool, and a model that answers in text instead returns `content` and `finish_reason` `stop`. Read the status
as before:

```bash
curl -s "$HORDE_URL/api/v2/generate/chat/status/$TOOL_REQUEST_ID" -H "apikey: $HORDE_API_KEY" \
  | jq '{done, generations: [.generations[] | {message, finish_reason}]}'
```

When `done` is true and the model produced a tool call, the reply looks like this:

```json
{
  "done": true,
  "generations": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {"id": "<call-id>", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}}
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

The model chose `get_weather` and wrote its arguments as a JSON string. `content` is null because the model did not
write text beside the call. Your application runs the function, then sends the conversation back with the assistant
message above and a `tool` message whose `tool_call_id` is `<call-id>`, and the model answers from the result.

## What you built

A text worker that also serves chat jobs. The horde routed both requests to it because its pop declared chat jobs,
the model, the limits and the required features. You read the worker's structured replies through the status route.
The worker still serves ordinary text requests for the same model.

Next:

- [Serve chat jobs from a text worker](../how-to/serve_chat_jobs.md) for every declaration field, images, and how a
  worker faults a job it cannot serve.
- [Send chat completions through the OpenAI-compatible front](../how-to/send_chat_completions.md) to reach the same
  worker from an OpenAI client.
- [Chat requests on the Horde](../explanation/chat_requests.md) for why chat requests travel this way.
- [Chat requests reference](../reference/chat_requests.md) for every route and field.
