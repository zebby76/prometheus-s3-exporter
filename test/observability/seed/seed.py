"""Create the demo buckets and fill them with random content.

One bucket per exporter instance, as in production. Sizes differ by orders of
magnitude so the listing's cost shows on the collection metrics. Idempotent:
a bucket that already holds its objects is left alone, a partial one is
completed, so `make obs/seed` can run again after an interruption.

BUCKETS is `name:count:min_bytes:max_bytes`, comma-separated.
"""

import logging
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

DEFAULT_BUCKETS = (
    "bucket-small:200:1024:524288,"
    "bucket-medium:5000:1024:65536,"
    "bucket-large:50000:1024:8192,"
    "bucket-tls:100:1024:65536"
)
WORKERS = int(os.getenv("SEED_WORKERS", "32"))


def parse(spec):
    for item in filter(None, (part.strip() for part in spec.split(","))):
        name, count, low, high = item.split(":")
        yield name, int(count), int(low), int(high)


def existing_keys(client, bucket):
    keys = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys.update(obj["Key"] for obj in page.get("Contents", ()))
    return keys


def seed(client, name, count, low, high):
    try:
        client.create_bucket(Bucket=name)
        logging.info("%s: created", name)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except client.exceptions.BucketAlreadyExists:
        pass

    present = existing_keys(client, name)
    # Spread over prefixes, like real data, so a listing is not one flat run.
    wanted = [f"{i % 100:02d}/obj-{i:07d}.bin" for i in range(count)]
    missing = [key for key in wanted if key not in present]
    if not missing:
        logging.info("%s: %s objects already there", name, count)
        return

    logging.info("%s: uploading %s of %s objects", name, len(missing), count)

    def put(key):
        client.put_object(
            Bucket=name, Key=key, Body=os.urandom(random.randint(low, high))
        )

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for done, _ in enumerate(pool.map(put, missing), start=1):
            if done % 5000 == 0:
                logging.info("%s: %s/%s", name, done, len(missing))
    logging.info("%s: done", name)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        config=Config(max_pool_connections=WORKERS),
    )
    for spec in parse(os.getenv("BUCKETS") or DEFAULT_BUCKETS):
        seed(client, *spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
