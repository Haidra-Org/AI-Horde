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
<!-- END GENERATED: documents -->
