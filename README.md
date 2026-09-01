# Prometheus S3 Exporter

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Exposes the size and object count of an S3 bucket as Prometheus metrics.

- metrics: `:9773/metrics`
- liveness: `:9774/healthz`
- readiness: `:9774/readyz`

## Usage sources

The cost of measuring a bucket depends entirely on how the figures are obtained.
Walking the bucket costs one request per 1000 objects; asking the storage system
for a number it already maintains costs one request, whatever the bucket size.

| Source | How | Requests per cycle | Needs |
| --- | --- | --- | --- |
| `storagegrid` | `GET /<bucket>?x-ntap-sg-usage` | 1 | the extension enabled on the grid |
| `minio` | MinIO Prometheus endpoint | 1 | admin JWT, or a public endpoint |
| `cloudwatch` | `AWS/S3` daily storage metrics | 1 | `cloudwatch:GetMetricStatistics` |
| `r2` | Cloudflare's `/r2/buckets/<bucket>/usage` | 1 | an API token with `Workers R2 Storage: Read` |
| `list` | `ListObjectsV2` | `ceil(objects / 1000)` | nothing beyond the S3 key pair |

`source: auto` (the default) probes them in that order at startup and keeps the
first that answers, so the cheap paths win where they exist and the listing
catches everything else. The listing always succeeds, so detection can never
come up empty.

`minio`, `cloudwatch` and `r2` stay inactive until their block is filled in under
`sources` in `config/collector.yml`: they authenticate differently from S3 and
an S3 access key alone cannot reach them. `r2` also accepts `CF_ACCOUNT_ID`,
`CF_API_TOKEN` and `CF_R2_JURISDICTION` from the environment, so one shared
config file can serve several buckets that differ only by credential.

`storagegrid` needs no extra credential -- the request is signed like any other
S3 call -- but the grid has to expose the extension. A grid that does not answers
HTTP 200 with an ordinary object listing rather than an error, so the probe
checks the content type and moves on rather than reading a megabyte of XML.

`r2` is the cheap path on Cloudflare R2, which otherwise has none: R2 speaks S3
but ships no usage extension, so `list` is the only option *over S3*. The account
API answers with the object count and the stored size in one request, whatever the
bucket holds -- 21.6s of listing for a 19 000-object bucket became 0.2s on the
account measured while writing this. It reads `payloadSize` plus its Infrequent
Access counterpart, and deliberately leaves `metadataSize` out, so the figure stays
comparable with what the listing computes.

Mind the trade: these are billing aggregates, computed on a schedule. The response
carries the `end` of the window it covers, which becomes `as_of`, so
`webtech_s3_usage_stale_seconds` reports the real lag instead of the age of our own
read -- 20 to 60 minutes on the account measured. Polling it more often does not
make it fresher. The source buys a constant cost, not freshness, and a bucket where
you need the truth at the moment you look at it is better left on `list`.

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
| `webtech_s3_bucket_probe_retries_total` | probes that had to reconnect, since start |
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
where a bucket-scoped policy does not grant `HeadBucket`. At the default 30s that
is 2880 requests a day, against 3600 for a 150 000-object bucket collected hourly.

The probe never runs from the scrape handler. A check hung on a network timeout
would time the whole scrape out and take every other metric with it, and each
client that scrapes -- an HA Prometheus pair, a stray `curl`, the healthcheck --
would trigger one of its own.

### Its own client, with a budget

The probe uses a client of its own rather than the collection's, because the two
want opposite things. A listing spread over 150 pages should ride out a transient
error at whatever cost; a probe is also a measurement, and anything it hides
inside its own duration makes that measurement useless.

So the probe's client is built with `probe_timeout` seconds for connect and for
read alike, and **botocore retries turned off entirely**. At the boto3 defaults a
probe could take minutes -- 60s connect plus 60s read, up to five attempts --
while being due every 30 seconds.

Turning retries off is about the backoff rather than the extra request. Both
retry modes sleep `rand(0, 1)` seconds before the first retry: `legacy` through
`"base": "rand"` in botocore's `_retry.json`, and `standard` identically, because
the cheaper 0.05 scaling in its `ExponentialBackoff` sits behind
`NEW_RETRIES_ENABLED`, an internal flag that is off by default. That sleep lands
squarely inside `webtech_s3_bucket_probe_duration_seconds`, where a real round
trip and a hidden backoff are indistinguishable.

