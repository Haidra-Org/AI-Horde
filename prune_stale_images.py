import logging
import boto3
from datetime import datetime, timedelta, timezone
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import ClientError
from dotenv import load_dotenv
import patreon
import os
load_dotenv()


sr3 = boto3.resource('s3', endpoint_url="https://a223539ccf6caa2d76459c9727d276e6.r2.cloudflarestorage.com")
cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=120)
for bucket in [
    sr3.Bucket('stable-horde'), 
    sr3.Bucket('stable-horde-source-images')
]:
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for obj in bucket.objects.all():
            last_modified = obj.last_modified.replace(tzinfo=timezone.utc)
            if last_modified < cutoff_time:
                futures.append(executor.submit(obj.delete))
                if len(futures) >= 1000:
                    for future in tqdm(futures):
                        future.result()
                    futures = []
        for future in tqdm(futures):
            future.result()
