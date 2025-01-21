# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later


from flask_restx import reqparse

import horde.apis.limiter_api as lim
from horde import exceptions as e
from horde.apis.v2.kobold import models, parsers
from horde.apis.v2.styles import (
    SingleStyleTemplate,
    SingleStyleTemplateGet,
    StyleTemplate,
    api,
)
from horde.classes.base.style import Style
from horde.database import functions as database
from horde.flask import cache
from horde.limiter import limiter
from horde.logger import logger
from horde.utils import ensure_clean
from horde.validation import ParamValidator


## Styles
class TextStyle(StyleTemplate):
    gentype = "text"

    get_parser = reqparse.RequestParser()
    get_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version.",
        location="headers",
    )
    get_parser.add_argument(
        "sort",
        required=False,
        default="popular",
        type=str,
        help="How to sort returned styles. 'popular' sorts by usage and 'age' sorts by date added.",
        location="args",
    )
    get_parser.add_argument(
        "page",
        required=False,
        default=1,
        type=int,
        help="Which page of results to return. Each page has 25 styles.",
        location="args",
    )
    get_parser.add_argument(
        "tag",
        required=False,
        type=str,
        help="If included, will only return styles with this tag",
        location="args",
    )
    get_parser.add_argument(
        "model",
        required=False,
        type=str,
        help="If included, will only return styles using this model",
        location="args",
    )

    @logger.catch(reraise=True)
    @cache.cached(timeout=1, query_string=True)
    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_style,
        code=200,
        description="Lists text styles information",
        as_list=True,
        skip_none=True,
    )
    def get(self):
        """Retrieves information about all text styles
        Can be filtered based on model or tags
        """
        self.args = self.get_parser.parse_args()
        return super().get()

    decorators = [
        limiter.limit(
            limit_value="20/hour",
            key_func=lim.get_request_path,
        ),
        limiter.limit(limit_value=lim.get_request_2sec_limit_per_ip, key_func=lim.get_request_path),
    ]

    @api.expect(parsers.style_parser, models.input_model_style, validate=True)
    @api.marshal_with(
        models.response_model_styles_post,
        code=200,
        description="Style Added",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    def post(self):
        """Creates a new text style"""
        self.params = {}
        self.warnings = set()
        self.args = parsers.style_parser.parse_args()
        if self.args.params:
            self.params = self.args.params
        self.models = []
        if self.args.models is not None:
            self.models = self.args.models.copy()
            if len(self.models) > 5:
                raise e.BadRequest("A style can only use a maximum of 5 models.")
            if len(self.models) < 1:
                raise e.BadRequest("A style has to specify at least one model.")
        else:
            raise e.BadRequest("A style has to specify at least one model.")
        self.tags = []
        if self.args.tags is not None:
            self.tags = self.args.tags.copy()
            if len(self.tags) > 10:
                raise e.BadRequest("A style can be tagged a maximum of 10 times.")
        self.user = database.find_user_by_api_key(self.args["apikey"])
        if not self.user:
            raise e.InvalidAPIKey("TextStyle POST")
        if not self.user.customizer and not self.user.trusted:
            raise e.Forbidden(
                "Only customizers and trusted users can create new styles. You can request the customizer role in our channels.",
                rc="StylesRequiresCustomizer",
            )
        if self.user.is_anon():
            raise e.Forbidden("Anonymous users cannot create styles", rc="StylesAnonForbidden")
        self.style_name = ensure_clean(self.args.name, "style name")
        self.validate()
        new_style = Style(
            user_id=self.user.id,
            style_type=self.gentype,
            info=ensure_clean(self.args.info, "style info") if self.args.info is not None else "",
            name=self.style_name,
            public=self.args.public,
            nsfw=self.args.nsfw,
            prompt=self.args.prompt,
            params=self.args.params if self.args.params is not None else {},
            sharedkey_id=self.sharedkey.id if self.sharedkey else None,
        )
        new_style.create()
        new_style.set_models(self.models)
        new_style.set_tags(self.tags)
        return {
            "id": new_style.id,
            "message": "OK",
            "warnings": self.warnings,
        }, 200

    def validate(self):
        super().validate()
        if database.get_style_by_name(f"{self.user.get_unique_alias()}::style::{self.style_name}"):
            raise e.BadRequest(
                (
                    f"Style with name '{self.style_name}' already exists for user '{self.user.get_unique_alias()}'."
                    " Please use PATCH to modify an existing style."
                ),
            )
        param_validator = ParamValidator(prompt=self.args.prompt, models=self.models, params=self.params, user=self.user)
        self.warnings = param_validator.validate_text_params()
        param_validator.check_for_special()
        param_validator.validate_text_prompt(self.args.prompt)


class SingleTextStyle(SingleStyleTemplate):
    gentype = "text"

    @cache.cached(timeout=30)
    @api.expect(parsers.basic_parser)
    @api.marshal_with(
        models.response_model_style,
        code=200,
        description="Lists text styles information",
        as_list=False,
        skip_none=True,
    )
    def get(self, style_id):
        """Displays information about a single text style."""
        return super().get_through_id(style_id)

    decorators = [
        limiter.limit(
            limit_value=lim.get_request_90min_limit_per_ip,
            key_func=lim.get_request_path,
        ),
        limiter.limit(limit_value=lim.get_request_2sec_limit_per_ip, key_func=lim.get_request_path),
    ]

    @api.expect(parsers.style_parser_patch, models.patch_model_style, validate=True)
    @api.marshal_with(
        models.response_model_styles_post,
        code=200,
        description="Style Updated",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    def patch(self, style_id):
        """Modifies a text style."""
        return super().patch(style_id)

    def validate(self):
        if (
            self.style_name is not None
            and database.get_style_by_name(f"{self.user.get_unique_alias()}::style::{self.style_name}")
            and self.existing_style.name != self.style_name
        ):
            raise e.BadRequest(
                (
                    f"Style with name '{self.style_name}' already exists for user '{self.user.get_unique_alias()}'."
                    " Please use a different name if you want to rename."
                ),
            )
        prompt = self.args.prompt if self.args.prompt is not None else self.existing_style.prompt
        models = self.models if len(self.models) > 0 else self.existing_style.get_model_names()
        params = self.args.params if self.args.params is not None else self.existing_style.params
        param_validator = ParamValidator(prompt=prompt, models=models, params=params, user=self.user)
        self.warnings = param_validator.validate_text_params()
        param_validator.check_for_special()
        param_validator.validate_text_prompt(prompt)

    @api.expect(parsers.apikey_parser)
    @api.marshal_with(
        models.response_model_simple_response,
        code=200,
        description="Style Deleted",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    def delete(self, style_id):
        """Deletes a text style."""
        return super().delete(style_id)


class SingleImageStyleByName(SingleStyleTemplateGet):
    gentype = "text"

    @cache.cached(timeout=30)
    @api.expect(parsers.basic_parser)
    @api.marshal_with(
        models.response_model_style,
        code=200,
        description="Lists text style information by name",
        as_list=False,
        skip_none=True,
    )
    def get(self, style_name):
        """Seeks a text style by name and displays its information."""
        self.existing_style = database.get_style_by_name(style_name, is_collection=False)
        if not self.existing_style:
            raise e.ThingNotFound("Style", style_name)
        return super().get_existing_style()
