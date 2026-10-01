# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import os
from collections.abc import Collection, Mapping
from io import BytesIO
from uuid import uuid4

import boto3
import logfire
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
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
    with logfire.span("horde.r2.upload_image", bucket=bucket, filename=filename):
        return _upload_image(client, bucket, image, filename, quality)


def _upload_image(client, bucket, image, filename, quality=100):
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
    with logfire.span("horde.r2.download_image", bucket=bucket, key=key):
        return _download_image(client, bucket, key)


def _download_image(client, bucket, key):
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
    with logfire.span("horde.r2.upload_prompt"):
        return _upload_prompt(prompt_dict)


def _upload_prompt(prompt_dict):
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


# Moderation evidence text. These objects live in their own bucket under EVIDENCE_OBJECT_PREFIX and share no client,
# bucket or key scheme with the image and prompt objects above.

EVIDENCE_ACCOUNT_ENV: str = "R2_EVIDENCE_ACCOUNT"
"""The environment variable holding the evidence store's S3 endpoint URL."""
EVIDENCE_BUCKET_ENV: str = "R2_EVIDENCE_BUCKET"
"""The environment variable holding the evidence bucket name."""
EVIDENCE_ACCESS_KEY_ID_ENV: str = "EVIDENCE_AWS_ACCESS_KEY_ID"
"""The environment variable holding the access key id of the token scoped to the evidence bucket."""
EVIDENCE_SECRET_ACCESS_KEY_ENV: str = "EVIDENCE_AWS_SECRET_ACCESS_KEY"
"""The environment variable holding the secret of the token scoped to the evidence bucket."""
EVIDENCE_REGION_ENV: str = "R2_EVIDENCE_REGION"
"""The environment variable holding the signing region of the evidence store.

R2 signs with the region ``auto``, the default. Another S3-compatible store rejects a signature for a region it does not
serve, so a deployment on one sets its region here.
"""
EVIDENCE_OBJECT_PREFIX: str = "evidence/"
"""The key prefix of every evidence object; ``evidence_object_key`` builds keys only under it."""
EVIDENCE_CONNECT_TIMEOUT_SECONDS: int = 3
"""How long an evidence request waits to connect.

Only the background upload and retention jobs call the store, and a short timeout lets a tick give up on an
unreachable store and retry on the next one.
"""
EVIDENCE_READ_TIMEOUT_SECONDS: int = 10
"""How long an evidence request waits for a response; an object is at most a few tens of kilobytes."""
EVIDENCE_MAX_ATTEMPTS: int = 2
"""The attempts per evidence request, including the first; the jobs retry anything left on their next tick."""
EVIDENCE_MAX_POOL_CONNECTIONS: int = 8
"""The connections the evidence client keeps; one job uploads at a time, so a small pool suffices."""
EVIDENCE_DELETE_BATCH_SIZE: int = 1000
"""The most keys one ``DeleteObjects`` request takes, the S3 limit."""
EVIDENCE_TEXT_URL_SECONDS: int = 600
"""How long a moderator's signed evidence text URL stays valid.

Ten minutes covers opening a listing page and reading its events, while a leaked URL stops working soon after.
"""


def build_evidence_client(environ: Mapping[str, str] = os.environ):
    """Return the evidence store client, or None unless the endpoint, bucket, access key id and secret are all set.

    Without credentials boto3 falls back to the default credential chain, which the image clients use, so a partial
    configuration builds no client rather than signing evidence requests with another token. Logs one warning naming
    the unset variables.

    Args:
        environ: Variables to read; the process environment by default.
    """
    required = (
        EVIDENCE_ACCOUNT_ENV,
        EVIDENCE_BUCKET_ENV,
        EVIDENCE_ACCESS_KEY_ID_ENV,
        EVIDENCE_SECRET_ACCESS_KEY_ENV,
    )
    missing = [variable for variable in required if not environ.get(variable)]
    if missing:
        logger.warning(f"{', '.join(missing)} unset; moderation evidence text stays in the database")
        return None
    return boto3.client(
        "s3",
        endpoint_url=environ[EVIDENCE_ACCOUNT_ENV],
        aws_access_key_id=environ[EVIDENCE_ACCESS_KEY_ID_ENV],
        aws_secret_access_key=environ[EVIDENCE_SECRET_ACCESS_KEY_ENV],
        region_name=environ.get(EVIDENCE_REGION_ENV) or "auto",
        config=Config(
            signature_version="s3v4",
            connect_timeout=EVIDENCE_CONNECT_TIMEOUT_SECONDS,
            read_timeout=EVIDENCE_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": EVIDENCE_MAX_ATTEMPTS},
            max_pool_connections=EVIDENCE_MAX_POOL_CONNECTIONS,
        ),
    )


r2_evidence_bucket: str | None = os.getenv(EVIDENCE_BUCKET_ENV) or None
"""The evidence bucket, or None when unset."""
evidence_client = build_evidence_client()
"""The evidence store client, or None when any of its endpoint, bucket and credential variables is unset.

Without it, prompt text stays in the database (``pending``), the upload job idles and the listing returns no text URL.
"""


def evidence_object_key(event_id: int) -> str:
    """Return the object key of one event's evidence text; the only way an evidence key is built."""
    return f"{EVIDENCE_OBJECT_PREFIX}{int(event_id)}.json"


def put_evidence_text(event_id: int, body: bytes) -> bool:
    """Store one event's canonical evidence text object.

    Returns:
        Whether the store accepted the object. False when no client is configured or the request failed; the failure
        is logged by type only, never with the body.
    """
    if evidence_client is None:
        return False
    try:
        evidence_client.put_object(
            Bucket=r2_evidence_bucket,
            Key=evidence_object_key(event_id),
            Body=body,
            ContentType="application/json",
        )
    except (BotoCoreError, ClientError) as error:
        logger.warning(f"Evidence text upload failed ({type(error).__name__})")
        return False
    return True


def delete_evidence_texts(event_ids: Collection[int]) -> set[int]:
    """Delete the evidence text objects of the given events.

    A key that does not exist counts as deleted, so deleting an event that never had an object succeeds.

    Returns:
        The IDs whose object could not be deleted; every ID when no client is configured.

    Raises:
        BotoCoreError: The store could not be reached.
        ClientError: The store refused a whole request.
    """
    ids = sorted({int(event_id) for event_id in event_ids})
    if evidence_client is None:
        return set(ids)
    failed: set[int] = set()
    for start in range(0, len(ids), EVIDENCE_DELETE_BATCH_SIZE):
        chunk = {evidence_object_key(event_id): event_id for event_id in ids[start : start + EVIDENCE_DELETE_BATCH_SIZE]}
        response = evidence_client.delete_objects(
            Bucket=r2_evidence_bucket,
            Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
        )
        for error in response.get("Errors", []):
            if error.get("Code") == "NoSuchKey":
                continue
            event_id = chunk.get(error.get("Key", ""))
            if event_id is not None:
                failed.add(event_id)
    return failed


def evidence_text_url(event_id: int) -> str | None:
    """Return a short-lived signed URL to one event's evidence text, or None when no client is configured.

    Signing is local, so this makes no request. The response is marked private and uncacheable, and served as UTF-8
    JSON.
    """
    if evidence_client is None:
        return None
    return generate_presigned_url(
        evidence_client,
        "get_object",
        {
            "Bucket": r2_evidence_bucket,
            "Key": evidence_object_key(event_id),
            "ResponseCacheControl": "private, no-store",
            "ResponseContentType": "application/json; charset=utf-8",
        },
        expires_in=EVIDENCE_TEXT_URL_SECONDS,
    )
