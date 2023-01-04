import uuid
from datetime import datetime
from horde.logger import logger
import boto3
from botocore.exceptions import ClientError

s3_client = boto3.client('s3', endpoint_url="https://a223539ccf6caa2d76459c9727d276e6.r2.cloudflarestorage.com")

@logger.catch(reraise=True)
def generate_presigned_url(client_method, method_parameters, expires_in):
    """
    Generate a presigned Amazon S3 URL that can be used to perform an action.

    :param s3_client: A Boto3 Amazon S3 client.
    :param client_method: The name of the client method that the URL performs.
    :param method_parameters: The parameters of the specified client method.
    :param expires_in: The number of seconds the presigned URL is valid for.
    :return: The presigned URL.
    """
    try:
        url = s3_client.generate_presigned_url(
            ClientMethod=client_method,
            Params=method_parameters,
            ExpiresIn=expires_in
        )
    except ClientError:
        logger.exception(
            f"Couldn't get a presigned URL for client method {client_method}", )
        raise
    # logger.debug(url)
    return url

def generate_procgen_upload_url(procgen_id):
    return generate_presigned_url("put_object", {'Bucket': "stable-horde", 'Key': f"{procgen_id}.webp"}, 1800)

def generate_procgen_download_url(procgen_id):
    return generate_presigned_url("get_object", {'Bucket': "stable-horde", 'Key': f"{procgen_id}.webp"}, 1800)

def delete_procgen_image(procgen_id):
    response = s3_client.delete_object(
        Bucket="stable-horde",
        Key=f"{procgen_id}.webp"
    )

def delete_source_image(source_image_uuid):
    response = s3_client.delete_object(
        Bucket="stable-horde-source-images",
        Key=f"{source_image_uuid}.webp"
    )


def upload_source_image(filename):
    try:
        response = s3_client.upload_file(
            filename, "stable-horde-source-images", filename
        )
    except ClientError as e:
        logger.error(f"Error encountered while uploading {filename}: {e}")
        return False
    return generate_img_download_url(filename, "stable-horde-source-images")

def generate_img_download_url(filename, bucket="stable-horde"):
    return generate_presigned_url("get_object", {'Bucket': bucket, 'Key': filename}, 1800)

def generate_img_upload_url(filename, bucket="stable-horde"):
    return generate_presigned_url("put_object", {'Bucket': bucket, 'Key': filename}, 1800)

def generate_uuid_img_upload_url(img_uuid, imgtype):
    return generate_img_upload_url(f"{img_uuid}.{imgtype}")

def generate_uuid_img_download_url(img_uuid, imgtype):
    return generate_img_download_url(f"{img_uuid}.{imgtype}")

