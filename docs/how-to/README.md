<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# How-to

Procedures for an operator or contributor who already knows what they want to achieve:
the ordered steps, the checks between them, and the way back if a step fails.

## Documents

<!-- BEGIN GENERATED: documents (gen_doc_index.py) -->
| Document | Summary |
| --- | --- |
| [Kudos ledger operations](kudos_ledger_operations.md) | Operator procedures for the kudos ledger: mode cutover, health checks, rollback, and recovery. |
| [Add extended image controls to a frontend](extended_image_frontend.md) | Integrate sampler discovery, schedules, solver controls, expanded ControlNet types, and control-map annotations into an existing image frontend. |
| [Add a sampler, scheduler, solver control, or annotator](add_sampler_or_annotator.md) | Extend the image sampler and control-map vocabularies across hordelib, horde_sdk, AI-Horde, and the reGen bridge, in the order that keeps them compatible. |
| [Add an image baseline](add_image_baseline.md) | Publish the baseline record on the model reference, and add a bridge row only when a release adds the engine support. |
| [Use the style endpoints](use_style_endpoints.md) | Read the published style contract, create text and image styles that declare a policy and placeholders, and send requests under them. |
| [Add style authoring and styled requests to a frontend](add_styles_to_a_frontend.md) | Build style authoring controls from the published style contract, build request inputs from a style's declarations, preview with a dry run, and handle collections and style errors. |
| [Update an existing style integration](update_a_style_integration.md) | The changes in AI Horde 5.1.13 that a client already using styles can observe, what to change for each, and how to check it. |
| [Run the OpenAI-compatible chat front](run_openai_chat_front.md) | Configure and start the horde.oai process that serves /v1/chat/completions and /v1/models in front of a horde, then check it with a real request. |
| [Send chat completions through the OpenAI-compatible front](send_chat_completions.md) | Point an existing OpenAI client at the Horde: base URL, key and attribution, model choice, accepted and refused keys, the horde object, images, tools, structured output, streams, errors and limits. |
| [Add chat completions to a web frontend](add_chat_to_a_frontend.md) | Call the Horde's chat front from a browser: bearer key over CORS, OpenAI-only request keys, a stream parser that reads content, tool calls and error events, the tool loop, and model card limits. |
| [Serve chat jobs from a text worker](serve_chat_jobs.md) | Declare chat on text pop, recognise a chat job, render and fit it, serve tools, structured output and images, and submit the structured reply, with koboldcpp's embedded worker as the worked example. |
<!-- END GENERATED: documents -->
