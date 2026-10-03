# SPDX-FileCopyrightText: 2026 Tazlin <tazlin@haidra.net>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a text style changes the request it is applied to.

A style brings its own prompt template, params and model list, and a request that uses one generates
under all three. The cases here pin that down from the outside: what a worker is handed after a
style has been applied, how many takes the request still gets to choose, and what the quote comes to.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from tests.fixture_types import MakeApiUser

TEXT_MODELS = ["elinas/chronos-70b-v2"]

STYLE_PROMPT = "### Instruction:\n{p}\n\n### Response:\n"
STYLE_PARAMS: dict[str, Any] = {"max_length": 80, "max_context_length": 1024, "temperature": 0.7}

STYLE_KUDOS_SURCHARGE = 2
"""What a request pays on top of its params for generating under someone else's style."""

TEMPLATE_FIELDS = [
    {"name": "caption", "description": "What the picture shows", "required": True},
    {"name": "tags", "description": "Comma-separated tags", "required": False},
]

STYLED_REQUEST_COUNT = 20
"""How many requests the author credit record cases queue under one style."""

ACTIVATION_HORDE_TAX = 1
"""The base horde tax a text request pays when it is queued, before any style surcharge."""

ANONYMOUS_API_KEY = "0000000000"
"""The public key anonymous requests are submitted with."""

ANONYMOUS_USER_ID = 0
"""The id of the user anonymous requests are charged to."""

WORKER_NAME = "CICD Style Scribe"

WORKER_MAX_CONTEXT_LENGTH = 4096
"""The largest context the workers these cases check in advertise."""


@pytest.fixture(autouse=True, scope="module")
def _no_rate_limit() -> Iterator[None]:
    """Turn the per-path rate limit off for this module.

    Creating a style is capped at two calls a second and the generate endpoints at ten, which these
    cases would run through.

    Yields:
        Nothing. The previous setting is put back afterwards.
    """
    from horde.limiter import limiter

    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def style_body(name: str, **overrides: Any) -> dict[str, Any]:
    """Build the body that creates a text style.

    Args:
        name: The style name, which is unique per user.
        **overrides: Keys to add to or replace in the body.

    Returns:
        The creation body.
    """
    body: dict[str, Any] = {
        "name": name,
        "info": "A style used by the text style application tests.",
        "prompt": STYLE_PROMPT,
        "params": dict(STYLE_PARAMS),
        "models": TEXT_MODELS,
        "public": True,
        "nsfw": False,
    }
    body.update(overrides)
    return body


