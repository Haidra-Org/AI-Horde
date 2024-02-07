import json
import os
from io import BytesIO
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from PIL import Image

from horde.logger import logger

r2_transient_account = os.getenv(
    "R2_TRANSIENT_ACCOUNT",
    "https://a223539ccf6caa2d76459c9727d276e6.r2.cloudflarestorage.com",
)
r2_permanent_account = os.getenv(
    "R2_PERMANENT_ACCOUNT",
    "https://a223539ccf6caa2d76459c9727d276e6.r2.cloudflarestorage.com",
)
r2_transient_bucket = os.getenv("R2_TRANSIENT_BUCKET", "stable-horde")
r2_permanent_bucket = os.getenv("R2_PERMANENT_BUCKET", "stable-horde")
r2_source_image_bucket = os.getenv("R2_SOURCE_IMAGE_BUCKET", "stable-horde-source-images")

s3_client = boto3.client("s3", endpoint_url=r2_transient_account)
s3_client_shared = boto3.client(
    "s3",
    endpoint_url=r2_permanent_account,
    aws_access_key_id=os.getenv("SHARED_AWS_ACCESS_ID"),
    aws_secret_access_key=os.getenv("SHARED_AWS_ACCESS_KEY"),
)
old_r2 = boto3.client(
    "s3",
    endpoint_url="https://eu2.contabostorage.com",
    aws_access_key_id=os.getenv("OLD_AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("OLD_AWS_SECRET_ACCESS_KEY"),
)

# Lists shared bucket contents
# for key in s3_client_shared.list_objects(Bucket=r2_transient_bucket)['Contents']:
#     logger.debug(key['Key'])


@logger.catch(reraise=True)
def generate_presigned_url(client, client_method, method_parameters, expires_in=1800):
    """
    Generate a presigned Amazon S3 URL that can be used to perform an action.

    :param s3_client: A Boto3 Amazon S3 client.
    :param client_method: The name of the client method that the URL performs.
    :param method_parameters: The parameters of the specified client method.
    :param expires_in: The number of seconds the presigned URL is valid for.
    :return: The presigned URL.
    """
    try:
        url = client.generate_presigned_url(ClientMethod=client_method, Params=method_parameters, ExpiresIn=expires_in)
    except ClientError:
        logger.exception(
            f"Couldn't get a presigned URL for client method {client_method}",
        )
        raise
    # logger.debug(url)
    return url


def generate_procgen_upload_url(procgen_id, shared=False):
    client = s3_client
    if shared:
        client = s3_client_shared
    return generate_presigned_url(
        client=client,
        client_method="put_object",
        method_parameters={"Bucket": r2_transient_bucket, "Key": f"{procgen_id}.webp"},
        expires_in=1800,
    )


def generate_procgen_download_url(procgen_id, shared=False):
    client = s3_client
    if shared:
        client = s3_client_shared
    # if not file_exists(client,  f"{procgen_id}.webp"):
    #     client = old_r2
    return generate_presigned_url(
        client=client,
        client_method="get_object",
        method_parameters={"Bucket": r2_transient_bucket, "Key": f"{procgen_id}.webp"},
        expires_in=1800,
    )


def delete_procgen_image(procgen_id):
    s3_client.delete_object(Bucket=r2_transient_bucket, Key=f"{procgen_id}.webp")


def delete_source_image(source_image_uuid):
    s3_client.delete_object(Bucket=r2_source_image_bucket, Key=f"{source_image_uuid}.webp")


def upload_image(client, bucket, image, filename, quality=100):
    image_io = BytesIO()
    image.save(image_io, format="WebP", quality=quality, exact=True)
    image_io.seek(0)
    try:
        client.upload_fileobj(image_io, bucket, filename)
    except ClientError as err:
        logger.error(f"Error encountered while uploading {filename}: {err}")
        return False
    return generate_img_download_url(filename, r2_source_image_bucket)


def download_image(client, bucket, key):
    # if not file_exists(client, f"{procgen_id}.webp"):
    #     client = old_r2
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        img = response["Body"].read()
        return Image.open(BytesIO(img))
    except ClientError as e:
        logger.error(f"Error encountered while downloading {key}: {e}")
        return None


def download_procgen_image(procgen_id, shared=False):
    if shared:
        return download_image(s3_client_shared, r2_permanent_bucket, f"{procgen_id}.webp")
    return download_image(s3_client, r2_transient_bucket, f"{procgen_id}.webp")


def download_source_image(wp_id, shared=False):
    return download_image(s3_client, r2_source_image_bucket, f"{wp_id}_src.webp")


def download_source_mask(wp_id, shared=False):
    return download_image(s3_client, r2_source_image_bucket, f"{wp_id}_msk.webp")


def upload_source_image(image, filename):
    return upload_image(s3_client, r2_source_image_bucket, image, filename, quality=50)


def upload_generated_image(image, filename):
    return upload_image(
        s3_client,
        r2_transient_bucket,
        image,
        filename,
        quality=95,
    )


def upload_shared_generated_image(image, filename):
    return upload_image(
        s3_client_shared,
        r2_permanent_bucket,
        image,
        filename,
        quality=95,
    )


def upload_shared_metadata(filename):
    try:
        s3_client_shared.upload_file(filename, r2_permanent_bucket, filename)
    except ClientError as e:
        logger.error(f"Error encountered while uploading metadata {filename}: {e}")
        return False


def upload_prompt(prompt_dict):
    filename = f"{uuid4()}.json"
    json_object = json.dumps(prompt_dict, indent=4)
    # Writing to sample.json
    with open(filename, "w") as f:
        f.write(json_object)
    try:
        s3_client.upload_file(filename, "prompts", filename)
        os.remove(filename)
    except Exception as err:
        logger.error(f"Error encountered while uploading prompt {filename}: {err}")
        os.remove(filename)
        return False


def generate_img_download_url(filename, bucket=r2_transient_bucket):
    return generate_presigned_url(s3_client, "get_object", {"Bucket": bucket, "Key": filename}, 1800)


def generate_img_upload_url(filename, bucket=r2_transient_bucket):
    return generate_presigned_url(s3_client, "put_object", {"Bucket": bucket, "Key": filename}, 1800)


def generate_uuid_img_upload_url(img_uuid, imgtype):
    return generate_img_upload_url(f"{img_uuid}.{imgtype}")


def generate_uuid_img_download_url(img_uuid, imgtype):
    return generate_img_download_url(f"{img_uuid}.{imgtype}")


def check_file(client, bucket, filename):
    try:
        return client.head_object(Bucket=bucket, Key=filename)
    except ClientError as e:
        return int(e.response["Error"]["Code"]) != 404


def check_shared_image(filename):
    return isinstance(check_file(s3_client_shared, r2_transient_bucket, filename), dict)


def file_exists(client, bucket, filename):
    # If the return of check_file is an int, it means it encountered an error
    return not isinstance(check_file(client, bucket, filename), int)
