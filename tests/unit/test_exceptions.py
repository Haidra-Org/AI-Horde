# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later
import inspect
import re

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

    def test_every_return_code_in_the_source_is_known(self):
        """A return code is part of the API contract: every literal the source can emit must be in KNOWN_RC."""
        import re
        from pathlib import Path

        source_root = Path(__file__).resolve().parents[2] / "horde"
        literal = re.compile(r'(?:rc=|"rc": ?)"([A-Za-z0-9_.]+)"|return \("([A-Za-z0-9_.]+)",')
        emitted = {
            match.group(1) or match.group(2)
            for source_file in source_root.rglob("*.py")
            for match in literal.finditer(source_file.read_text(encoding="utf-8"))
        }
        assert emitted, "no return-code literals found; the scan pattern is broken"
        assert emitted <= set(KNOWN_RC), f"return codes emitted but not in KNOWN_RC: {sorted(emitted - set(KNOWN_RC))}"

    def test_return_codes_are_bare_identifiers(self):
        for rc in KNOWN_RC:
            assert re.fullmatch(r"[A-Za-z0-9_]+", rc), f"return code is not a bare identifier: {rc!r}"
