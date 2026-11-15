---
title: "Add an image baseline"
summary: "Publish the baseline record on the model reference, and add a bridge row only when a release adds the engine support."
topics: [generation, kudos, workers]
order: 40
---

<!--
SPDX-FileCopyrightText: 2026 Tazlin

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Add an image baseline

<!-- BEGIN GENERATED: topics (gen_doc_index.py) -->
Topics: [generation](../topics.md#generation), [kudos](../topics.md#kudos), [workers](../topics.md#workers)
<!-- END GENERATED: topics -->

Adding a *model* on an existing baseline needs no change here: publish it to the model reference and
the service picks it up. A new *architecture* needs a baseline record published on models.aihorde.net,
and a row in `horde/bridge_reference.py` only once a bridge release renders something on it. The
[baseline policy reference](../reference/baseline_policy.md) documents what each authority decides.

An uncatalogued baseline uses a pricing multiplier of 1 (par) and accepted for plain txt2img requests, so the models can
land before the record does.

Before starting, have access to publish baseline records on the model reference and a checkout of this
repository for any bridge-gating changes. After publication and refresh, the API uses the baseline's capabilities
and policy. New bridge features remain gated by the release that implements them.

## 1. Publish the baseline record

Use the model-reference PRIMARY's v2 API, separate from the Horde generation API. You need a Horde key on that
service's pending-queue requestor allowlist, and an approver must review the proposal before publication. The
service must have writes and its pending queue enabled. Its deployed API schema is authoritative for its version.

The Bash examples require `curl` and `jq`. Set `MODEL_REFERENCE_API` to the service API root, for example
`https://models.aihorde.net/api`, and `APIKEY` to your requestor key. Save the current catalog before any change:

```bash
curl -fsS "$MODEL_REFERENCE_API/model_references/v2/image_generation/baselines/export" -o catalog-before.json
jq -e 'has("baselines") and has("metadata")' catalog-before.json
```

Stop on a failed read or malformed response. Retain this file for comparison and recovery. Save the proposed record
as `baseline-after.json`; its `capabilities` state what exists for the family. This example's policy values are
illustrative and need review for the architecture:

```json
{
  "name": "my_baseline",
  "display_name": "My Baseline",
  "native_resolution": 1024,
  "capabilities": {
    "controlnet": false,
    "controlnet_types_unavailable": [],
    "transparent": false,
    "qr_code": false,
    "remix": false,
    "flow_matching": true
  },
  "horde_policy": {
    "kudos": 8,
    "batching": 8,
    "ttl": 3,
    "resolution_floor": 1024
  }
}
```

Set each capability from the weights and mechanisms published for the architecture.
`horde_policy` states only what departs from the default multiplier of 1 (par). The fields are described in the
[reference page](../reference/baseline_policy.md#the-horde-policy-on-a-baseline).

Build a change set with the saved record as `expected_before`. A new name has no previous record, so that field
is null. Do not replace an existing record's expected value with null to bypass a conflict.

```bash
jq -n --slurpfile catalog catalog-before.json --slurpfile record baseline-after.json '
  {title: "Update image baseline", changes: [{operation: "upsert", name: $record[0].name,
    record: $record[0], expected_before: ($catalog[0].baselines[$record[0].name] // null)}]}
' > baseline-change.json
curl -fsS -X POST "$MODEL_REFERENCE_API/model_references/v2/image_generation/baselines/change-sets" \
  -H "apikey: $APIKEY" -H "Content-Type: application/json" \
  --data-binary @baseline-change.json
```

A 202 response is a queued proposal, not publication. Retain its returned queue record and have an authorized
approver review it through the PRIMARY's pending queue. A 401/403 needs credential or allowlist diagnosis;
a 409 needs a new catalog read and review of the competing change, rather than a blind resend.
After approval, read `/model_references/v2/image_generation/baselines/<baseline>` from that API root and compare
the published record with the proposal before checking Horde replicas.

Healthy fleet publication normally refreshes hourly, without a Horde deployment. Fetch, validation or Redis
failures retain the previous snapshot. If the change does not appear, inspect
[snapshot health](../reference/model_reference.md#snapshot-health) and publisher logs.

Verification: after refresh, `model_reference.baseline_record("<baseline>")` on an API instance returns the
published capabilities and policy. To reverse a policy change, propose the saved previous record through the same
queue, using the currently published record as `expected_before`, then verify approval and fleet refresh. This
changes future policy; it does not reverse completed charges. Keep a new baseline record while published models
reference it rather than treating deletion as an automatic undo.

## 2. Add the bridge row when a release renders a feature

Engine support is not a property of the architecture: it is a property of a bridge release. When a
release adds `hires_fix`, `control_type`, `flow_shift` or `transparent` on the new baseline, add it to
that release's version in `BRIDGE_BASELINE_FEATURES`:

```python
"AI Horde Worker reGen": {
    19: {"flow_shift": frozenset({"my_baseline"})},
},
```

Land the bridge release first. Until the row exists, these bridge-gated features are refused for the baseline.
A bridge that ignores a requested field can otherwise generate an incorrect result without an error.

Verification: extend `TestBridgeSupport` in `tests/unit/test_baseline_policy.py` to check the feature below and
at the supported version. Reverse the gate by removing the new entry. This affects new assignments and does not
recall jobs already dispatched.

## 3. Update the reference page

Add the release row to the bridge table in [the reference page](../reference/baseline_policy.md).

Verification: the documented version, feature and baseline match `BRIDGE_BASELINE_FEATURES`.

## 4. Verify

```bash
pytest tests/unit/test_baseline_policy.py tests/unit/parity
```

Verification: both suites pass. The parity suite compares request shapes against the previous rules and reports
differences that require review, including incorrectly declared capabilities.
