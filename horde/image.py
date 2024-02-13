import base64
from io import BytesIO

import requests
from PIL import Image, UnidentifiedImageError

from horde.exceptions import ImageValidationFailed
from horde.logger import logger
from horde.r2 import upload_source_image


def convert_b64_to_pil(source_image_b64):
    base64_bytes = source_image_b64.encode("utf-8")
    try:
        img_bytes = base64.b64decode(base64_bytes)
    except Exception:
        return None
    try:
        image = Image.open(BytesIO(img_bytes))
    except UnidentifiedImageError:
        return None
    return image


def convert_pil_to_b64(source_image, quality=95):
    buffer = BytesIO()
    source_image.save(buffer, format="webp", exact=True)
    img_bytes = buffer.getvalue()
    return base64.b64encode(img_bytes).decode("utf-8")


# TODO: Merge with convert_b64_to_pil()
def convert_source_image_to_pil(source_image_b64):
    base64_bytes = source_image_b64.encode("utf-8")
    img_bytes = base64.b64decode(base64_bytes)
    image = Image.open(BytesIO(img_bytes))
    width, height = image.size
    resolution = width * height
    resolution_threshold = 3072 * 3072
    if resolution > resolution_threshold:
        except_msg = "Image size cannot exceed 3072*3072 pixels"
        # Not sure e exists here?
        raise ImageValidationFailed(except_msg, rc="SourceImageResolutionExceeded")
    quality = 100
    # We adjust the amount of compression based on the starting image to avoid running out of bandwidth
    if resolution > resolution_threshold * 0.9:
        quality = 50
    elif resolution > resolution_threshold * 0.8:
        quality = 60
    elif resolution > resolution_threshold * 0.6:
        logger.debug([resolution, resolution_threshold * 0.6])
        quality = 70
    elif resolution > resolution_threshold * 0.4:
        logger.debug([resolution, resolution_threshold * 0.4])
        quality = 80
    elif resolution > resolution_threshold * 0.3:
        logger.debug([resolution, resolution_threshold * 0.4])
        quality = 90
    elif resolution > resolution_threshold * 0.15:
        quality = 95

    return image, quality, width, height


def convert_source_image_to_webp(source_image_b64):
    """Convert img2img sources to 90% compressed webp, to avoid wasting bandwidth, while still supporting all types"""
    try:
        if source_image_b64 is None:
            return source_image_b64
        image, quality, width, height = convert_source_image_to_pil(source_image_b64)
        buffer = BytesIO()
        image.save(buffer, format="WebP", quality=quality, exact=True)
        final_image_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
        logger.debug(
            f"Received img2img source of {width}*{height}. "
            f"Started {round(len(source_image_b64) / 1000)} base64 kilochars. "
            f"Ended with quality {quality} = {round(len(final_image_b64) / 1000)} base64 kilochars",
        )
        return final_image_b64
    except ImageValidationFailed as err:
        raise err
    except Exception:
        raise ImageValidationFailed


def upload_source_image_to_r2(source_image_b64, uuid_string):
    """Convert source images to webp and uploads it to r2,
    to avoid wasting bandwidth, while still supporting all types
    """
    try:
        if source_image_b64 is None:
            return (None, None)
        image, quality, width, height = convert_source_image_to_pil(source_image_b64)
        filename = f"{uuid_string}.webp"
        download_url = upload_source_image(image, filename)
        return (download_url, image)
    except ImageValidationFailed as err:
        raise err
    except Exception:
        raise ImageValidationFailed


def ensure_source_image_uploaded(source_image_string, uuid_string, force_r2=False):
    if source_image_string.startswith("http"):
        try:
            with requests.get(source_image_string, stream=True, timeout=2) as r:
                size = r.headers.get("Content-Length", 0)
                # if not size:
                #     raise ImageValidationFailed("Source image URL must provide a Content-Length header")
                if int(size) / 1024 > 5000:
                    raise ImageValidationFailed("Provided image cannot be larger than 5Mb", rc="SourceImageSizeExceeded")
                mbs = 0
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        if mbs == 0:
                            img_data = chunk
                        else:
                            img_data += chunk
                        mbs += 1
                        if mbs > 5:
                            raise ImageValidationFailed("Provided image cannot be larger than 5Mb", rc="SourceImageSizeExceeded")
                try:
                    img = Image.open(BytesIO(img_data))
                except UnidentifiedImageError:
                    raise ImageValidationFailed("Url does not contain a valid image.", rc="SourceImageUrlInvalid")
                except Exception:
                    raise ImageValidationFailed("Something went wrong when opening image.", rc="SourceImageUnreadable")
                if force_r2:
                    logger.debug(f"uploading {img} {uuid_string}")
                    download_url = upload_source_image(img, uuid_string)
                    return (download_url, img, True)
        except Exception as err:
            if type(err) == ImageValidationFailed:
                raise err
            raise ImageValidationFailed("Something went wrong when retrieving image url.")
        return (source_image_string, img, False)
    download_url, img = upload_source_image_to_r2(source_image_string, uuid_string)
    return (download_url, img, True)


def calculate_image_tiles(image):
    """Returns the amount of 512x512 tiles the image
    is composed of
    image is a PIL object
    """
    width, height = image.size
    tiles_x = (width + 511) // 512
    tiles_y = (height + 511) // 512
    return tiles_x * tiles_y