@contextmanager
def created_style(
    client: FlaskClient,
    request_headers: dict[str, str],
    body: dict[str, Any],
) -> Iterator[str]:
    """Create a text style for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the style belongs to.
        body: The creation body.

    Yields:
        The id of the new style.
    """
    response = client.post("/api/v2/styles/text", json=body, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    style_id = response.get_json()["id"]
    try:
        yield style_id
    finally:
        client.delete(f"/api/v2/styles/text/{style_id}", headers=request_headers)


def post_request(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> TestResponse:
    """Send a text request and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt and the model list.

    Returns:
        The response, so a case can assert on a rejection as well as on a queued request.
    """
    request_body: dict[str, Any] = {
        "prompt": "a horde of cute stable robots repairing a mainframe",
        "models": TEXT_MODELS,
        "trusted_workers": True,
        "validated_backends": False,
    }
    request_body.update(body)
    return client.post("/api/v2/generate/text/async", json=request_body, headers=request_headers)


@contextmanager
def queued_request(
    client: FlaskClient,
    request_headers: dict[str, str],
    **body: Any,
) -> Iterator[str]:
    """Queue a text request for the duration of the block and cancel it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt and the model list.

    Yields:
        The id of the queued request.
    """
    response = post_request(client, request_headers, **body)
    assert response.status_code == 202, response.get_data(as_text=True)
    request_id = response.get_json()["id"]
    try:
        yield request_id
    finally:
        client.delete(f"/api/v2/generate/text/status/{request_id}", headers=request_headers)


def pop_payload(
    client: FlaskClient,
    request_headers: dict[str, str],
    *,
    worker_name: str = WORKER_NAME,
    models: list[str] = TEXT_MODELS,
) -> dict[str, Any]:
    """Take one job off the text queue as a worker and return what the worker was handed.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the worker owner's API key.
        worker_name: The name the worker checks in under.
        models: The models the worker serves.

    Returns:
        The payload of the popped job.
    """
    response = client.post(
        "/api/v2/generate/text/pop",
        json={
            "name": worker_name,
            "models": models,
            "bridge_agent": request_headers["Client-Agent"],
            "amount": 1,
            "max_context_length": WORKER_MAX_CONTEXT_LENGTH,
            "max_length": 512,
        },
        headers=request_headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    popped = response.get_json()
    assert popped["id"] is not None, popped
    return popped["payload"]


def post_dry_run(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> TestResponse:
    """Ask for a quote on a text request and return the raw response.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt, the model list and the dry run flag.

    Returns:
        The response, so a case can assert on the resolved request as well as on the quote.
    """
    return post_request(client, request_headers, dry_run=True, **body)


def dry_run_kudos(client: FlaskClient, request_headers: dict[str, str], **body: Any) -> float:
    """Ask for a quote on a text request and return it.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the requesting user's API key.
        **body: The request body, on top of a prompt, the model list and the dry run flag.

    Returns:
        The quoted kudos.
    """
    response = post_dry_run(client, request_headers, **body)
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["kudos"]


def settled_kudos(app: Flask, settle_kudos: Callable[[], int], user_id: int) -> float:
    """Fold every pending kudos posting and return the user's balance.

    Args:
        app: The Flask app, for the database session.
        settle_kudos: The fixture helper that folds pending ledger postings.
        user_id: The user whose balance to read.

    Returns:
        The user's balance once everything pending has been applied.
    """
    from horde.database import functions as database

    settle_kudos()
    with app.app_context():
        return database.find_user_by_id(user_id).kudos


def style_record(client: FlaskClient, user_id: int) -> float:
    """Return the user's text style record as the user details endpoint reports it.

    The endpoint caches a user's details for a short while, so a case reads it once, after settling.

    Args:
        client: The Flask test client.
        user_id: The user whose record to read.

    Returns:
        How many requests have run under the user's text styles, or 0 when none has.
    """
    response = client.get(f"/api/v2/users/{user_id}")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["records"].get("style", {}).get("text", 0)


@contextmanager
def balance_pinned(app: Flask, settle_kudos: Callable[[], int], user_id: int, *, above_floor: int) -> Iterator[None]:
    """Pin a user's balance to a set distance above their minimum for the block, and put it back afterwards.

    Everything pending is folded first, so no earlier posting shifts the pinned balance. The balance is written
    directly, which no ledger posting records, so the floor arithmetic of the cases is exact.

    Args:
        app: The Flask app, for the database session.
        settle_kudos: The fixture helper that folds pending ledger postings.
        user_id: The user whose balance to pin.
        above_floor: How far above the user's minimum balance to pin it.

    Yields:
        Nothing. The previous balance is written back afterwards.
    """
    from horde.database import functions as database
    from horde.flask import db

    settle_kudos()
    with app.app_context():
        user = database.find_user_by_id(user_id)
        previous_balance = user.kudos
        user.kudos = user.get_min_kudos() + above_floor
        db.session.commit()
    try:
        yield
    finally:
        settle_kudos()
        with app.app_context():
            user = database.find_user_by_id(user_id)
            user.kudos = previous_balance
            db.session.commit()


@pytest.fixture
def shadow_mode(app: Flask) -> Iterator[None]:
    """Run the case with kudos movements projected inline, and put the ledger mode back afterwards.

    Yields:
        Nothing.
    """
    from horde.classes.base.kudos import set_kudos_ledger_mode
    from horde.enums import KudosLedgerMode

    with app.app_context():
        set_kudos_ledger_mode(KudosLedgerMode.SHADOW)
    try:
        yield
    finally:
        with app.app_context():
            set_kudos_ledger_mode(KudosLedgerMode.LEDGER)


class TestTextStyleParams:
    """Which params reach the worker when a style is applied."""

    def test_the_styles_params_replace_the_requests(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text apply params")) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "max_context_length": 4096, "temperature": 1.4},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == STYLE_PARAMS["max_length"]
                assert payload["max_context_length"] == STYLE_PARAMS["max_context_length"]
                assert payload["temperature"] == STYLE_PARAMS["temperature"]

    def test_the_prompt_is_formatted_into_the_template(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text apply prompt")) as style_id:
            with queued_request(client, request_headers, prompt="describe a lighthouse", style=style_id):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == "### Instruction:\ndescribe a lighthouse\n\n### Response:\n"

    def test_the_number_of_takes_comes_from_the_request(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text apply takes")) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"n": 2}) as request_id:
                status = client.get(f"/api/v2/generate/text/status/{request_id}", headers=request_headers)

                assert status.status_code == 200, status.get_data(as_text=True)
                assert status.get_json()["waiting"] == 2


class TestTextStyleQuote:
    """What a quote for a styled request comes to."""

    def test_a_dry_run_returns_a_quote(self, client, request_headers: dict[str, str]) -> None:
        quote = dry_run_kudos(client, request_headers, params={"max_length": 80, "max_context_length": 1024})

        assert quote > 0

    def test_another_users_style_adds_a_surcharge(
        self,
        client,
        request_headers: dict[str, str],
        make_api_user,
    ) -> None:
        # The baseline sends exactly the params the style carries, so the only difference between the
        # two quotes is the style itself. The styled request sends a temperature no other case uses:
        # quotes are cached against the params they were computed from, and temperature changes that
        # key without changing the price, which comes from max_length, max_context_length and the model.
        unstyled_quote = dry_run_kudos(client, request_headers, params=dict(STYLE_PARAMS))

        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text apply surcharge")) as style_id:
            styled_quote = dry_run_kudos(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "temperature": 1.3},
            )

            assert styled_quote == unstyled_quote + STYLE_KUDOS_SURCHARGE

    def test_your_own_style_adds_no_surcharge(self, client, request_headers: dict[str, str]) -> None:
        # The surcharge pays the style's author, so a request generating under its own author's style
        # has nobody to pay and is quoted at what its params come to.
        unstyled_quote = dry_run_kudos(client, request_headers, params=dict(STYLE_PARAMS))

        with created_style(client, request_headers, style_body("text apply own style")) as style_id:
            styled_quote = dry_run_kudos(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "temperature": 1.1},
            )

            assert styled_quote == unstyled_quote

    def test_an_unstyled_quote_after_a_styled_one_carries_no_surcharge(
        self,
        client,
        request_headers: dict[str, str],
        make_api_user,
    ) -> None:
        """An unstyled dry run is not answered with the cached quote of a styled one for the same params.

        The styled dry run stores its quote against the params the style resolved to, which the unstyled
        request then sends unchanged. A styled dry run skips the cache lookup, so only this order can
        return a cached quote across the two.
        """
        # Sending n makes the resolved params identical to the request's, and a temperature no other
        # case uses keeps an earlier quote out of the cache.
        shared_params = {**STYLE_PARAMS, "temperature": 0.83, "n": 1}
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text quote cache", params=shared_params)) as style_id:
            styled = post_dry_run(client, request_headers, style=style_id, params=shared_params)
            assert styled.status_code == 200, styled.get_data(as_text=True)
            assert styled.get_json()["resolved"]["params"] == shared_params
            assert styled.get_json()["resolved"]["models"] == TEXT_MODELS

            unstyled_quote = dry_run_kudos(client, request_headers, params=shared_params)

            assert unstyled_quote == styled.get_json()["kudos"] - STYLE_KUDOS_SURCHARGE


class TestTextStyleAuthorCredit:
    """When the author of a style is paid for a request that runs under it."""

    def test_a_queued_request_pays_the_author_the_surcharge(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text credit queued")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, request_headers, style=style_id):
                assert settled_kudos(app, settle_kudos, owner.id) == balance_before + STYLE_KUDOS_SURCHARGE

    def test_a_dry_run_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A quote includes the surcharge, but asking for one queues nothing and so pays nobody."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text credit dry run")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            for _ in range(3):
                dry_run_kudos(client, request_headers, style=style_id)

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before

    def test_a_refused_request_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A request refused after the style resolved, here for carrying extra source images, pays no one."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text credit refused")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            refused = post_request(
                client,
                request_headers,
                style=style_id,
                extra_source_images=[{"image": "aGVsbG8="}],
            )
            assert refused.status_code == 400, refused.get_data(as_text=True)
            assert refused.get_json()["rc"] == "InvalidExtraSourceImages"

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before

    def test_every_queued_request_counts_once_in_the_authors_style_record(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Each queued request adds one to the author's text style record and the surcharge to their balance.

        Each request is cancelled before the next is queued. Cancelling leaves the author's credit and record
        in place.
        """
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text credit record")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            for _ in range(STYLED_REQUEST_COUNT):
                with queued_request(client, request_headers, style=style_id):
                    pass

            balance_after = settled_kudos(app, settle_kudos, owner.id)
            assert style_record(client, owner.id) == STYLED_REQUEST_COUNT
            assert balance_after == balance_before + STYLED_REQUEST_COUNT * STYLE_KUDOS_SURCHARGE

    def test_concurrently_queued_requests_each_count_once_in_the_authors_style_record(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """Requests queued at the same time under one style each add one to the author's record."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text credit concurrent")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            def queue_and_cancel(_: int) -> None:
                # Each thread has a client of its own, so each request runs in an app context and on a
                # database session of its own.
                with queued_request(app.test_client(), request_headers, style=style_id):
                    pass

            # The app's connection pool holds five connections, and the main thread keeps one.
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(queue_and_cancel, range(STYLED_REQUEST_COUNT)))

            balance_after = settled_kudos(app, settle_kudos, owner.id)
            assert style_record(client, owner.id) == STYLED_REQUEST_COUNT
            assert balance_after == balance_before + STYLED_REQUEST_COUNT * STYLE_KUDOS_SURCHARGE

    def test_a_cancelled_request_keeps_the_authors_credit(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """The surcharge is not refunded on cancel, so the author keeps the credit and the record count."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("text credit cancelled")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, request_headers, style=style_id):
                pass

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before + STYLE_KUDOS_SURCHARGE
            assert style_record(client, owner.id) == 1

    def test_an_anonymous_request_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """An anonymous request at the anonymous minimum balance credits the author nothing, even when cancelled.

        The floor forgives the whole debit, surcharge included, so nothing was collected to credit, and the
        author's style record does not move either.
        """
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        anonymous_headers = {"apikey": ANONYMOUS_API_KEY, "Client-Agent": request_headers["Client-Agent"]}
        with (
            created_style(client, owner_headers, style_body("text credit anonymous")) as style_id,
            balance_pinned(app, settle_kudos, ANONYMOUS_USER_ID, above_floor=0),
        ):
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, anonymous_headers, style=style_id):
                pass

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before
            assert style_record(client, owner.id) == 0

    def test_a_requester_at_their_minimum_balance_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A registered requester with no headroom above their minimum balance credits the author nothing."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        requester = make_api_user()
        requester_headers = {"apikey": requester.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with (
            created_style(client, owner_headers, style_body("text credit at floor")) as style_id,
            balance_pinned(app, settle_kudos, requester.id, above_floor=0),
        ):
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, requester_headers, style=style_id):
                assert settled_kudos(app, settle_kudos, owner.id) == balance_before

            assert style_record(client, owner.id) == 0

    def test_a_requester_short_of_the_surcharge_pays_the_author_what_they_had(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
    ) -> None:
        """A requester whose headroom covers the horde tax and part of the surcharge credits only that part.

        The floor forgives what the requester's headroom does not cover, and the forgiven amount is taken out
        of the surcharge first. A partly collected surcharge still counts once in the author's record.
        """
        collected_surcharge = 1
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        requester = make_api_user()
        requester_headers = {"apikey": requester.api_key, "Client-Agent": request_headers["Client-Agent"]}
        headroom = ACTIVATION_HORDE_TAX + collected_surcharge
        with (
            created_style(client, owner_headers, style_body("text credit partial")) as style_id,
            balance_pinned(app, settle_kudos, requester.id, above_floor=headroom),
        ):
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, requester_headers, style=style_id):
                assert settled_kudos(app, settle_kudos, owner.id) == balance_before + collected_surcharge

            assert style_record(client, owner.id) == 1


class TestTextStyleAuthorCreditInShadowMode:
    """The author's credit when kudos movements are projected inline rather than by the applier."""

    def test_an_anonymous_request_pays_the_author_nothing(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
        shadow_mode: None,
    ) -> None:
        """An anonymous request at the anonymous minimum balance credits the author nothing."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        anonymous_headers = {"apikey": ANONYMOUS_API_KEY, "Client-Agent": request_headers["Client-Agent"]}
        with (
            created_style(client, owner_headers, style_body("shadow credit anonymous")) as style_id,
            balance_pinned(app, settle_kudos, ANONYMOUS_USER_ID, above_floor=0),
        ):
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, anonymous_headers, style=style_id):
                pass

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before
            assert style_record(client, owner.id) == 0

    def test_a_funded_request_pays_the_author_the_surcharge_once(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
        shadow_mode: None,
    ) -> None:
        """A funded request credits the author the whole surcharge once, inline, and the applier adds nothing."""
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        with created_style(client, owner_headers, style_body("shadow credit funded")) as style_id:
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, request_headers, style=style_id):
                pass

            assert settled_kudos(app, settle_kudos, owner.id) == balance_before + STYLE_KUDOS_SURCHARGE
            assert style_record(client, owner.id) == 1

    def test_a_requester_short_of_the_surcharge_pays_the_author_what_they_had(
        self,
        app: Flask,
        client: FlaskClient,
        request_headers: dict[str, str],
        make_api_user: MakeApiUser,
        settle_kudos: Callable[[], int],
        shadow_mode: None,
    ) -> None:
        """The inline floor is attributed to the surcharge first, as the applier's is."""
        collected_surcharge = 1
        owner = make_api_user(trusted=True, customizer=True, kudos=100)
        owner_headers = {"apikey": owner.api_key, "Client-Agent": request_headers["Client-Agent"]}
        requester = make_api_user()
        requester_headers = {"apikey": requester.api_key, "Client-Agent": request_headers["Client-Agent"]}
        headroom = ACTIVATION_HORDE_TAX + collected_surcharge
        with (
            created_style(client, owner_headers, style_body("shadow credit partial")) as style_id,
            balance_pinned(app, settle_kudos, requester.id, above_floor=headroom),
        ):
            balance_before = settled_kudos(app, settle_kudos, owner.id)

            with queued_request(client, requester_headers, style=style_id):
                assert settled_kudos(app, settle_kudos, owner.id) == balance_before + collected_surcharge


class TestTextStyleTypeMismatch:
    """A text request refuses a style of the other type."""

    def test_an_image_style_is_refused(self, client, request_headers: dict[str, str]) -> None:
        image_style = {
            "name": "text request image style",
            "info": "An image style used by the text style application tests.",
            "prompt": "{p}, watercolour###{np}",
            "params": {"steps": 8, "width": 512, "height": 512},
            "models": ["stable_diffusion"],
            "public": True,
            "nsfw": False,
        }
        created = client.post("/api/v2/styles/image", json=image_style, headers=request_headers)
        assert created.status_code == 200, created.get_data(as_text=True)
        style_id = created.get_json()["id"]
        try:
            response = post_dry_run(client, request_headers, style=style_id)

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleMismatch"
        finally:
            client.delete(f"/api/v2/styles/image/{style_id}", headers=request_headers)


class TestTextStyleParameterPolicy:
    """Which of a request's params a style's parameter policy lets through."""

    def test_override_none_ignores_the_request_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text policy none", parameter_policy={"override": "none"})
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"max_length": 480}):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == STYLE_PARAMS["max_length"]

    def test_override_all_takes_the_request_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text policy all", parameter_policy={"override": "all"})
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "temperature": 1.4},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 480
                assert payload["temperature"] == 1.4

    def test_override_listed_takes_only_the_listed_params(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text policy listed",
            parameter_policy={"override": "listed", "overridable": ["max_length"]},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"max_length": 480}):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 480
                assert payload["max_context_length"] == STYLE_PARAMS["max_context_length"]

    def test_a_param_the_style_does_not_list_is_ignored(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text policy listed unlisted",
            parameter_policy={"override": "listed", "overridable": ["max_length"]},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 480, "temperature": 1.4},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 480
                assert payload["temperature"] == STYLE_PARAMS["temperature"]

    def test_a_param_above_the_ceiling_is_refused_rather_than_trimmed(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "text policy ceiling",
            parameter_policy={"override": "all", "ceilings": {"max_length": 256}},
        )
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"max_length": 512})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterAboveCeiling"

    def test_a_param_at_the_ceiling_is_accepted(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text policy ceiling boundary",
            parameter_policy={"override": "all", "ceilings": {"max_length": 256}},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params={"max_length": 256}):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == 256

    def test_the_request_count_is_capped_even_under_none(self, client, request_headers: dict[str, str]) -> None:
        # The number of takes always comes from the request, so a ceiling on it applies under a mode
        # that lets no other param through.
        body = style_body("text policy takes ceiling", parameter_policy={"override": "none", "ceilings": {"n": 1}})
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, params={"n": 3})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleParameterAboveCeiling"

    def test_a_stored_policy_that_no_longer_validates_is_a_client_error(
        self,
        app,
        client,
        request_headers: dict[str, str],
    ) -> None:
        """A stored policy the text vocabulary does not accept is refused with 400 and the style's name."""
        import uuid

        from horde.classes.base.style import Style
        from horde.flask import db

        style_name = "text policy stored invalid"
        body = style_body(style_name, parameter_policy={"override": "all"})
        with created_style(client, request_headers, body) as style_id:
            # The endpoints refuse this policy, so it can only reach the row by a direct write, as it
            # would after the params model drops a param a stored policy names.
            with app.app_context():
                stored_style = db.session.query(Style).filter_by(id=uuid.UUID(style_id)).one()
                stored_style.parameter_policy = {"override": "listed", "overridable": ["max_tokens"]}
                db.session.commit()

            response = post_request(client, request_headers, style=style_id, params={"max_length": 120})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "StyleDeclarationInvalid"
            assert style_name in response.get_json()["message"]


