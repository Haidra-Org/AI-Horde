# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from horde.flask import create_app, db


@pytest.fixture(scope="session")
def app():
    """Create a Flask application for testing with an in-memory SQLite database."""
    app = create_app(
        config={
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        },
    )
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def db_session(app):
    """Database session that rolls back after each test."""
    with app.app_context():
        yield db.session
        db.session.rollback()
