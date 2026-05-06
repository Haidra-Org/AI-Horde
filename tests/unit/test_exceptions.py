# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import inspect

from werkzeug import exceptions as wze

from horde import exceptions as e
from horde.exceptions import KNOWN_RC


class TestExceptionInstantiation:
    """Verify all exception classes can be instantiated without errors.

    This catches breaking changes in werkzeug's exception hierarchy during version bumps.
    """

    def test_bad_request(self):
        exc = e.BadRequest("test message")
        assert exc.specific == "test message"
        assert exc.rc == "BadRequest"

    def test_forbidden(self):
        exc = e.Forbidden("forbidden message")
        assert exc.specific == "forbidden message"
        assert exc.rc == "Forbidden"

    def test_locked(self):
        exc = e.Locked("locked message")
        assert exc.specific == "locked message"

    def test_missing_prompt(self):
        exc = e.MissingPrompt("testuser")
        assert exc.rc == "MissingPrompt"
        assert "empty prompt" in exc.specific

    def test_kudos_validation_error(self):
        exc = e.KudosValidationError("testuser", "not enough kudos")
        assert exc.rc == "KudosValidationError"
        assert exc.specific == "not enough kudos"

    def test_no_valid_actions(self):
        exc = e.NoValidActions("nothing to do")
        assert exc.rc == "NoValidActions"


class TestExceptionHierarchy:
    """Verify exception classes inherit from werkzeug HTTP exceptions."""

    def test_all_exception_classes_are_http_exceptions(self):
        exception_classes = [
            obj
            for name, obj in inspect.getmembers(e)
            if inspect.isclass(obj) and issubclass(obj, Exception) and obj.__module__ == e.__name__
        ]
        assert len(exception_classes) > 10
        for cls in exception_classes:
            assert issubclass(cls, wze.HTTPException), f"{cls.__name__} does not inherit from HTTPException"


class TestKnownRC:
    """Verify KNOWN_RC list integrity."""

    def test_not_empty(self):
        assert len(KNOWN_RC) > 0

    def test_all_strings(self):
        for rc in KNOWN_RC:
            assert isinstance(rc, str), f"KNOWN_RC entry is not a string: {rc}"

    def test_no_duplicates(self):
        assert len(KNOWN_RC) == len(set(KNOWN_RC)), "KNOWN_RC contains duplicate entries"
