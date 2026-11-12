# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import boto3
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

PASS_INTERVAL_ENV = "IMAGE_PRUNER_PASS_INTERVAL_SECONDS"
"""The environment variable holding the pruner's sleep between passes, in seconds."""

PASS_INTERVAL_SECONDS = int(os.getenv(PASS_INTERVAL_ENV, "600"))
"""How long the pruner sleeps between passes over the buckets.

Every pass lists both buckets in full, and the object store bills each list request. Objects are pruned 120 minutes
after their last write, so the default ten minute interval lets an object live at most ten minutes longer.
"""

r2_transient_account = os.getenv(
    "R2_TRANSIENT_ACCOUNT",
    "https://a223539ccf6caa2d76459c9727d276e6.r2.cloudflarestorage.com",
)
sr3 = boto3.resource(
    "s3",
    endpoint_url=r2_transient_account,
)
while True:
    try:
        cutoff_time = datetime.now(UTC) - timedelta(minutes=120)
        logger.info("Image Pruner: Starting Next Cleanup Iteration...")
        for bucket in [
            sr3.Bucket("stable-horde"),
            sr3.Bucket("stable-horde-source-images"),
        ]:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for obj in bucket.objects.all():
                    last_modified = obj.last_modified.replace(tzinfo=UTC)
                    if last_modified < cutoff_time:
                        futures.append(executor.submit(obj.delete))
                        if len(futures) >= 1000:
                            for future in futures:
                                future.result()
                            logger.info(f"Image Pruner: Bucket {bucket} Deleted: {len(futures)}")
                            futures = []
                for future in futures:
                    future.result()
                logger.info(f"Image Pruner: Bucket {bucket} Deleted: {len(futures)}")
        time.sleep(PASS_INTERVAL_SECONDS)
    except Exception as e:
        logger.exception(f"Image Pruner: Cleanup iteration failed with error: {e}, retrying in 30 seconds")
        time.sleep(30)
