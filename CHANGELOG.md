<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->
<!-- Keep a Changelog repeats "### Added" and "### Fixed" under every version, which MD024 flags by
     default. siblings_only narrows it to duplicates under the SAME parent, which is the case that
     actually signals a mistake. Scoped to this file: a .markdownlint.yml at the root would replace
     super-linter's bundled defaults wholesale and re-enable rules the other documents rely on. -->

# Changelog

All notable changes to this image are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
the image's own `x.y.z`.

This file is the durable copy of what a release says. `make retag` and `make notes` regenerate the
GitHub release notes from the previous tag, which discards anything hand-written in them; what is
written here survives.

Releases up to and including `4.2.0` predate this file and are documented only by their generated
notes.

## [Unreleased]

Nothing pending.

## [5.1.0]

### Added

- The connectivity probe tells a DNS failure from a network one. botocore prints the same
  `Could not connect to the endpoint URL` for both; a failed check now logs every attempt with its
  own duration and root cause (`caused by gaierror: …`, `caused by ConnectionRefusedError: …`),
  and a reason: `dns`, `connect_timeout`, `connect`, `read_timeout`, `http_5xx`, `http_4xx`,
  `other`. The reason is also in the `503` body of `/readyz`.
- `webtech_s3_bucket_probe_errors_total{reason}`: failed probe attempts by reason, retried ones
  included, every reason present from the start.
- `webtech_s3_bucket_dns_duration_seconds` and `webtech_s3_bucket_dns_failures_total`: the
  endpoint's name is resolved on its own after each check, through the same resolver, and its
  addresses are logged when they change.
- `webtech_s3_bucket_probe_connections_total`: TCP connections the probe established, a
  reconnection after an idle timeout included. A check that opened one, or retried, is logged at
  INFO with the cause of the failed attempt.
- `test/observability`: a local stack with one exporter per bucket behind a cluster-like DNS and a
  load balancer, Prometheus, Loki, Grafana and cAdvisor, and `make obs/chaos/*` targets that inject
  DNS and network faults. Grafana is provisioned with the S3 buckets dashboard, extended with a
  connectivity diagnosis row.

### Changed

- A failed collection logs the root cause of the error, not only botocore's message.

## [5.0.0]

### Upgrading from 4.x

A major version: three changes can affect a running deployment.

- **`webtech_s3_bucket_size_kbytes` is removed** (deprecated since 3.2.0). Dashboards and alerts
  still reading it must move to `webtech_s3_bucket_size_bytes` (divide by 1000 for the same figure).
- **The default `interval` is 5 minutes** instead of 1. A deployment that relied on the shipped
  `collector.yml` collects five times less often; set `interval: 1` to keep the old cadence. With the
  `list` source, every cycle costs one request per 1000 objects.
- **An `interval` or `retry_interval` that is not a number above 0 stops the start** (exit 1, with
  a message). `interval: 0` used to mean no wait at all between collections.

Access-log lines now quote `rid`, `host`, `via`, `vxid` and `proto`. Valid logfmt either way, but a
parser matching on the unquoted form needs a look.

### Fixed

- One client that connected and sent nothing blocked `/healthz` (and `/metrics`) for as long as it
  stayed: both listeners served one request at a time with no timeout, and the liveness probe then
  restarted the pod. Each connection has its own thread now, and a silent one is dropped after 10
  seconds (#83).
- An outage no longer pins the exporter to the listing. Detection during an outage picked `list`
  and nothing ever detected again: 150 requests a cycle instead of 1 on a 150 000-object
  StorageGRID bucket. A run of failures no longer re-detects while the probe says the bucket is
  unreachable, and on the listing fallback detection is retried every `redetect_interval` seconds
  (3600) (#84).
- `storagegrid` and `minio` verify TLS against `AWS_CA_BUNDLE`, as the S3 client does: behind a
  private CA the usage extension failed silently and detection settled on the listing (#84).
- A client could add fields to the access log through a header such as
  `X-Request-Id: abc status=500 src=admin`; every value a client sends is quoted now (#86).

### Added

- `redetect_interval` in `collector.yml` (#84).

### Changed

- A source skipped at detection is logged at INFO (`storagegrid`) or WARNING (configured sources)
  instead of DEBUG (#84).
- Texts inherited from another project are gone from the metric HELP strings and the command line
  (#86).
- The image is built on Debian 13 (`python:3.12-slim-trixie`) instead of Debian 12 (#85).
- The exporter runs from a virtualenv, and the image carries no pip: copying `site-packages` over
  the base image's left two pip installs mixed together, whose vendored libraries were the image's
  Python vulnerability findings (4 HIGH, now 0). The image is 292 MB instead of 339 (#85).
- CI runs the unit tests and the smoke test, against the prd image, and publishes only when they
  pass. `make test` builds the prd target, not dev (#85).
- The release tooling refuses untracked files and no longer tags after a failed signature;
  `make lint` names its default branch (#85).
- The test stack's S3 storage moved from MinIO to SeaweedFS (#82).
