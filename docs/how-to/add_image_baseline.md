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

An uncatalogued baseline is priced at par and accepted for plain txt2img requests, so the models can
land before the record does.

## 1. Publish the baseline record

Submit the record through the model reference's baseline resource. Its `capabilities` state what
exists for the family:

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

Answer each capability from what has been published for the architecture, not from the model card.
`horde_policy` states only what departs from par; the fields are described in the
[reference page](../reference/baseline_policy.md#the-horde-policy-on-a-baseline).

The record is served to every replica, so a pricing change reaches the API within the hour without a
deployment.

## 2. Add the bridge row when a release renders a feature

Engine support is not a property of the architecture: it is a property of a bridge release. When a
release adds `hires_fix`, `control_type`, `flow_shift` or `transparent` on the new baseline, add it to
that release's version in `BRIDGE_BASELINE_FEATURES`:

```python
"AI Horde Worker reGen": {
    19: {"flow_shift": frozenset({"my_baseline"})},
},
```

Land the bridge release first. Until the row exists the four features are refused for the baseline,
which is the correct answer: a bridge that ignores the field renders something the request did not ask
for rather than reporting an error.

## 3. Update the reference page

Add the release row to the bridge table in [the reference page](../reference/baseline_policy.md).

## 4. Verify

```bash
pytest tests/unit/test_baseline_policy.py tests/unit/parity
```

The parity suite compares every request shape against the rules this design replaced, so a capability
answered wrongly shows up as an unadjudicated difference rather than as silence.
