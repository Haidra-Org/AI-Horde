import base64
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from horde.logger import logger


def convert_b64_to_pil(source_image_b64):
    base64_bytes = source_image_b64.encode('utf-8')
    img_bytes = base64.b64decode(base64_bytes)
    try:
        image = Image.open(BytesIO(img_bytes))          
    except UnidentifiedImageError as err:
        return None
    return image