The probe retries once by itself instead: **immediately, with no backoff, and
counted** in `webtech_s3_bucket_probe_retries_total`. A dropped idle connection
then costs one connection setup -- on the order of 120ms against a local grid --
rather than up to a second of sleep, and it shows up as a retry rather than as
latency. `probe_timeout` is clamped to a quarter of `connectivity_interval`, so a
whole check, two attempts of connect plus read, always fits inside one cycle.

**Keep `connectivity_interval` under the idle timeout of whatever sits in front
of the storage system.** Between two collections the probe is the only traffic on
its connection, so the interval is also what keeps that connection alive. This is
why the default is 30 and not 60: 60 is the most common idle timeout there is, and
a probe set to exactly that lands on the boundary and reconnects on roughly every
other cycle. `webtech_s3_bucket_probe_retries_total` tells you whether you are on
the wrong side of it:

```promql
increase(webtech_s3_bucket_probe_retries_total[1h])
```

Steadily above zero means the connection is being dropped between probes; shorten
the interval until it settles.

`tcp_keepalive` is set on the socket, but do not expect it to solve this. It sets
`SO_KEEPALIVE` and nothing more, and Linux waits `net.ipv4.tcp_keepalive_time` --
7200 seconds by default -- before the first keepalive packet, so at any sane probe
cadence it never fires. It is there so that lowering that sysctl below the idle
timeout takes effect, if your cluster allows those keepalive sysctls
unprivileged.

**Consequence, deliberately taken:** two failed attempts in a row flip `reachable`
to 0 and answer `/readyz` with `503` until the next cycle. Keep the readiness
probe's `failureThreshold` at 3 or more -- the Kubernetes default -- so an
isolated cycle does not pull the pod out of service.

### Reading `probe_duration_seconds`

The gauge measures the **outbound** call to the storage system, not the time to
serve `/readyz` -- which only reads a frozen result from memory and never touches
S3. Comparing them is comparing milliseconds to microseconds.

When instances that look identical report very different figures, read the shape
rather than the average:

```promql
stddev_over_time(webtech_s3_bucket_probe_duration_seconds[1h])
  / avg_over_time(webtech_s3_bucket_probe_duration_seconds[1h])
```

- **Around 1 or above.** The series is a mixture of two populations, not a
  latency. Values spread evenly up to a hard ceiling around one second are the
  `rand(0, 1)` retry backoff described above, from a version or a client that
  still has botocore retries on. The average in that case describes nothing that
  ever happens: a series alternating between 8ms and 1.1s averages out to a
  plausible-looking 337ms that no single probe ever took.
- **Near zero, each instance steady at its own value.** A genuine per-path or
  per-bucket difference. On StorageGRID, start with the bucket's consistency
  level: `strong-global` coordinates every site where `strong-site` stays local.

The two endpoints answer different questions:

- **`/healthz`** is liveness: is the process responding. Independent of S3 by
  design, since restarting the pod does not fix an unreachable bucket and only
  throws away the metrics right when they are wanted. This drives the container
  `HEALTHCHECK`.
- **`/readyz`** is readiness: `200` while the bucket is reachable, `503` once the
  probe says it is not, `200` before the first probe has run. Wire an OpenShift
  readiness probe to it if you want the pod pulled out of service during an
  outage; it is safe to ignore otherwise.

Set `connectivity_interval: 0` to switch the probe off entirely: no thread, no
requests, no probe metrics, and `/readyz` is not routed.

## Access logs

Requests are logged as logfmt on the same key set the Apache tier emits, so one
parser reads both tiers and a request can be followed across them on `vxid` and
`via`:

```text
ts=2026-08-31T18:57:03.720+0000 rid=4285d115ed6745bd class=health src=exporter \
status=200 dur_us=468 method=GET uri="/readyz" qs="-" target="/readyz" bytes=37 \
host=exporter:9774 via=apache vxid=987654 xff="10.0.0.1" proto=https \
ua="kube-probe/1.29"
```

| Key | Value |
| --- | --- |
| `rid` | the inbound `X-Request-Id`, or one minted per request |
| `class` | `metrics`, `health` (`/healthz`, `/readyz`), or `other` |
| `dur_us` | time in the application and the response write, microseconds |
| `uri` / `target` / `qs` | request target, path alone, query alone |
| `bytes` | response body size; `0` for an empty body, never `-` |
| `via` `vxid` `xff` `proto` | the `X-Smals-*` headers, `-` when absent |

Access lines go to their own `access` logger, unprefixed and not propagated:
mixing them with the process-wide format would put a second timestamp in front
of each line and break the parse. Lower them with:

```python
logging.getLogger("access").setLevel(logging.WARNING)
```

A Prometheus scrape every 15s plus the container healthcheck is roughly 7 000
lines a day per instance.

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
