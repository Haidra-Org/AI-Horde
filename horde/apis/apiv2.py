# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from flask import Blueprint
from flask_restx import Api

from horde.apis.v2 import api as v2
from horde.consts import HORDE_API_VERSION
from horde.vars import horde_contact_email, horde_title

blueprint = Blueprint("apiv2", __name__, url_prefix="/api")
api = Api(
    blueprint,
    version=str(HORDE_API_VERSION),
    title=f"{horde_title}",
    description=f"The API documentation for the {horde_title}",
    contact_email=horde_contact_email,
    default="v2",
    default_label="Latest Version",
    ordered=True,
)

api.add_namespace(v2)
