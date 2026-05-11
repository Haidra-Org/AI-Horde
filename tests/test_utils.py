# SPDX-FileCopyrightText: 2026 Guillem
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from horde.utils import get_random_seed


def test_get_random_seed_uses_start_point_as_lower_bound(monkeypatch):
    monkeypatch.setattr("horde.utils.secrets.randbelow", lambda upper_bound: upper_bound - 1)

    assert get_random_seed(10) == 2**32 - 1


def test_get_random_seed_allows_maximum_start_point(monkeypatch):
    monkeypatch.setattr("horde.utils.secrets.randbelow", lambda upper_bound: 0)

    assert get_random_seed(2**32 - 1) == 2**32 - 1


def test_get_random_seed_rejects_start_point_above_seed_range():
    with pytest.raises(ValueError):
        get_random_seed(2**32)
