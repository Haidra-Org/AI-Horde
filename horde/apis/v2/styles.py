# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from flask_restx import Resource, reqparse

import horde.apis.limiter_api as lim
from horde import exceptions as e
from horde.apis.v2.base import api, models, parsers
from horde.classes.base.style import StyleCollection
from horde.database import functions as database
from horde.flask import cache, db
from horde.limiter import limiter
from horde.logger import logger
from horde.utils import ensure_clean

## Styles


class StyleTemplate(Resource):
    gentype = "template"
    args = None

    def get(self):
        if self.args.sort not in ["popular", "age"]:
            raise e.BadRequest("'model_state' needs to be one of ['popular', 'age']")
        styles_ret = database.retrieve_available_styles(
            style_type=self.gentype,
            sort=self.args.sort,
            page=self.args.page - 1,
            tag=self.args.tag,
            model=self.args.model,
        )
        styles_ret = [st.get_details() for st in styles_ret]
        return styles_ret, 200

    def post(self):
        # I have to extract and store them this way, because if I use the defaults
        # It causes them to be a shared object from the parsers class
        self.params = {}
        self.warnings = set()
        if self.args.params:
            self.params = self.args.params
        # For styles, we just store the models in the params
        self.models = []
        if self.args.models:
            self.params["models"] = self.args.models.copy()
        self.user = None
        self.validate()
        return

    def validate(self):
        self.sharedkey = None
        if self.args.sharedkey:
            self.sharedkey = database.find_sharedkey(self.args.sharedkey)
            if self.sharedkey is None:
                raise e.BadRequest("This shared key does not exist", "SharedKeyInvalid")
            shared_key_validity = self.sharedkey.is_valid()
            if shared_key_validity[0] is False:
                raise e.BadRequest(shared_key_validity[1], shared_key_validity[2])
        if self.user.deleted:
            raise e.Forbidden(message="This account has been scheduled for deletion and is disabled.", rc="DeletedUser")


class SingleStyleTemplateGet(Resource):
    gentype = "template"

    def get_existing_style(self):
        if self.existing_style.style_type != self.gentype:
            raise e.BadRequest(
                f"Style was found but was of the wrong type: {self.existing_style.style_type} != {self.gentype}",
                "StyleGetMistmatch",
            )
        return self.existing_style.get_details()

    def get_through_id(self, style_id):
        self.existing_style = database.get_style_by_uuid(style_id, is_collection=False)
        if not self.existing_style:
            raise e.ThingNotFound(f"{self.gentype} Style", style_id)
        return self.get_existing_style()


