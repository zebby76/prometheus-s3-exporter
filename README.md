# Prometheus S3 Exporter

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Exposes the size and object count of an S3 bucket as Prometheus metrics.

- image: `zebby76/prometheus-s3-exporter` (linux/amd64 + linux/arm64)
- metrics: `:9773/metrics`
- liveness: `:9774/health`
- readiness: `:9774/ready`

## Usage sources

The cost of measuring a bucket depends entirely on how the figures are obtained.
Walking the bucket costs one request per 1000 objects; asking the storage system
for a number it already maintains costs one request, whatever the bucket size.

| Source | How | Requests per cycle | Needs |
| --- | --- | --- | --- |
| `storagegrid` | `GET /<bucket>?x-ntap-sg-usage` | 1 | the extension enabled on the grid |
| `minio` | MinIO Prometheus endpoint | 1 | admin JWT, or a public endpoint |
| `cloudwatch` | `AWS/S3` daily storage metrics | 1 | `cloudwatch:GetMetricStatistics` |
| `list` | `ListObjectsV2` | `ceil(objects / 1000)` | nothing beyond the S3 key pair |

`source: auto` (the default) probes them in that order at startup and keeps the
first that answers, so the cheap paths win where they exist and the listing
catches everything else. The listing always succeeds, so detection can never
come up empty.

`minio` and `cloudwatch` stay inactive until their block is filled in under
`sources` in `config/collector.yml`: both authenticate differently from S3 and
an S3 access key alone cannot reach them.

`storagegrid` needs no extra credential -- the request is signed like any other
S3 call -- but the grid has to expose the extension. A grid that does not answers
HTTP 200 with an ordinary object listing rather than an error, so the probe
checks the content type and moves on rather than reading a megabyte of XML.

The source in use is published as a label on `webtech_s3_exporter_info`, so a
fallback stays visible in Prometheus rather than hiding in the configuration.
After `max_failures` consecutive failures the source is detected again, so a
provider that was briefly unreachable at startup does not pin the exporter to
the listing until the next restart.

Set `COLLECTOR_SOURCE` to override `source` at runtime without editing the
mounted ConfigMap.

## Metrics

| Metric | Meaning |
| --- | --- |
| `webtech_s3_bucket_size_bytes` | bucket size in bytes |
| `webtech_s3_bucket_size_kbytes` | **deprecated**, removed at the next major |
| `webtech_s3_bucket_count_total` | number of objects |
| `webtech_s3_usage_stale_seconds` | age of the figures being served |
| `webtech_s3_collect_duration_seconds` | duration of the last collection |
| `webtech_s3_collect_interval_seconds` | configured interval, so alerts can use a ratio |
| `webtech_s3_bucket_reachable` | 1 when the last connectivity probe reached the bucket |
| `webtech_s3_bucket_probe_duration_seconds` | latency of the last probe |
| `webtech_s3_bucket_probe_age_seconds` | age of the last probe |
| `webtech_s3_exporter_status` | 1 when the last collection succeeded |
| `webtech_s3_exporter_up` | 1 when the exporter is answering |

`webtech_s3_usage_stale_seconds` is worth alerting on. Sources backed by a
background scanner report figures that lag by minutes (StorageGRID, MinIO) or by
a day (CloudWatch), and the value keeps growing while collection is failing.

## Connectivity probe

The collection interval is sized for the cost of measuring a bucket, which on a
large one means tens of minutes. `webtech_s3_exporter_status` therefore only
tells you about a collection that may be an hour old, and an outage would stay
invisible until the next cycle.

A separate thread answers reachability on its own clock -- one `HeadBucket` every
`connectivity_interval` seconds, falling back to `ListObjectsV2` with `MaxKeys=1`
where a bucket-scoped policy does not grant `HeadBucket`. At the default 60s that
is 1440 requests a day, against 3600 for a 150 000-object bucket collected hourly.

The probe never runs from the scrape handler. A check hung on a network timeout
would time the whole scrape out and take every other metric with it, and each
client that scrapes -- an HA Prometheus pair, a stray `curl`, the healthcheck --
would trigger one of its own.

The two endpoints answer different questions:

