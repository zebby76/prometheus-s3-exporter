"""The S3 clients: one for collections, one sized for the connectivity probe."""

import logging

import boto3
from botocore.config import Config


def build_s3_client(
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
    config=None,
):
    """Build the S3 client. Raises so the caller can decide what to do."""
    return boto3.client(
        service_name="s3",
        region_name=aws_default_region or None,
        endpoint_url=aws_endpoint_url or None,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        config=config,
    )


def probe_timeout_for(configured, interval_seconds):
    """A whole check has to fit inside the interval it serves.

    Two attempts of connect plus read, so four timeouts: a probe must never
    still be running when the next one is due.
    """
    if not interval_seconds:
        return int(configured)
    return max(1, min(int(configured), interval_seconds // 4))


def build_probe_client(
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
    timeout_seconds,
):
    """A client sized for one round trip, not for a long listing.

    The collection client keeps the boto3 defaults, which are right for it: a
    transient error in the middle of 150 pages should be retried, not fail the
    hour. A probe wants the opposite.
    """
    return build_s3_client(
        aws_access_key_id,
        aws_secret_access_key,
        aws_default_region,
        aws_endpoint_url,
        config=Config(
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            # No botocore retry at all. Zero and not one, because botocore
            # counts retries here rather than attempts, so max_attempts=1 would
            # still permit a second request; this resolves to
            # total_max_attempts=1.
            #
            # The reason is the backoff, not the extra request. Both retry
            # modes sleep rand(0, 1) seconds before the first retry -- legacy
            # by way of `"base": "rand"` in _retry.json, and standard the same,
            # since the cheaper 0.05 scaling in its ExponentialBackoff sits
            # behind NEW_RETRIES_ENABLED, an internal flag that is off. That
            # sleep lands inside probe_duration_seconds and is indistinguishable
            # from real latency. ConnectivityProbe retries once by itself
            # instead: no sleep, and counted.
            retries={"max_attempts": 0, "mode": "standard"},
            # SO_KEEPALIVE on the socket, and nothing more: Linux waits
            # tcp_keepalive_time -- 7200s out of the box -- before the first
            # probe packet, so at any sane probe cadence this fires never. It is
            # set so that lowering that sysctl below the load balancer's idle
            # timeout takes effect. What keeps the connection warm today is the
            # probe traffic itself, which is why the interval has to stay under
            # that timeout -- see DEFAULT_CONNECTIVITY_INTERVAL_SECONDS.
            tcp_keepalive=True,
            max_pool_connections=1,
        ),
    )


def probe_client_for(
    shared_client,
    interval_seconds,
    timeout_seconds,
    aws_access_key_id,
    aws_secret_access_key,
    aws_default_region,
    aws_endpoint_url,
):
    """The probe's own client, or the shared one when there is no probe.

    The probe is auxiliary: a client it cannot build must not stop the exporter
    from starting, unlike the collection client whose failure is fatal.
    """
    if not interval_seconds:
        return shared_client

    try:
        client = build_probe_client(
            aws_access_key_id,
            aws_secret_access_key,
            aws_default_region,
            aws_endpoint_url,
            timeout_seconds,
        )
    except Exception as e:
        logging.error("Cannot build the probe client, using the shared one: %s", e)
        return shared_client

    logging.info(
        "Connectivity probe client: %ss timeout, single attempt, keepalive on",
        timeout_seconds,
    )
    return client