class SingleStyleTemplate(SingleStyleTemplateGet):

    def patch(self, style_id):
        self.params = {}
        self.warnings = set()
        self.args = parsers.style_parser.parse_args()
        if self.args.params:
            self.params = self.args.params
        # For styles, we just store the models in the params
        self.models = []
        style_modified = False
        self.tags = []
        if self.args.tags:
            self.tags = self.args.tags.copy()
            if len(self.tags) > 10:
                raise e.BadRequest("A style can be tagged a maximum of 10 times.")
        self.user = database.find_user_by_api_key(self.args["apikey"])
        if not self.user:
            raise e.InvalidAPIKey("Style PATCH")
        self.existing_style = database.get_style_by_uuid(style_id, is_collection=False)
        if not self.existing_style:
            raise e.ThingNotFound("Style", style_id)
        if self.existing_style.user_id != self.user.id:
            raise e.Forbidden(f"This Style is not owned by user {self.user.get_unique_alias()}")
        if self.args.models:
            self.models = self.args.models.copy()
            if len(self.models) > 5:
                raise e.BadRequest("A style can only use a maximum of 5 models.")
            if len(self.models) < 1:
                raise e.BadRequest("A style has to specify at least one model.")
        else:
            self.models = self.existing_style.get_model_names()
        self.style_name = None
        if self.args.name:
            self.style_name = ensure_clean(self.args.name, "style name")
            style_modified = True
        self.validate()
        self.existing_style.name = self.style_name
        if self.args.info is not None:
            self.existing_style.info = ensure_clean(self.args.info, "style info")
            style_modified = True
        if self.args.public is not None:
            self.existing_style.public = self.args.public
            style_modified = True
        if self.args.nsfw is not None:
            self.existing_style.nsfw = self.args.nsfw
            style_modified = True
        if self.args.prompt is not None:
            self.existing_style.prompt = self.args.prompt
            style_modified = True
        if self.args.params is not None:
            self.existing_style.params = self.args.params
            style_modified = True
        if len(self.models) > 0:
            style_modified = True
        if len(self.tags) > 0:
            style_modified = True
        if self.sharedkey is not None:
            self.existing_style.sharedkey_id = self.sharedkey.id
            style_modified = True
        if not style_modified:
            return {
                "id": self.existing_style.id,
                "message": "OK",
            }, 200
        db.session.commit()
        self.existing_style.set_models(self.models)
        self.existing_style.set_tags(self.tags)
        return {
            "id": self.existing_style.id,
            "message": "OK",
            "warnings": self.warnings,
        }, 200

    def validate(self):
        self.sharedkey = None
        if self.args.sharedkey:
            self.shared_key = database.find_sharedkey(self.args.sharedkey)
            if self.sharedkey is None:
                raise e.BadRequest("This shared key does not exist", "SharedKeyInvalid")
            shared_key_validity = self.sharedkey.is_valid()
            if shared_key_validity[0] is False:
                raise e.BadRequest(shared_key_validity[1], shared_key_validity[2])

    def delete(self, style_id):
        self.args = parsers.apikey_parser.parse_args()
        self.user = database.find_user_by_api_key(self.args["apikey"])
        if not self.user:
            raise e.InvalidAPIKey("Style DELETE")
        if self.user.is_anon():
            raise e.Forbidden("Anonymous users cannot delete styles", rc="StylesAnonForbidden")
        self.existing_style = database.get_style_by_uuid(style_id, is_collection=False)
        if not self.existing_style:
            raise e.ThingNotFound("Style", style_id)
        if self.existing_style.user_id != self.user.id and not self.user.moderator:
            raise e.Forbidden(f"This Style is not owned by user {self.user.get_unique_alias()}")
        if self.existing_style.user_id != self.user.id and self.user.moderator:
            logger.info(f"Moderator {self.user.moderator} deleted style {self.existing_style.id}")
        self.existing_style.delete()
        return ({"message": "OK"}, 200)


## Collections