- **`/health`** is liveness: is the process responding. Independent of S3 by
  design, since restarting the pod does not fix an unreachable bucket and only
  throws away the metrics right when they are wanted. This drives the container
  `HEALTHCHECK`.
- **`/ready`** is readiness: `200` while the bucket is reachable, `503` once the
  probe says it is not, `200` before the first probe has run. Wire an OpenShift
  readiness probe to it if you want the pod pulled out of service during an
  outage; it is safe to ignore otherwise.

Set `connectivity_interval: 0` to switch the probe off entirely: no thread, no
requests, no probe metrics, and `/ready` is not routed.

## Alerting

```yaml
groups:
  - name: prometheus-s3-exporter
    rules:
      - alert: S3CollectionFailing
        expr: webtech_s3_exporter_status == 0
        for: 15m
        annotations:
          summary: "{{ $labels.name }}: the exporter cannot read the bucket"

      - alert: S3BucketUnreachable
        expr: webtech_s3_bucket_reachable == 0
        for: 5m
        annotations:
          summary: "{{ $labels.name }}: the bucket cannot be reached"

      # A dead probe thread would leave `bucket_reachable` frozen at its last
      # value and quietly trusted.
      - alert: S3ProbeStalled
        expr: webtech_s3_bucket_probe_age_seconds > 600
        for: 10m
        annotations:
          summary: "{{ $labels.name }}: the connectivity probe stopped running"

      - alert: S3UsageStale
        expr: webtech_s3_usage_stale_seconds > 3600
        for: 10m
        annotations:
          summary: "{{ $labels.name }}: usage figures are over an hour old"

      # Only the `list` source scales with the bucket: one request per 1000
      # objects, and about 86 us of parsing per object. Comparing against the
      # configured interval rather than a fixed number of seconds keeps one
      # rule valid across buckets that differ by two orders of magnitude.
      - alert: S3CollectionSlow
        expr: >
          webtech_s3_collect_duration_seconds
            / webtech_s3_collect_interval_seconds > 0.5
        for: 30m
        annotations:
          summary: "{{ $labels.name }}: a collection eats half its interval"
```

## Sizing the interval

With the `list` source the cost of a cycle tracks the **number of objects**, not
the number of bytes: a 74 GB bucket holding 1360 objects is cheaper to measure
than a 2 GB one holding 150 000. Budget roughly one request per 1000 objects and
86 us of parsing per object, plus one round-trip per request.

| Objects | Requests | Collection | Share of a 1 min interval |
| --- | --- | --- | --- |
| 1 000 | 1 | 0.1 s | 0.2 % |
| 10 000 | 10 | 0.9 s | 1.5 % |
| 150 000 | 150 | ~16 s | 27 % |
| 700 000 | 700 | ~60 s | 100 % |

Past roughly a quarter of the interval the exporter logs a warning on every
cycle. Raising `interval` is almost always the right answer: a bucket size is a
capacity figure, and few dashboards need it fresher than every 5 to 15 minutes.
Each deployment watches a single bucket, so the interval can be tuned per bucket
rather than globally.

## Build

```bash
make build
```

## Test

```bash
make unit    # unit tests, no containers
make test    # full stack, asserts the exposed metrics, then tears down
```

## Local development

```bash
make up      # exporter + MinIO, bucket preloaded from test/assets
make logs
make down
```

Or without containers:

```bash
python -m venv dev-venv
source ./dev-venv/bin/activate
pip install -e '.[dev]'
python exporter/__main__.py -v
```

## Lint

```bash
make lint
```

## Release

```bash
make release-info                 # detected remote, repo slug, branch, tags
make release VERSION=x.y.z        # commit, signed tag, push, GitHub release
make retag VERSION=x.y.z          # move a tag and refresh its release
make notes VERSION=x.y.z          # regenerate release notes
make build-version VERSION=x.y.z  # trigger a build via workflow_dispatch
```

Run `make` with no target for the full list.

## About this fork

Published from the author's original project so the image can be pulled on an
arm64 cluster: the upstream build targets `linux/amd64` only, this one ships a
manifest list covering `linux/amd64` and `linux/arm64`. The code is otherwise
unchanged -- metric names included, so dashboards and alert rules written
against either build are interchangeable.
