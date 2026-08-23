import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("LOCUST_SKIP_MONKEY_PATCH", "1")

from tests.stress.locustsuite.events import _remove_tag_filtered_user_classes


def _user_class(name: str, *, tasks: list[object], fixed_count: int = 0):
    return type(name, (), {"tasks": tasks, "fixed_count": fixed_count})


def test_tag_filtered_user_classes_are_removed_before_spawning():
    active = _user_class("ActiveUser", tasks=[object()])
    filtered = _user_class("FilteredUser", tasks=[])
    environment = SimpleNamespace(user_classes=[active, filtered])

    _remove_tag_filtered_user_classes(environment)

    assert environment.user_classes == [active]


def test_tag_filter_rejects_explicit_fixed_users_without_tasks():
    filtered = _user_class("FilteredUser", tasks=[], fixed_count=2)
    environment = SimpleNamespace(user_classes=[filtered])

    with pytest.raises(RuntimeError, match="2 fixed users"):
        _remove_tag_filtered_user_classes(environment)