def client_default_params() -> dict[str, Any]:
    """Build the params body a generated client sends when the user sets nothing.

    Read from the text payload model at call time, so the body tracks the defaults the API documents.

    Returns:
        Every param of the text payload model that documents a default, set to that default.
    """
    from horde.apis.v2.kobold import models

    return {
        parameter_name: parameter_field.default
        for parameter_name, parameter_field in models.input_model_generation_payload.resolved.items()
        if parameter_field.default is not None
    }


class TestTextStyleGeneratedClientDefaults:
    """A request from a client that sends every param with a default, under each policy mode."""

    style_params: dict[str, Any] = {
        "max_length": 120,
        "max_context_length": 1024,
        "temperature": 0.7,
        "min_p": 0.1,
        "smoothing_factor": 0.5,
        "dynatemp_range": 0.5,
        "dynatemp_exponent": 2.0,
    }
    """Differs from the documented default on every param that has one, so a payload shows which side won."""

    def test_the_style_differs_from_every_default(self) -> None:
        client_params = client_default_params()

        assert client_params
        for parameter_name, default_value in client_params.items():
            assert parameter_name in self.style_params, parameter_name
            assert self.style_params[parameter_name] != default_value, parameter_name

    def test_listed_takes_the_listed_param_and_the_style_keeps_the_rest(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        body = style_body(
            "text client defaults listed",
            params=dict(self.style_params),
            parameter_policy={"override": "listed", "overridable": ["max_length"]},
        )
        client_params = client_default_params()
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params=client_params):
                payload = pop_payload(client, request_headers)

                assert payload["max_length"] == client_params["max_length"]
                for parameter_name, style_value in self.style_params.items():
                    if parameter_name != "max_length":
                        assert payload[parameter_name] == style_value, parameter_name

    def test_all_takes_every_param_the_client_sent(self, client, request_headers: dict[str, str]) -> None:
        # A client that fills in every default overrides each of those params under this mode.
        body = style_body(
            "text client defaults all",
            params=dict(self.style_params),
            parameter_policy={"override": "all"},
        )
        client_params = client_default_params()
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params=client_params):
                payload = pop_payload(client, request_headers)

                for parameter_name, client_value in client_params.items():
                    assert payload[parameter_name] == client_value, parameter_name
                assert payload["temperature"] == self.style_params["temperature"]

    def test_none_keeps_every_param_of_the_style(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text client defaults none",
            params=dict(self.style_params),
            parameter_policy={"override": "none"},
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(client, request_headers, style=style_id, params=client_default_params()):
                payload = pop_payload(client, request_headers)

                for parameter_name, style_value in self.style_params.items():
                    assert payload[parameter_name] == style_value, parameter_name


class TestTextStyleTemplateFields:
    """Filling the placeholders a text style declares."""

    prompt_with_fields = "### Instruction:\nDescribe {caption} ({tags}).\n{p}\n\n### Response:\n"

    def test_the_fields_are_formatted_into_the_prompt(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text fields",
            prompt=self.prompt_with_fields,
            template_fields=TEMPLATE_FIELDS,
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="keep it short",
                style=style_id,
                template_fields={"caption": "a lighthouse", "tags": "storm, night"},
            ):
                payload = pop_payload(client, request_headers)

                assert payload["prompt"] == ("### Instruction:\nDescribe a lighthouse (storm, night).\nkeep it short\n\n### Response:\n")

    def test_an_optional_field_left_out_formats_to_nothing(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text fields optional",
            prompt=self.prompt_with_fields,
            template_fields=TEMPLATE_FIELDS,
        )
        with created_style(client, request_headers, body) as style_id:
            with queued_request(
                client,
                request_headers,
                prompt="keep it short",
                style=style_id,
                template_fields={"caption": "a lighthouse"},
            ):
                payload = pop_payload(client, request_headers)

                assert "Describe a lighthouse ()." in payload["prompt"]

    def test_fields_without_a_style_are_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(client, request_headers, template_fields={"caption": "a lighthouse"})

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "TemplateFieldsRequireStyle"

    def test_a_field_the_style_does_not_declare_is_refused(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text fields unknown", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = post_request(
                client,
                request_headers,
                style=style_id,
                template_fields={"caption": "a lighthouse", "mood": "bleak"},
            )

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "TemplateFieldUnknown"

    def test_a_required_field_left_out_is_refused(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text fields missing", prompt=self.prompt_with_fields, template_fields=TEMPLATE_FIELDS)
        with created_style(client, request_headers, body) as style_id:
            response = post_request(client, request_headers, style=style_id, template_fields={"tags": "storm"})

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "TemplateFieldMissing"


class TestTextStyleInstructPlaceholders:
    """What happens to the placeholders a text backend fills in itself."""

    def test_they_reach_the_request_as_written(self, client, request_headers: dict[str, str]) -> None:
        body = style_body("text instruct placeholders", prompt="{{[INPUT]}}{p}{{[OUTPUT]}}")
        with created_style(client, request_headers, body) as style_id:
            response = post_dry_run(client, request_headers, prompt="describe a lighthouse", style=style_id)

            assert response.status_code == 200, response.get_data(as_text=True)
            resolved = response.get_json()["resolved"]
            assert resolved["prompt"] == "{{[INPUT]}}describe a lighthouse{{[OUTPUT]}}"


class TestContextFit:
    """Sizing the prompt against the context the request asked for."""

    over_long_prompt = "filler text " * 400
    """Around 1600 estimated tokens, well past the 1024 context the cases below ask for."""

    def test_ignore_is_the_default_and_sends_an_over_long_prompt(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        with queued_request(
            client,
            request_headers,
            prompt=self.over_long_prompt,
            params={"max_length": 240, "max_context_length": 1024},
        ):
            payload = pop_payload(client, request_headers)

            assert payload["max_context_length"] == 1024

    def test_reject_refuses_a_prompt_that_does_not_fit(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(
            client,
            request_headers,
            prompt=self.over_long_prompt,
            context_fit="reject",
            params={"max_length": 240, "max_context_length": 1024},
        )

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "PromptExceedsContext"

    def test_reject_accepts_a_prompt_that_fits(self, client, request_headers: dict[str, str]) -> None:
        with queued_request(
            client,
            request_headers,
            context_fit="reject",
            params={"max_length": 80, "max_context_length": 1024},
        ):
            payload = pop_payload(client, request_headers)

            assert payload["max_context_length"] == 1024

    def test_grow_raises_the_context_to_a_power_of_two(self, client, request_headers: dict[str, str]) -> None:
        with queued_request(
            client,
            request_headers,
            prompt=self.over_long_prompt,
            context_fit="grow",
            params={"max_length": 240, "max_context_length": 1024},
        ):
            payload = pop_payload(client, request_headers)

            assert payload["max_context_length"] == 2048

    def test_growth_stops_at_the_styles_ceiling(self, client, request_headers: dict[str, str]) -> None:
        body = style_body(
            "text grow ceiling",
            parameter_policy={"override": "all", "ceilings": {"max_context_length": 1024}},
        )
        with created_style(client, request_headers, body) as style_id:
            response = post_request(
                client,
                request_headers,
                prompt=self.over_long_prompt,
                style=style_id,
                context_fit="grow",
                params={"max_length": 240, "max_context_length": 1024},
            )

            assert response.status_code == 400, response.get_data(as_text=True)
            assert response.get_json()["rc"] == "PromptExceedsContext"

    bound_worker_models = ["cicd/context-bound-scribe"]
    """Served only by the worker ``bound_worker_online`` checks in, so no other worker sets the bound."""

    unserved_models = ["cicd/context-unserved-scribe"]
    """Served by no worker in this module."""

    prompt_past_half_the_worker_context = "filler text " * 700
    """2800 estimated tokens: past 2048 with the 240 to generate, and within 4096."""

    prompt_past_the_worker_context = "filler text " * 1100
    """4400 estimated tokens, past 4096 before anything is generated."""

    @pytest.fixture
    def bound_worker_online(self, client, request_headers: dict[str, str]) -> None:
        """Check in a worker serving ``bound_worker_models`` at 4096 tokens of context.

        Taking a job checks the worker in, which puts it inside the online window.
        """
        with queued_request(client, request_headers, models=self.bound_worker_models):
            pop_payload(
                client,
                request_headers,
                worker_name="CICD Context Bound Scribe",
                models=self.bound_worker_models,
            )

    @pytest.mark.usefixtures("bound_worker_online")
    def test_grow_stops_at_the_largest_context_an_online_worker_serves(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        with queued_request(
            client,
            request_headers,
            prompt=self.prompt_past_half_the_worker_context,
            models=self.bound_worker_models,
            context_fit="grow",
            params={"max_length": 240, "max_context_length": 1024},
        ):
            payload = pop_payload(
                client,
                request_headers,
                worker_name="CICD Context Bound Scribe",
                models=self.bound_worker_models,
            )

            assert payload["max_context_length"] == WORKER_MAX_CONTEXT_LENGTH

    @pytest.mark.usefixtures("bound_worker_online")
    def test_grow_refuses_a_prompt_past_the_largest_context_an_online_worker_serves(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        response = post_request(
            client,
            request_headers,
            prompt=self.prompt_past_the_worker_context,
            models=self.bound_worker_models,
            context_fit="grow",
            params={"max_length": 240, "max_context_length": 1024},
        )

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "PromptExceedsContext"
        assert f"max_context_length of {WORKER_MAX_CONTEXT_LENGTH}" in response.get_json()["message"]

    def test_grow_without_an_online_worker_refuses_at_the_requested_context(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        response = post_request(
            client,
            request_headers,
            prompt=self.over_long_prompt,
            models=self.unserved_models,
            context_fit="grow",
            params={"max_length": 240, "max_context_length": 1024},
        )

        assert response.status_code == 400, response.get_data(as_text=True)
        assert response.get_json()["rc"] == "PromptExceedsContext"
        assert "max_context_length of 1024" in response.get_json()["message"]

    def test_an_unknown_setting_is_refused(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(client, request_headers, context_fit="shrink")

        assert response.status_code == 400, response.get_data(as_text=True)

    @pytest.mark.parametrize("context_fit", ["ignore", "reject", "grow"])
    def test_a_request_that_fits_is_unaffected_by_its_setting(
        self,
        client,
        request_headers: dict[str, str],
        context_fit: str,
    ) -> None:
        with queued_request(
            client,
            request_headers,
            context_fit=context_fit,
            params={"max_length": 80, "max_context_length": 2048},
        ):
            payload = pop_payload(client, request_headers)

            assert payload["max_context_length"] == 2048


class TestTextDryRunResolvedRequest:
    """What a dry run returns alongside the quote."""

    def test_a_request_without_a_style_resolves_to_itself(self, client, request_headers: dict[str, str]) -> None:
        response = post_dry_run(
            client,
            request_headers,
            prompt="describe a lighthouse",
            params={"max_length": 80},
        )

        assert response.status_code == 200, response.get_data(as_text=True)
        resolved = response.get_json()["resolved"]
        assert resolved["prompt"] == "describe a lighthouse"
        assert resolved["style"] is None
        assert resolved["models"] == TEXT_MODELS
        assert resolved["context_fit"] == "ignore"
        assert resolved["estimated_prompt_tokens"] == 7

    def test_a_styled_request_reports_the_style_it_ran_under(self, client, request_headers: dict[str, str]) -> None:
        with created_style(client, request_headers, style_body("text resolved style")) as style_id:
            response = post_dry_run(client, request_headers, style=style_id, params={"max_length": 480})

            assert response.status_code == 200, response.get_data(as_text=True)
            resolved = response.get_json()["resolved"]
            assert resolved["style"]["id"] == style_id
            assert resolved["style"]["name"] == "text resolved style"
            assert resolved["prompt"].startswith("### Instruction:")
            assert resolved["params"]["max_length"] == STYLE_PARAMS["max_length"]

    def test_the_grown_context_is_reported_and_charged_for(self, client, request_headers: dict[str, str]) -> None:
        # A quote is cached against the params it was computed from, and both requests here send the
        # same params, so the grown one has to skip that cache to be quoted at its grown context.
        ungrown = post_dry_run(
            client,
            request_headers,
            prompt=TestContextFit.over_long_prompt,
            params={"max_length": 240, "max_context_length": 1024},
        )
        grown = post_dry_run(
            client,
            request_headers,
            prompt=TestContextFit.over_long_prompt,
            context_fit="grow",
            params={"max_length": 240, "max_context_length": 1024},
        )

        assert ungrown.status_code == 200, ungrown.get_data(as_text=True)
        assert grown.status_code == 200, grown.get_data(as_text=True)
        assert grown.get_json()["resolved"]["params"]["max_context_length"] == 2048
        assert grown.get_json()["kudos"] > ungrown.get_json()["kudos"]

    def test_a_queued_request_carries_no_resolved_request(self, client, request_headers: dict[str, str]) -> None:
        response = post_request(client, request_headers)

        assert response.status_code == 202, response.get_data(as_text=True)
        queued = response.get_json()
        assert "resolved" not in queued
        client.delete(f"/api/v2/generate/text/status/{queued['id']}", headers=request_headers)


class TestTextStyleApplicationCompatibility:
    """A style that declares no policy or template fields applies exactly as it did before."""

    def test_the_prompt_the_params_and_the_quote_are_unchanged(
        self,
        client,
        request_headers: dict[str, str],
    ) -> None:
        # The baseline sends exactly the style's params and a temperature no other case uses, so the
        # two are quoted under different cache keys. Temperature does not affect the price, which
        # comes from max_length, max_context_length and the model.
        unstyled = post_dry_run(client, request_headers, params={**STYLE_PARAMS, "n": 2, "temperature": 0.9})
        assert unstyled.status_code == 200, unstyled.get_data(as_text=True)

        with created_style(client, request_headers, style_body("text compatibility")) as style_id:
            styled = post_dry_run(
                client,
                request_headers,
                style=style_id,
                params={"max_length": 512, "max_context_length": 4096, "temperature": 1.5, "n": 2},
            )
            assert styled.status_code == 200, styled.get_data(as_text=True)
            quote = styled.get_json()

            assert quote["resolved"]["prompt"] == STYLE_PROMPT.format(
                p="a horde of cute stable robots repairing a mainframe",
            )
            assert quote["resolved"]["params"] == {**STYLE_PARAMS, "n": 2}
            # The style here belongs to the requester, so no surcharge applies and the whole price
            # comes from the style's params.
            assert quote["kudos"] == unstyled.get_json()["kudos"]


@contextmanager
def created_collection(client: FlaskClient, request_headers: dict[str, str], *, name: str, style_ids: list[str]) -> Iterator[str]:
    """Create a style collection for the duration of the block and delete it at the end.

    Args:
        client: The Flask test client.
        request_headers: Headers carrying the API key of the user the collection belongs to.
        name: The collection name.
        style_ids: The styles the collection holds.

    Yields:
        The id of the new collection.
    """
    response = client.post("/api/v2/collections", json={"name": name, "styles": style_ids}, headers=request_headers)
    assert response.status_code == 200, response.get_data(as_text=True)
    collection_id = response.get_json()["id"]
    try:
        yield collection_id
    finally:
        client.delete(f"/api/v2/collections/{collection_id}", headers=request_headers)


class TestTextStyleCollection:
    """A text request can run under a collection, which draws one of its styles."""

    def test_a_dry_run_runs_under_one_of_the_collections_styles(self, client, request_headers: dict[str, str]) -> None:
        with (
            created_style(client, request_headers, style_body("text collection first")) as first_style_id,
            created_style(client, request_headers, style_body("text collection second")) as second_style_id,
            created_collection(
                client,
                request_headers,
                name="text collection dry run",
                style_ids=[first_style_id, second_style_id],
            ) as collection_id,
        ):
            response = post_dry_run(client, request_headers, style=collection_id)

            assert response.status_code == 200, response.get_data(as_text=True)
            assert response.get_json()["resolved"]["style"]["id"] in {first_style_id, second_style_id}

    def test_a_queued_request_counts_a_use_of_the_collection(self, client, request_headers: dict[str, str]) -> None:
        with (
            created_style(client, request_headers, style_body("text collection queued")) as style_id,
            created_collection(client, request_headers, name="text collection queued", style_ids=[style_id]) as collection_id,
        ):
            with queued_request(client, request_headers, style=collection_id):
                pass

            details = client.get(f"/api/v2/collections/{collection_id}", headers=request_headers).get_json()
            assert details["use_count"] == 1


class TestTextStyleCollectionTemplateFields:
    """A collection whose styles declare different template fields draws only a style that accepts the request's."""

    DRAWS = 8
    """How many requests each case sends, so a draw that ignored the fields would show up."""

    def test_each_request_runs_under_a_style_that_accepts_its_fields(self, client, request_headers: dict[str, str]) -> None:
        captioned_body = style_body("collection captioned", prompt="{p} {caption} {tags}", template_fields=TEMPLATE_FIELDS)
        with (
            created_style(client, request_headers, captioned_body) as captioned_style_id,
            created_style(client, request_headers, style_body("collection plain")) as plain_style_id,
            created_collection(
                client,
                request_headers,
                name="collection mixed fields",
                style_ids=[captioned_style_id, plain_style_id],
            ) as collection_id,
        ):
            with_caption = {
                post_dry_run(client, request_headers, style=collection_id, template_fields={"caption": "a lighthouse"}).get_json()[
                    "resolved"
                ]["style"]["id"]
                for _ in range(self.DRAWS)
            }
            without_fields = {
                post_dry_run(client, request_headers, style=collection_id).get_json()["resolved"]["style"]["id"] for _ in range(self.DRAWS)
            }

            assert with_caption == {captioned_style_id}
            assert without_fields == {plain_style_id}

    def test_fields_no_style_accepts_are_refused_every_time(self, client, request_headers: dict[str, str]) -> None:
        captioned_body = style_body("collection refusing", prompt="{p} {caption}", template_fields=TEMPLATE_FIELDS)
        with (
            created_style(client, request_headers, captioned_body) as captioned_style_id,
            created_collection(client, request_headers, name="collection refusing", style_ids=[captioned_style_id]) as collection_id,
        ):
            return_codes = {
                post_dry_run(client, request_headers, style=collection_id, template_fields={"mood": "calm"}).get_json()["rc"]
                for _ in range(self.DRAWS)
            }

            assert return_codes == {"TemplateFieldsMatchNoCollectionStyle"}
