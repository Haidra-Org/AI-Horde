# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Smoke tests that verify horde modules can be imported without triggering
side effects (Redis connections, background threads, etc.).
"""


def test_import_utils():
    from horde import utils  # noqa: F401


def test_import_consts():
    from horde import consts  # noqa: F401


def test_import_exceptions():
    from horde import exceptions  # noqa: F401


def test_import_vars():
    from horde import vars  # noqa: F401


def test_flask_extensions_importable():
    """db and cache should be importable without a live app."""
    from horde.flask import cache, db

    assert db is not None
    assert cache is not None