class Collection(Resource):
    args = None

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
        "type",
        required=False,
        default="all",
        type=str,
        help="Filter by type. Accepts either 'image', 'text' or 'all'.",
        location="args",
    )

    @cache.cached(timeout=30, query_string=True)
    @api.expect(get_parser)
    @api.marshal_with(
        models.response_model_collection,
        code=200,
        description="Lists collection information",
        as_list=True,
    )
    def get(self):
        """Displays all existing collections. Can filter by type"""
        self.args = self.get_parser.parse_args()
        if self.args.sort not in ["popular", "age"]:
            raise e.BadRequest("'model_state' needs to be one of ['popular', 'age']")
        if self.args.type not in ["all", "image", "text"]:
            raise e.BadRequest("'type' needs to be one of ['all', 'image', 'text']")
        collections = database.retrieve_available_collections(
            sort=self.args.sort,
            page=self.args.page - 1,
            collection_type=self.args.type if self.args.type in ["image", "text"] else None,
        )
        collections_ret = [co.get_details() for co in collections]
        return collections_ret, 200

    post_parser = reqparse.RequestParser()
    post_parser.add_argument(
        "apikey",
        type=str,
        required=True,
        help="The API Key corresponding to a registered user.",
        location="headers",
    )
    post_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )
    post_parser.add_argument(
        "name",
        type=str,
        required=True,
        location="json",
    )
    post_parser.add_argument(
        "info",
        type=str,
        required=False,
        location="json",
    )
    post_parser.add_argument(
        "public",
        type=bool,
        default=True,
        required=False,
        location="json",
    )
    post_parser.add_argument(
        "styles",
        type=list,
        required=True,
        location="json",
    )

    decorators = [
        limiter.limit(
            limit_value=lim.get_request_90min_limit_per_ip,
            key_func=lim.get_request_path,
        ),
        limiter.limit(limit_value=lim.get_request_2sec_limit_per_ip, key_func=lim.get_request_path),
    ]

    @api.expect(post_parser, models.input_model_collection, validate=True)
    @api.marshal_with(
        models.response_model_styles_post,
        code=200,
        description="Collection Added",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    def post(self):
        """Creates a new style collection."""
        self.warnings = set()
        # For styles, we just store the models in the params
        self.styles = []
        styles_type = None
        self.args = self.post_parser.parse_args()
        if self.args.styles:
            if len(self.args.styles) < 1:
                raise e.BadRequest("A collection has to include at least 1 style")
        else:
            raise e.BadRequest("A collection has to include at least 1 style")
        self.user = database.find_user_by_api_key(self.args["apikey"])
        if not self.user:
            raise e.InvalidAPIKey("Collection POST")
        if self.user.deleted:
            raise e.Forbidden(message="This account has been scheduled for deletion and is disabled.", rc="DeletedUser")
        if self.user.is_anon():
            raise e.Forbidden("Anonymous users cannot create collections", rc="StylesAnonForbidden")
        for st in self.args.styles:
            existing_style = database.get_style_by_uuid(st, is_collection=False)
            if not existing_style:
                existing_style = database.get_style_by_name(st, is_collection=False)
                if not existing_style:
                    raise e.BadRequest(f"A style with name '{st}' cannot be found")
                if styles_type is None:
                    styles_type = existing_style.style_type
                elif styles_type != existing_style.style_type:
                    raise e.BadRequest("Cannot mix image and text styles in the same collection")
            self.styles.append(existing_style)
        self.collection_name = ensure_clean(self.args.name, "collection name")
        new_collection = StyleCollection(
            user_id=self.user.id,
            style_type=styles_type,
            info=ensure_clean(self.args.info, "collection info") if self.args.info is not None else "",
            name=self.collection_name,
            public=self.args.public,
        )
        new_collection.create(self.styles)
        return {
            "id": new_collection.id,
            "message": "OK",
            "warnings": self.warnings,
        }, 200


class SingleCollectionGet(Resource):

    def get_through_id(self, style_id):
        self.existing_collection = database.get_style_by_uuid(style_id, is_collection=True)
        if not self.existing_collection:
            raise e.ThingNotFound("Collection", style_id)
        return self.existing_collection.get_details()


class SingleCollection(SingleCollectionGet):
    args = None

    @cache.cached(timeout=30, query_string=True)
    @api.expect(parsers.basic_parser)
    @api.marshal_with(
        models.response_model_collection,
        code=200,
        description="Lists collection information",
        as_list=False,
    )
    def get(self, collection_id):
        """Displays information about a single style collection."""
        return super().get_through_id(collection_id)

    patch_parser = reqparse.RequestParser()
    patch_parser.add_argument(
        "apikey",
        type=str,
        required=True,
        help="The API Key corresponding to a registered user.",
        location="headers",
    )
    patch_parser.add_argument(
        "Client-Agent",
        default="unknown:0:unknown",
        type=str,
        required=False,
        help="The client name and version",
        location="headers",
    )
    patch_parser.add_argument(
        "name",
        type=str,
        required=False,
        location="json",
    )
    patch_parser.add_argument(
        "info",
        type=str,
        required=False,
        location="json",
    )
    patch_parser.add_argument(
        "public",
        type=bool,
        required=False,
        location="json",
    )
    patch_parser.add_argument(
        "styles",
        type=list,
        required=False,
        location="json",
    )

    decorators = [
        limiter.limit(
            limit_value=lim.get_request_90min_limit_per_ip,
            key_func=lim.get_request_path,
        ),
        limiter.limit(limit_value=lim.get_request_2sec_limit_per_ip, key_func=lim.get_request_path),
    ]

    @api.expect(patch_parser, models.input_model_collection, validate=True)
    @api.marshal_with(
        models.response_model_styles_post,
        code=200,
        description="Collection Modified",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    def patch(self, collection_id):
        """Modifies an existing style collection."""
        self.warnings = set()
        # For styles, we just store the models in the params
        self.styles = []
        styles_type = None
        self.args = self.patch_parser.parse_args()
        if self.args.styles:
            if len(self.args.styles) < 1:
                raise e.BadRequest("A collection has to include at least 1 style")
            for st in self.args.styles:
                existing_style = database.get_style_by_uuid(st, is_collection=False)
                if not existing_style:
                    existing_style = database.get_style_by_name(st, is_collection=False)
                    if not existing_style:
                        raise e.BadRequest(f"A style with name '{st}' cannot be found")
                    if styles_type is None:
                        styles_type = existing_style.style_type
                    elif styles_type != existing_style.style_type:
                        raise e.BadRequest("Cannot mix image and text styles in the same collection", "StyleMismatch")
                self.styles.append(existing_style)
        self.user = database.find_user_by_api_key(self.args["apikey"])
        if not self.user:
            raise e.InvalidAPIKey("Collection PATCH")
        self.existing_collection = database.get_style_by_uuid(collection_id, is_collection=True)
        if not self.existing_collection:
            raise e.ThingNotFound("Collection", collection_id)
        if self.existing_collection.user_id != self.user.id:
            raise e.Forbidden(f"This Collection is not owned by user {self.user.get_unique_alias()}")
        if self.existing_collection.style_type != styles_type:
            raise e.BadRequest("Cannot mix image and text styles in the same collection", "StyleMismatch")
        collection_modified = False
        if self.args.name:
            self.existing_collection.name = ensure_clean(self.args.name, "collection name")
            collection_modified = True
        if self.args.info is not None:
            self.existing_collection.info = ensure_clean(self.args.info, "style info")
            collection_modified = True
        if self.args.public is not None:
            self.existing_collection.public = self.args.public
            collection_modified = True
        if len(self.styles) > 0:
            self.existing_collection.styles.clear()
            for st in self.styles:
                self.existing_collection.styles.append(st)
            collection_modified = True
        if not collection_modified:
            return {
                "id": self.existing_collection.id,
                "message": "OK",
            }, 200
        db.session.commit()
        return {
            "id": self.existing_collection.id,
            "message": "OK",
            "warnings": self.warnings,
        }, 200

    @api.expect(parsers.apikey_parser)
    @api.marshal_with(
        models.response_model_simple_response,
        code=200,
        description="Operation Completed",
        skip_none=True,
    )
    @api.response(400, "Validation Error", models.response_model_validation_errors)
    @api.response(401, "Invalid API Key", models.response_model_error)
    def delete(self, collection_id):
        """Deletes a style collection."""
        self.args = parsers.apikey_parser.parse_args()
        self.user = database.find_user_by_api_key(self.args["apikey"])
        if not self.user:
            raise e.InvalidAPIKey("Collection PATCH")
        self.existing_collection = database.get_style_by_uuid(collection_id, is_collection=True)
        if not self.existing_collection:
            raise e.ThingNotFound("Collection", collection_id)
        if self.existing_collection.user_id != self.user.id and not self.user.moderator:
            raise e.Forbidden(f"This Collection is not owned by user {self.user.get_unique_alias()}")
        if self.existing_collection.user_id != self.user.id and self.user.moderator:
            logger.info(f"Moderator {self.user.moderator} deleted collection {self.existing_collection.id}")
        self.existing_collection.delete()
        return ({"message": "OK"}, 200)


class SingleCollectionByName(SingleCollectionGet):
    @cache.cached(timeout=30)
    @api.expect(parsers.basic_parser)
    @api.marshal_with(
        models.response_model_collection,
        code=200,
        description="Lists collection information by name",
        as_list=False,
    )
    def get(self, collection_name):
        """Seeks an style collection by name and displays its information."""
        self.existing_collection = database.get_style_by_name(collection_name)
        if not self.existing_collection:
            raise e.ThingNotFound("Collection", collection_name)
        return self.existing_collection.get_details()


# TODO: vote and transfer kudos on vote
