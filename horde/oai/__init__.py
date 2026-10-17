# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An OpenAI-compatible chat completions front for the AI Horde.

The front is a separate ASGI process (``python -m horde.oai``). It accepts ``POST /v1/chat/completions`` and
``GET /v1/models``, submits each chat request to the Horde's native ``/v2/generate/chat`` routes over HTTP, waits for
the Horde's Redis completion signal (with a status poll as fallback), and answers in the OpenAI response shape. It
imports nothing from the Flask application, so it runs beside the Horde without its database or Flask stack.

Modules:
    ``settings``: The environment-driven [`FrontSettings`][horde.oai.settings.FrontSettings].
    ``schema``: The OpenAI request, response and error models, the default no-op table and the refusal screen.
    ``mapping``: The native request body, the completion built from a Horde status, and the error envelope.
    ``sse``: The streamed chunks, keepalive comments and the error event.
    ``horde_client``: The asynchronous HTTP client for the Horde API, with a separate cancel pool.
    ``waiter``: The Redis completion signal listener, the per-request wait loop and the deadline rule.

The ``horde`` extension object is validated against ``horde.chat_contract``, the model the native chat route shares.
    ``app``: The FastAPI application factory, [`create_app`][horde.oai.app.create_app].

The package namespace imports nothing itself, so importing a submodule does not start any client or connection.
"""
