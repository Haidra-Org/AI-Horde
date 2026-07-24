# SPDX-License-Identifier: AGPL-3.0-or-later

from grid_api.routers.worker_ws import _can_connect_worker, _worker_key_matches_name


def test_worker_connection_accepts_narrow_grid_scopes():
    assert _can_connect_worker({"source": "v2", "scopes": ["worker.connect"]})
    assert _can_connect_worker({"source": "v2", "scopes": ["inference.submit"]})
    assert not _can_connect_worker({"source": "legacy", "scopes": []})


def test_worker_connection_rejects_unrelated_v2_keys():
    assert not _can_connect_worker({"source": "v2", "scopes": ["account.read"]})


def test_manager_worker_key_is_bound_to_its_rig_name():
    worker_key = {
        "source": "v2",
        "key_kind": "worker",
        "key_label": "worker:audio-rig-1",
    }
    assert _worker_key_matches_name(worker_key, "audio-rig-1")
    assert not _worker_key_matches_name(worker_key, "another-rig")
    assert _worker_key_matches_name(
        {"source": "v2", "key_kind": "user", "key_label": "default"},
        "another-rig",
    )
