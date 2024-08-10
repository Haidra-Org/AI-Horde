# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from datetime import datetime
from typing import Union

from horde.flask import db
from horde.logger import logger


class KnownImageModel(db.Model):
    """The schema for the known image models database table."""

    __tablename__ = "known_image_models"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    baseline = db.Column(db.String(128), nullable=False)
    """The baseline of the model. For example, 'stable diffusion 1' or 'stable_diffusion_xl`."""
    inpainting = db.Column(db.Boolean, nullable=False)
    description = db.Column(db.String(512), nullable=True)
    version = db.Column(db.String(16), nullable=False)
    style = db.Column(db.String(64), nullable=False)
    tags = db.Column(db.JSON, nullable=False)
    homepage = db.Column(db.String(256), nullable=True)
    nsfw = db.Column(db.Boolean, nullable=False)
    requirements = db.Column(db.JSON, nullable=True)
    config = db.Column(db.JSON, nullable=False)
    features_not_supported = db.Column(db.JSON, nullable=True)
    size_on_disk_bytes = db.Column(db.BigInteger, nullable=True)
    """The size of the model on disk in bytes."""
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    """The time the model was added to the database."""
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    """The time the model was last updated in the database."""


@logger.catch(reraise=True)
def get_known_image_models() -> list[KnownImageModel]:
    """Get all known image models from the database."""
    return db.session.query(KnownImageModel).all()


@logger.catch(reraise=True)
def is_model_known(model_name: Union[KnownImageModel, str]) -> bool:
    """Check if a model is known in the database.

    Args:
        model_name (str): The name of the model to check.

    Returns:
        bool: Whether the model is known.
    """
    if isinstance(model_name, KnownImageModel):
        model_name = model_name.name

    return db.session.query(KnownImageModel).filter(KnownImageModel.name == model_name).first() is not None


@logger.catch(reraise=True)
def add_known_image_model(
    name: str,
    baseline: str,
    inpainting: bool,
    description: str,
    version: str,
    style: str,
    tags: list[str],
    homepage: str,
    nsfw: bool,
    requirements: dict,
    config: dict,
    features_not_supported: list[str],
    size_on_disk_bytes: int,
    *,
    defer_commit: bool = False,
) -> None:
    """Add an image model to the database. This function will update the model if it already exists.

    Note that the arguments of this function reflect those found in the model reference JSON.

    Args:
        name (str): The name of the model.
        baseline (str): The baseline model used.
        inpainting (bool): Whether the model is capable of inpainting.
        description (str): A description of the model.
        version (str): The version of the model.
        style (str): The style of the model.
        tags (list[str]): A list of tags for the model.
        homepage (str): The homepage of the model.
        nsfw (bool): Whether the model is NSFW.
        requirements (dict): The requirements of the model.
        config (dict): The configuration of the model.
        features_not_supported (list[str]): A list of features not supported by the model.
        size_on_disk_bytes (int): The size of the model on disk.

        defer_commit (bool): Whether to defer committing the addition to the database.
    """

    model: Union[KnownImageModel, None] = db.session.query(KnownImageModel).filter(KnownImageModel.name == name).first()

    if model:
        model.baseline = baseline
        model.inpainting = inpainting
        model.description = description
        model.version = version
        model.style = style
        model.tags = tags
        model.homepage = homepage
        model.nsfw = nsfw
        model.requirements = requirements
        model.config = config
        model.features_not_supported = features_not_supported
        model.size_on_disk_bytes = size_on_disk_bytes
    else:
        logger.info(f"Attempting to add new known image model: {name}")
        model = KnownImageModel(
            name=name,
            baseline=baseline,
            inpainting=inpainting,
            description=description,
            version=version,
            style=style,
            tags=tags,
            homepage=homepage,
            nsfw=nsfw,
            requirements=requirements,
            config=config,
            features_not_supported=features_not_supported,
            size_on_disk_bytes=size_on_disk_bytes,
        )
        db.session.add(model)

    if not defer_commit:
        db.session.commit()


@logger.catch(reraise=True)
def add_known_image_model_from_json(json: dict[str, object], defer_commit: bool = False) -> None:
    """Add a image model to the database from a JSON object.

    Args:
        json (dict[str, object]): The model reference JSON object.
        defer_commit (bool): Whether to defer committing the addition to the database.

    """
    add_known_image_model(
        name=json.get("name"),
        baseline=json.get("baseline"),
        inpainting=json.get("inpainting"),
        description=json.get("description"),
        version=json.get("version"),
        style=json.get("style"),
        tags=json.get("tags"),
        homepage=json.get("homepage"),
        nsfw=json.get("nsfw"),
        requirements=json.get("requirements"),
        config=json.get("config"),
        features_not_supported=json.get("features_not_supported"),
        size_on_disk_bytes=json.get("size_on_disk_bytes"),
        defer_commit=defer_commit,
    )


@logger.catch(reraise=True)
def add_known_image_models_from_json(json: dict[str, dict]) -> None:
    """Add multiple image models to the database from a JSON object.

    Args:
        json (dict[str, dict]): The model reference JSON object.
    """
    for model in json.values():
        add_known_image_model_from_json(model, defer_commit=True)

    db.session.commit()
    logger.info(f"Added (or updated) {len(json)} known image models.")


@logger.catch(reraise=True)
def delete_known_image_model(model_name: str, defer_commit: bool = False) -> bool:
    """Attempt to delete a known image model from the database.

    Args:
        model_name (str): Name of the model to delete.
        defer_commit (bool): Whether to defer committing the deletion to the database.

    Returns:
        bool: Whether the model was deleted, or if defer_commit is True, whether the model was found and queued for deletion.
    """
    model = db.session.query(KnownImageModel).filter(KnownImageModel.name == model_name).first()
    if model:
        db.session.delete(model)
        logger.info(f"Queueing deletion of known image model: {model_name}")
        if not defer_commit:
            db.session.commit()

        return True
    else:
        logger.error(f"Model {model_name} not found in the database")

    return False


@logger.catch(reraise=True)
def delete_any_unspecified_image_models(models_desired: list[str]) -> None:
    """Delete any models not specified in the list from the database.

    Args:
        models_desired (list[str]): List of model names to keep in the database.
    """
    models_records_in_db = db.session.query(KnownImageModel).all()
    model_names_in_db = [model.name for model in models_records_in_db]
    num_deleted = 0
    for model in model_names_in_db:
        if model not in models_desired:
            was_deleted = delete_known_image_model(model, defer_commit=True)
            if was_deleted:
                num_deleted += 1

    if num_deleted > 0:
        logger.info(f"Deleted {num_deleted} models from the database")

    db.session.commit()
