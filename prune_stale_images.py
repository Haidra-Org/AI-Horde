import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

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
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=120)
        logger.info("Image Pruner: Starting Next Cleanup Iteration...")
        for bucket in [
            sr3.Bucket("stable-horde"),
            sr3.Bucket("stable-horde-source-images"),
        ]:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for obj in bucket.objects.all():
                    last_modified = obj.last_modified.replace(tzinfo=timezone.utc)
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
        time.sleep(30)
    except Exception:
        time.sleep(30)
