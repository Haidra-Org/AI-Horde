# SPDX-FileCopyrightText: 2026 MoonlightByte
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The interrogation status models publish the values their ``state`` field can take."""

from flask import Flask
from flask_restx import Api

from horde.apis.models.stable_v2 import ImageModels
from horde.enums import State


def test_interrogation_state_fields_list_their_enum_values():
    api = Api(Flask(__name__))
    models = ImageModels(api)
    expected = [state.name.lower() for state in State]
    for model in (models.response_model_interrogation_status, models.response_model_interrogation_form_status):
        schema = model["state"].__schema__
        assert schema["enum"] == expected
        assert {"waiting", "processing", "done", "faulted", "partial"} <= set(schema["enum"])
