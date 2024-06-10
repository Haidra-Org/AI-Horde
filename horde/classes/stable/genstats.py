from datetime import datetime

from sqlalchemy import Enum

from horde.enums import ImageGenState
from horde.flask import db


class ImageGenerationStatisticPP(db.Model):
    __tablename__ = "image_gen_stats_post_processors"
    id = db.Column(db.Integer, primary_key=True)
    imgstat_id = db.Column(
        db.Integer,
        db.ForeignKey("image_gen_stats.id", ondelete="CASCADE"),
        nullable=False,
    )
    imgstat = db.relationship("ImageGenerationStatistic", back_populates="post_processors")
    pp = db.Column(db.String(40), nullable=False)


class ImageGenerationStatisticCN(db.Model):
    __tablename__ = "image_gen_stats_controlnet"
    id = db.Column(db.Integer, primary_key=True)
    imgstat_id = db.Column(
        db.Integer,
        db.ForeignKey("image_gen_stats.id", ondelete="CASCADE"),
        nullable=False,
    )
    imgstat = db.relationship("ImageGenerationStatistic", back_populates="controlnet")
    control_type = db.Column(db.String(40), nullable=False)


class ImageGenerationStatisticLora(db.Model):
    __tablename__ = "image_gen_stats_loras"
    id = db.Column(db.Integer, primary_key=True)
    imgstat_id = db.Column(
        db.Integer,
        db.ForeignKey("image_gen_stats.id", ondelete="CASCADE"),
        nullable=False,
    )
    imgstat = db.relationship("ImageGenerationStatistic", back_populates="loras")
    lora = db.Column(db.String(255), nullable=False)


class ImageGenerationStatisticTI(db.Model):
    __tablename__ = "image_gen_stats_tis"
    id = db.Column(db.Integer, primary_key=True)
    imgstat_id = db.Column(
        db.Integer,
        db.ForeignKey("image_gen_stats.id", ondelete="CASCADE"),
        nullable=False,
    )
    imgstat = db.relationship("ImageGenerationStatistic", back_populates="tis")
    ti = db.Column(db.String(255), nullable=False)


class ImageGenerationStatistic(db.Model):
    __tablename__ = "image_gen_stats"
    id = db.Column(db.Integer, primary_key=True)
    finished = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    # Created comes from the procgen
    created = db.Column(db.DateTime(timezone=False), nullable=True)
    model = db.Column(db.String(255), index=True, nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    steps = db.Column(db.Integer, nullable=False)
    cfg = db.Column(db.Integer, nullable=False)
    sampler = db.Column(db.String(30), nullable=False, index=True)
    prompt_length = db.Column(db.Integer, nullable=False)
    negprompt = db.Column(db.Boolean, nullable=False)
    img2img = db.Column(db.Boolean, nullable=False, index=True)
    hires_fix = db.Column(db.Boolean, nullable=False)
    tiling = db.Column(db.Boolean, nullable=False)
    nsfw = db.Column(db.Boolean, nullable=False)
    state = db.Column(Enum(ImageGenState), default=ImageGenState.OK, nullable=False, index=True)
    client_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False, index=True)
    bridge_agent = db.Column(db.Text, default="unknown:0:unknown", nullable=False, index=True)
    post_processors = db.relationship(
        "ImageGenerationStatisticPP",
        back_populates="imgstat",
        cascade="all, delete-orphan",
    )
    controlnet = db.relationship(
        "ImageGenerationStatisticCN",
        back_populates="imgstat",
        cascade="all, delete-orphan",
    )
    loras = db.relationship(
        "ImageGenerationStatisticLora",
        back_populates="imgstat",
        cascade="all, delete-orphan",
    )
    tis = db.relationship(
        "ImageGenerationStatisticTI",
        back_populates="imgstat",
        cascade="all, delete-orphan",
    )


def record_image_statistic(procgen):
    # We don't record stats for special models
    if "horde_special" in procgen.model:
        return
    state = ImageGenState.OK
    if procgen.censored:
        state = ImageGenState.CENSORED
    # Currently there's no way to record cancelled images, but maybe there will be in the future
    elif procgen.cancelled:
        state = ImageGenState.CANCELLED
    elif procgen.faulted:
        state = ImageGenState.FAULTED
    statistic = ImageGenerationStatistic(
        created=procgen.start_time,
        model=procgen.model,
        width=procgen.wp.width,
        height=procgen.wp.height,
        steps=procgen.wp.params["steps"],
        cfg=procgen.wp.params["cfg_scale"],
        sampler=procgen.wp.params["sampler_name"],
        prompt_length=len(procgen.wp.prompt),
        negprompt="###" in procgen.wp.prompt,
        hires_fix=procgen.wp.params.get("hires_fix", False),
        tiling=procgen.wp.params.get("tiling", False),
        img2img=procgen.wp.source_image != None,  # noqa E711
        nsfw=procgen.wp.nsfw,
        bridge_agent=procgen.worker.bridge_agent,
        client_agent=procgen.wp.client_agent,
        state=state,
    )
    db.session.add(statistic)
    db.session.commit()
    # face_fixers = ["GFPGAN", "CodeFormers"]
    # upscalers = ["RealESRGAN_x4plus"]
    post_processors = procgen.wp.params.get("post_processing", [])
    if len(post_processors) > 0:
        for pp in post_processors:
            new_pp_entry = ImageGenerationStatisticPP(imgstat_id=statistic.id, pp=pp)
            db.session.add(new_pp_entry)
        db.session.commit()
    # For now we support only one control_type per request, but in the future we might allow more
    # So I set it up on an external table to be able to expand
    if procgen.wp.params.get("control_type", None):
        new_cn_entry = ImageGenerationStatisticCN(
            imgstat_id=statistic.id,
            control_type=procgen.wp.params["control_type"],
        )
        db.session.add(new_cn_entry)
        db.session.commit()
    loras = procgen.wp.params.get("loras", [])
    if len(loras) > 0:
        for lora in loras:
            new_lora_entry = ImageGenerationStatisticLora(imgstat_id=statistic.id, lora=lora["name"])
            db.session.add(new_lora_entry)
        db.session.commit()
    tis = procgen.wp.params.get("tis", [])
    if len(tis) > 0:
        for ti in tis:
            new_ti_entry = ImageGenerationStatisticTI(imgstat_id=statistic.id, ti=ti["name"])
            db.session.add(new_ti_entry)
        db.session.commit()


class CompiledImageGenStatsTotals(db.Model):
    """A table to store the compiled image generation statistics for the minute, hour, day, month, and total periods."""

    __tablename__ = "compiled_image_gen_stats_totals"
    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)
    minute_images = db.Column(db.Integer, nullable=False)
    minute_pixels = db.Column(db.Integer, nullable=False)
    hour_images = db.Column(db.Integer, nullable=False)
    hour_pixels = db.Column(db.Integer, nullable=False)
    day_images = db.Column(db.Integer, nullable=False)
    day_pixels = db.Column(db.Integer, nullable=False)
    month_images = db.Column(db.Integer, nullable=False)
    month_pixels = db.Column(db.BigInteger, nullable=False)
    total_images = db.Column(db.BigInteger, nullable=False)
    total_pixels = db.Column(db.BigInteger, nullable=False)


def get_compiled_imagegen_stats_totals() -> dict[str, dict[str, int]]:
    """Get the precompiled image generation statistics the minute, hour, day, month, and total periods.

    Returns:
        dict[str, dict[str, int]]: A dictionary containing the number of images and pixels generated for each period.
    """

    latest_entry = db.session.query(CompiledImageGenStatsTotals).order_by(CompiledImageGenStatsTotals.created.desc()).first()

    periods = ["minute", "hour", "day", "month", "total"]
    stats = {period: {"images": 0, "ps": 0} for period in periods}

    if latest_entry:
        for period in periods:
            stats[period]["images"] = getattr(latest_entry, f"{period}_images")
            stats[period]["ps"] = getattr(latest_entry, f"{period}_pixels")

    return stats


class CompiledImageGenStatsModels(db.Model):
    """A table to store the compiled image generation statistics for each model."""

    __tablename__ = "compiled_image_gen_stats_models"
    id = db.Column(db.Integer, primary_key=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True, nullable=False)
    model_id = db.Column(db.Integer, db.ForeignKey("known_image_models.id"), nullable=True)
    model = db.relationship("KnownImageModel", backref=db.backref("known_image_models", lazy=True))
    model_name = db.Column(db.String(255), nullable=False)
    model_state = db.Column(db.String(16), nullable=False)
    day_images = db.Column(db.Integer, nullable=False)
    month_images = db.Column(db.Integer, nullable=False)
    total_images = db.Column(db.Integer, nullable=False)


def get_compiled_imagegen_stats_models(model_state: str = "all") -> dict[str, dict[str, dict[str, int]]]:
    """Gets the precompiled image generation statistics for the day, month, and total periods for each model."""

    models: tuple[CompiledImageGenStatsModels] = ()

    # If model_state is "all" we get all models, if it's "known" we get only known models, if it's "custom" we get only custom models
    if model_state == "all":
        models = db.session.query(CompiledImageGenStatsModels.model_name).distinct().all()
    elif model_state == "known":
        models = (
            db.session.query(CompiledImageGenStatsModels.model_name)
            .filter(CompiledImageGenStatsModels.model_state == "known")
            .distinct()
            .all()
        )
    elif model_state == "custom":
        models = (
            db.session.query(CompiledImageGenStatsModels.model_name)
            .filter(CompiledImageGenStatsModels.model_state == "custom")
            .distinct()
            .all()
        )
    else:
        raise ValueError("Invalid model_state. Expected 'all', 'known', or 'custom'.")

    periods = ["day", "month", "total"]
    stats = {period: {model.model_name: 0 for model in models} for period in periods}

    for model in models:
        latest_entry = (
            db.session.query(CompiledImageGenStatsModels)
            .filter_by(model_name=model.model_name)
            .order_by(CompiledImageGenStatsModels.created.desc())
            .first()
        )

        if latest_entry:
            for period in periods:
                stats[period][model.model_name] = getattr(latest_entry, f"{period}_images")

    return stats
