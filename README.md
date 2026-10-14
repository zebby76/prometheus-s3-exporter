# Prometheus S3 Exporter

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

Watches S3 buckets for Prometheus: whether each one can be reached -- DNS, TCP,
TLS, then S3 -- and, in the background, its size and object count. One exporter
watches one bucket, or a list of them (6.0.0).

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
Detection runs again in two cases, and neither can leave the exporter on the
listing for good:

- **After `max_failures` consecutive failures** — unless the connectivity probe
  says the bucket is unreachable. Then the storage is down, not the source, and
  detecting during the outage would only pick the listing, the one source whose
  probe checks nothing; the source is kept until the storage is back.
- **Every `redetect_interval` seconds (default 3600) while on the listing
  fallback** — that is, on `list` although `auto` or another source was asked
  for. Whatever brought it there (a start during an outage, an extension
  briefly unavailable, no probe to ask), the next attempt climbs back. Each
  attempt costs one request per candidate source; `0` turns it off.

A source skipped at detection says why in the log: at INFO for `storagegrid`,
which is tried everywhere, at WARNING for the sources you configured.

`storagegrid` and `minio` talk to the S3 endpoint's own server over `requests`,
and verify its certificate against `AWS_CA_BUNDLE` when it is set, as the S3
client does. Behind a private CA, set `AWS_CA_BUNDLE` and both paths trust it.

Set `COLLECTOR_SOURCE` to override `source` at runtime without editing the
mounted ConfigMap.

## Multi-bucket configuration

Without `buckets:` the exporter watches one bucket, named by the environment:
`S3_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_DEFAULT_REGION`, `OPENSHIFT_NAMESPACE` and, optionally, `AWS_ENDPOINT_URL`.

With `buckets:`, one exporter watches a list. Each entry names where the bucket's
credentials live; the bucket's name comes with them.

```yaml
configuration:
  # The key names inside the Secrets -- and the variable suffixes with
  # env_prefix. Defaults: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET_NAME.
  - secret_keys:
      access_key: S3_KEY
      secret_key: S3_SECRET
      bucket: S3_BUCKET

  - buckets:
      - secret: app-a-s3          # a Secret, read through the Kubernetes API
        namespace: app-a          # default: the exporter's own namespace
      - secret: legacy-s3
        namespace: app-b
        name: legacy-uploads      # when the Secret does not hold the bucket name
        secret_keys: { access_key: KEY_ID }   # merged over the global names
        endpoint_url: https://grid-2.example:8082   # default: AWS_ENDPOINT_URL
        region: eu-west-1         # default: AWS_DEFAULT_REGION
        interval: 240             # minutes; default: the global interval
        source: list              # default: the global source
      - secret: huge-s3
        namespace: app-d
        collect: false            # probed, never listed
      - env_prefix: APP_C_        # variables set by envFrom with prefix APP_C_
        namespace: app-c          # label only; default: OPENSHIFT_NAMESPACE
```

`AWS_DEFAULT_REGION` and `AWS_ENDPOINT_URL` stay global settings, overridable per
bucket.

### Probes first, sizes in the background

With a list, what matters most is that every bucket is there and answering; its
size can be an hour old. So:

- **Every bucket has its own probe**, on its own thread, every
  `connectivity_interval` seconds (30). Probes never wait for a collection. Their
  first checks are spread over the first interval, instead of a hundred requests
  in the same second.
- **Collections are background work.** `interval` defaults to **60 minutes**
  with a list (5 without one), and at most `max_concurrent_collections` (2) run
  at once, the most overdue first. First collections are spread over the first
  five minutes; later ones keep their own rhythm.
- **The interval stretches by itself.** The next collection of a bucket is due
  no sooner than four times the last one's duration: a listing that takes
  30 minutes runs every two hours, while the small buckets keep their interval.
  `webtech_s3_collect_interval_seconds` shows the interval in force.
- **`collect: false`** watches a bucket with its probe only, for one too large to
  list at all. It publishes no size rather than a zero.

| Setting | Default | Meaning |
| --- | --- | --- |
| `interval` | 60 (list), 5 (single) | minutes between collections, before stretching |
| `max_concurrent_collections` | 2 | collections running at once |
| `credentials_refresh` | 300 | seconds between two reads of every bucket's credentials |

### Credentials

**From a Secret, through the Kubernetes API** -- the main mode. The exporter
reads `GET /api/v1/namespaces/<namespace>/secrets/<secret>` with its pod's
ServiceAccount token, verified against the cluster CA. The Secret stays in the
namespace of the application that owns the bucket: nothing to copy, and a
rotated key is picked up without restarting the exporter. Each Secret needs its
own grant, restricted to that one Secret, in its namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: s3-exporter-read, namespace: app-a }
rules:
  - apiGroups: [""]
    resources: [secrets]
    resourceNames: [app-a-s3]
    verbs: [get]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: { name: s3-exporter-read, namespace: app-a }
roleRef: { apiGroup: rbac.authorization.k8s.io, kind: Role, name: s3-exporter-read }
subjects:
  - { kind: ServiceAccount, name: s3-exporter, namespace: s3-exporter }
```

`deploy/openshift/rbac-app-namespace.yaml` holds the pair, ready to copy.

Never a ClusterRole on Secrets: the exporter must read the Secrets it is given
and nothing else.

**From environment variables** -- the fallback, for a run outside a cluster or
a Secret already mounted into the pod. `envFrom` with a `prefix` turns the
Secret's key `S3_KEY` into the variable `APP_C_S3_KEY`, so the same
`secret_keys` describe a Secret whichever way it arrives:

```yaml
envFrom:
  - prefix: APP_C_
    secretRef: { name: app-c-s3 }
```

Environment variables do not follow a rotation: the pod has to restart.

Secrets are read again every `credentials_refresh` seconds (300), and at once --
at most once a minute per bucket -- when S3 rejects a key
(`InvalidAccessKeyId`, `SignatureDoesNotMatch`). A rotated key rebuilds the
bucket's clients and keeps its figures; a Secret repointed at another bucket
starts that bucket from scratch. A Secret that cannot be read for a moment keeps
the credentials already in hand.

Whatever the mode, no credential is ever logged, printed or put in an error
message: they name the Secret, the key or the variable, never what it holds.

### Validation and `--check-config`

What only a human can fix stops the start (exit 1, so the pod lands in
CrashLoopBackOff): `buckets` that is not a non-empty list, an entry with both or
neither of `secret` and `env_prefix`, an unknown key (a typo in one of a hundred
entries must not go unnoticed), an invalid Secret or namespace name, the same
bucket listed twice, `secret` mode outside a cluster, no region.

What can fix itself does not stop the start: a Secret that does not exist yet, a
RoleBinding still to be applied, a missing key in a Secret. That bucket shows as
`webtech_s3_bucket_configured 0`, with the reason in the log and in `/readyz`,
the others are watched as usual, and the next read of its Secret picks it up
once fixed -- no restart. Alert on it:

```promql
webtech_s3_bucket_configured == 0
```

Without `buckets:`, a bucket that cannot be watched still stops the start, as it
always has.

`--check-config` resolves every bucket's credentials once, reports, and exits
with 0 when all of them resolve, 1 otherwise. Run it to find the missing Secret,
key or RoleBinding before going live:

```console
$ oc exec deploy/s3-exporter -- exporter --check-config
OK    secret app-a/app-a-s3: bucket uploads-a, namespace app-a, region us-east-1, endpoint https://grid.example:8082
FAIL  reading secret app-b/app-b-s3 is forbidden (missing Role/RoleBinding for `get` on this secret?)
FAIL  secret app-d/partial: S3_SECRET is missing or empty
1 of 3 bucket(s) resolved
```

### Reloading the configuration

`collector.yml` is read again every `config_reload_interval` seconds (60; 0
turns the period off) and on `SIGHUP`. When its content changed, the bucket
list is applied as a difference, without a restart:

- **a bucket added** is watched, its first probe and collection spread over the
  first minutes as at startup;
- **a bucket removed** stops: its probe ends and its series disappear;
- **a bucket whose entry changed** -- any key, including a `secret_keys` it
  inherits -- is watched afresh: a new detection and a new collection. Changing
  the `interval` of a very large bucket therefore lists it again;
- **every other bucket** is left alone, figures and series included.

Only `buckets:` and `secret_keys:` reload. Every other setting --
`interval`, `connectivity_interval`, `probe_timeout`,
`max_concurrent_collections`, `sources`, … -- and switching between a single
bucket and a list need a restart: a change to one of them is logged as a
warning, raises `webtech_s3_config_restart_required` to 1, and is not applied.
The environment (`AWS_*`, `COLLECTOR_*`) is read at startup only.

**A content that cannot be honoured keeps the configuration in force**: invalid
YAML, an unknown key, an entry listed twice -- anything that would stop the
exporter at startup. The error is logged once, and
`webtech_s3_config_last_reload_successful` drops to 0 until the file is fixed
or put back as it was:

```promql
webtech_s3_config_last_reload_successful == 0
```

**Mount the ConfigMap as a directory, not with `subPath`.** The kubelet updates
a ConfigMap volume by swapping a link in its directory, about a minute after the
change; a file mounted with `subPath` is copied once when the pod starts and
**never** updated. The image keeps nothing else in `/app/config`, so the whole
directory can take the ConfigMap (its key must be `collector.yml`):

```yaml
# Never updated: changes need a restart
volumeMounts:
  - name: config
    mountPath: /app/config/collector.yml
    subPath: collector.yml

# Updated within about two minutes: the kubelet's delay, then the reload period
volumeMounts:
  - name: config
    mountPath: /app/config
```

The exporter warns at startup when its configuration file is mounted on its own.
Mounting the ConfigMap elsewhere works too, with `COLLECTOR_CONFIG` pointing at
the file.

## Deploying on OpenShift

`deploy/openshift/` holds a complete example, validated against the Kubernetes
schemas:

| File | What |
| --- | --- |
| `serviceaccount.yaml` | the exporter's identity; no right in its own namespace |
| `rbac-app-namespace.yaml` | per application namespace: a Role with `get` on the Secrets it names (`resourceNames`), and a RoleBinding to the exporter's ServiceAccount, across namespaces |
| `configmap.yaml` | `collector.yml` with the bucket list and the Secrets' key names |
| `deployment.yaml` | one replica, the ConfigMap mounted as a directory, probes, resources, a read-only root filesystem |
| `service.yaml`, `servicemonitor.yaml` | scraped by user-workload monitoring |
| `podmonitor.yaml` | the alternative to the Service and ServiceMonitor: one or the other, not both |
| `networkpolicy.yaml` | ingress only from user-workload monitoring (metrics) and the kubelet (probes); egress open |

What matters, in order:

1. **One Role and RoleBinding per application namespace**, listing that
   namespace's Secrets in `resourceNames`. `get` only: `list` or `watch` would
   hand out every Secret there. Never a ClusterRole on Secrets.
2. **The ConfigMap mounted as a directory**, never with `subPath`, or changes to
   the list need a restart (see
   [Reloading the configuration](#reloading-the-configuration)).
3. **`--check-config` before going live**, to find the Secret, key or
   RoleBinding still missing:
   `oc exec deploy/s3-exporter -- exporter --check-config`.
4. **Readiness** answers 503 only when every bucket fails, so one bucket down
   never takes the others' metrics out of service. Keep `failureThreshold` at 3
   or more.
5. **One replica.** Two would probe and list every bucket twice; the exporter
   holds no state worth a second copy, and a restart costs one detection per
   bucket.

Sizing: about 160 MB of memory for a hundred buckets, and almost no CPU at rest.
The CPU goes to the listings, about 86 us per object; a throttled exporter lists
slower, and its probes are not affected. See
[Watching many buckets](#watching-many-buckets) for measurements.

### Migrating from one deployment per bucket

The single-bucket deployments keep working with 6.0.0 unchanged, apart from the
renames listed in the changelog. To move to one exporter:

1. Create the Roles and RoleBindings in every application namespace.
2. Write the ConfigMap with one entry per bucket: `secret` and `namespace` of the
   Secret the old deployment took its variables from, and `secret_keys` with
   their key names.
3. Deploy the new exporter **next to the old ones** and run `--check-config`.
   Every bucket should resolve.
4. Compare on the dashboard: the same buckets appear twice, once per exporter,
   with the same figures. A bucket's `bucket_namespace` is now its Secret's
   namespace; it matches the old label only where the old deployment ran in
   the application's namespace -- check the alerts that filter on it.
5. Delete the old deployments and their PodMonitor; scrape the new exporter
   with `servicemonitor.yaml`.

## Metrics

Every bucket's metrics are labelled `{bucket_namespace, name}`: the namespace of
the application that owns the bucket (with a Secret, the Secret's namespace;
otherwise `OPENSHIFT_NAMESPACE`) and the bucket name. `bucket_namespace` and not
`namespace`, which Prometheus sets itself on every target: the exporter's value
used to land in `exported_namespace`.

| Metric | Meaning |
| --- | --- |
| `webtech_s3_bucket_size_bytes` | bucket size in bytes |
| `webtech_s3_bucket_count_total` | number of objects |
| `webtech_s3_usage_stale_seconds` | age of the figures being served |
| `webtech_s3_collect_duration_seconds` | duration of the last collection |
| `webtech_s3_collect_interval_seconds` | interval in force, stretched for slow buckets |
| `webtech_s3_bucket_reachable` | 1 when the last connectivity probe reached the bucket |
| `webtech_s3_bucket_probe_duration_seconds` | latency of the last probe |
| `webtech_s3_bucket_probe_age_seconds` | age of the last probe |
| `webtech_s3_bucket_probe_retries_total` | probes that had to reconnect, since start |
| `webtech_s3_bucket_probe_errors_total{reason}` | failed probe attempts by reason, retried ones included |
| `webtech_s3_bucket_probe_connections_total` | TCP connections the probe established |
| `webtech_s3_exporter_status` | 1 when the last collection succeeded |
| `webtech_s3_exporter_info{version, source, endpoint}` | one series per watched bucket; `endpoint` joins it to the endpoint metrics |
| `webtech_s3_bucket_configured{bucket_key}` | 1 when the bucket's credentials could be read; `name` is empty until then |
| `webtech_s3_buckets{state}` | buckets `configured` and `unconfigured` |
| `webtech_s3_collections_running` | collections in progress |
| `webtech_s3_collections_overdue` | collections late with no worker free: raise `max_concurrent_collections` or the CPU limit |
| `webtech_s3_exporter_up` | 1 when the exporter is answering |
| `webtech_s3_config_last_reload_successful` | 0 when the last content of `collector.yml` was rejected and the previous one kept |
| `webtech_s3_config_last_reload_success_timestamp_seconds` | when the configuration in force was applied |
| `webtech_s3_config_restart_required` | 1 when a setting that only a restart applies changed in `collector.yml` |

A probe-only bucket (`collect: false`) has no size, count or collection
metrics.

The endpoints the buckets live on are checked once each, whatever the number of
buckets on them (see [Where it breaks](#where-it-breaks-dns--tcp--tls--s3)).
Their metrics are labelled `{endpoint}`, as `scheme://host:port`:

| Metric | Meaning |
| --- | --- |
| `webtech_s3_endpoint_up` | 1 when the last check passed every phase |
| `webtech_s3_endpoint_check_age_seconds` | age of the last check |
| `webtech_s3_endpoint_dns_duration_seconds` | duration of the resolution of the endpoint name |
| `webtech_s3_endpoint_tcp_connect_seconds` | duration of the TCP connect |
| `webtech_s3_endpoint_tls_handshake_seconds` | duration of the TLS handshake (`https` only) |
| `webtech_s3_endpoint_failures_total{phase}` | checks that failed, by the phase that failed: `dns`, `tcp`, `tls` |
| `webtech_s3_endpoint_addresses` | addresses the name resolved to |
| `webtech_s3_endpoint_tls_cert_expiry_timestamp_seconds` | when the certificate presented expires, in Unix time |

A phase that a check did not reach keeps the duration it last measured.

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

### Where it breaks: DNS → TCP → TLS → S3

botocore prints `Could not connect to the endpoint URL` for a name that does not
resolve and for a connection that was refused alike; the error that tells them
apart is buried in the exception chain. The probe digs it out. A failed check
logs **every attempt** with its own duration and root cause, and its reason:

```text
Bucket b became unreachable via HeadBucket after 10.019s and 2 attempts [connect_timeout]:
  #1 5.010s ConnectTimeoutError: Connect timeout on endpoint URL: "…" (caused by TimeoutError: timed out);
  #2 5.010s ConnectTimeoutError: …
```

The reason is one of `dns`, `connect_timeout`, `connect`, `read_timeout`,
`http_5xx`, `http_4xx`, `other`. It is in that line, in the `503` body of
`/readyz`, and counted per failed attempt in
`webtech_s3_bucket_probe_errors_total{reason}` -- retried attempts included, so
a hiccup the retry absorbed still shows.

The probe alone cannot see everything, though: it keeps its connection open
between checks, and only a new connection resolves the name, connects and
shakes hands. So each **endpoint** is also checked on its own, the way the
blackbox exporter does, every `connectivity_interval`:

1. **DNS**: the endpoint's name resolved through the pod's resolver -- search
   list, `ndots` and all, as botocore resolves it. Not bounded by
   `probe_timeout`: how long the resolver takes to give up is what this
   measures.
2. **TCP**: a connection to the resolved addresses, in order, the first that
   answers -- as urllib3 does. `probe_timeout` per address.
3. **TLS**, for an `https` endpoint: a handshake, with the name checked and the
   certificate verified against `AWS_CA_BUNDLE` when it is set -- the S3
   client's own trust. `probe_timeout`.

Then the connection is closed, without a request. A phase that fails stops the
check there.

Once per endpoint, not per bucket: a hundred buckets on one grid are one
resolution, one connection and one handshake every 30 seconds. The endpoint is
known from the configuration, so it is checked from the start, even while no
Secret is readable yet. With `endpoint_url` botocore addresses buckets
path-style, so the name checked is the one the requests use. Without one (AWS)
it goes virtual-host style, to `<bucket>.s3.<region>.amazonaws.com`, and the
check covers `s3.<region>.amazonaws.com`: the same service, not the same DNS
record.

A balancer in front of the storage logs each check as a connection closed
without a request -- on HAProxy, `<BADREQ>` with the `CR--` flags, or nothing
with `option dontlognull`.

The two read together:

| Endpoint check | Bucket probe | Where it breaks |
| --- | --- | --- |
| fails at `dns` | `[dns]`, or still green on its open connection | the DNS |
| `dns` slow, in steps of 5s | slow on a new connection | DNS queries lost, waiting out the resolver's timeout |
| fails at `tcp` at once | `[connect]` | nothing listens (refused, reset) |
| fails at `tcp` after `probe_timeout`, or `No route to host` | `[connect_timeout]` | the address does not answer: down, filtered |
| fails at `tls` | `[other]`, `SSLError` | the certificate: expired, another name, an unknown CA |
| up | `[http_5xx]` | not the network: the storage behind the balancer |
| up | `[http_4xx]` | the credentials or the bucket's policy |

The certificate's end is published in
`webtech_s3_endpoint_tls_cert_expiry_timestamp_seconds`, and logged as a warning
once a day when it is less than 14 days away:

```promql
webtech_s3_endpoint_tls_cert_expiry_timestamp_seconds - time() < 14 * 86400
```

What each failure looked like on the local stack (`make obs/chaos/…`):

| Failure | Endpoint check | Bucket probe |
| --- | --- | --- |
| DNS answers SERVFAIL | `failed at dns` in 8ms | **stays green** on its open connection |
| One DNS query in three lost (conntrack race, overloaded DNS) | up, `dns` at 10.01s: two queries waiting out the resolver's 5s | green on its open connection; slow in multiples of 5s when it reconnects |
| Nothing listens (RST) | `failed at tcp` at once, `ConnectionRefusedError` | `[connect]` in 13ms |
| Address down | `failed at tcp` after 5.0s, `TimeoutError`; later `No route to host` after 3s, once ARP gives up | `[connect_timeout]`, 5s per attempt |
| Balancer up, storage down | up | `[http_5xx]` in 4ms |
| Certificate for another name | `failed at tls` in 3ms, `Hostname mismatch` | `[other]`, `SSLError` |
| Certificate expired | `failed at tls` in 2ms, `certificate has expired` | `[other]`, `SSLError` |
| Certificate ending in 10 days | up; `WARNING … expires in 10.0 days` | green |

The first line is the case the endpoint check exists for: a DNS that fails
while every probe stays green, until the connections are dropped and nothing
resolves any more.

**New connections are counted** in `webtech_s3_bucket_probe_connections_total`,
and a probe that opened one or retried is logged at INFO. Only a new
connection resolves the name, so a peak in `probe_duration_seconds` without a
new connection is not the DNS.

On OpenShift, `ndots:5` makes every name with fewer than five dots go through
the whole search list first: `s3.example.com` costs four pairs of A and AAAA
queries, three of them answered NXDOMAIN, each a chance to lose a packet.
Writing the endpoint as an absolute name, with a trailing dot
(`https://s3.example.com.:443`), or lowering `ndots` in the pod's `dnsConfig`,
removes the detour. Measure it first: `webtech_s3_endpoint_dns_duration_seconds`
before and after.

The endpoint checks go straight to the endpoint: behind an `HTTPS_PROXY` they
would test a path the S3 requests do not take.

The two endpoints answer different questions:

- **`/healthz`** is liveness: is the process responding. Independent of S3 by
  design, since restarting the pod does not fix an unreachable bucket and only
  throws away the metrics right when they are wanted. This drives the container
  `HEALTHCHECK`.
- **`/readyz`** is readiness, from the probes. It answers `503` only when
  **every** bucket is unreachable or unconfigured, and `200` otherwise: the pod
  serves the metrics of all its buckets, so one unreachable bucket out of a
  hundred must not take the other ninety-nine out of service. A bucket not probed
  yet does not count against it. With a single bucket, that is `200` while it is
  reachable and `503` once it is not. The body details every bucket:

  ```json
  {"status": true, "buckets": {
    "secret:app-a/app-a-s3": {"name": "uploads-a", "state": "reachable"},
    "secret:app-b/app-b-s3": {"name": "archive", "state": "unreachable",
                              "reason": "dns", "error": "#1 …", "age_seconds": 12.0},
    "secret:app-c/app-c-s3": {"name": null, "state": "unconfigured",
                              "error": "reading secret app-c/app-c-s3 is forbidden …"}},
   "endpoints": {
    "https://grid.example:8082": {"state": "down", "phase": "tls",
                                  "error": "SSLCertVerificationError: …", "age_seconds": 3.1}}}
  ```

  The endpoints are there to read, and never change the status: an endpoint
  that is down already shows as unreachable buckets, and a failed handshake
  while the probes still pass on their open connections must not take the pod
  out of service.

Set `connectivity_interval: 0` to switch the probe off entirely: no thread, no
requests, no probe or endpoint metrics, and `/readyz` is not routed.

Both listeners, `:9773` and `:9774`, serve each connection on a thread of its
own and drop one that sends nothing for 10 seconds. A port scan or a TCP-only
check holding a connection open therefore never delays `/healthz` or a scrape.

## Access logs

Requests are logged as logfmt on the same key set a reverse proxy tier emits, so
one parser reads both tiers and a request can be followed across them on `vxid`
and `via`:

```text
ts=2026-08-31T18:57:03.720+0000 rid="4285d115ed6745bd" class=health src=exporter \
status=200 dur_us=468 method=GET uri="/readyz" qs="-" target="/readyz" bytes=37 \
host="exporter:9774" via="apache" vxid="987654" xff="10.0.0.1" proto="https" \
ua="kube-probe/1.29"
```

Every value a client sends is quoted, so a header cannot add keys of its own to
the line.

| Key | Value |
| --- | --- |
| `rid` | the inbound `X-Request-Id`, or one minted per request |
| `class` | `metrics`, `health` (`/healthz`, `/readyz`), or `other` |
| `dur_us` | time in the application and the response write, microseconds |
| `uri` / `target` / `qs` | request target, path alone, query alone |
| `bytes` | response body size; `0` for an empty body, never `-` |
| `via` `vxid` `xff` `proto` | the proxy header family, `-` when absent |

Every proxy names its correlation headers differently, and not as variations on
one prefix, so each is named in full under `proxy_headers`:

```yaml
  - proxy_headers:
      via: Via
      vxid: X-Transaction-Id
      xff: X-Forwarded-For
      proto: X-Forwarded-Proto
```

Those are the defaults -- three of them the real standard headers. Name only what
your proxy stamps differently; the rest keep their defaults, so naming one header
never blanks the others. Each has its own override:
`COLLECTOR_PROXY_HEADER_VIA`, `_VXID`, `_XFF`, `_PROTO`. A header that is not
sent logs as `-`.

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

      # A collector.yml change that was rejected: the previous one is in force.
      - alert: S3ConfigRejected
        expr: webtech_s3_config_last_reload_successful == 0
        for: 5m
        annotations:
          summary: "collector.yml was rejected, the previous configuration is in force"

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

## Dashboard

`test/observability/grafana/dashboards/s3.dashboard.json` is a Grafana
dashboard (built on 10.2) that reads the same whether one exporter watches a
hundred buckets or a hundred exporters watch one each. Buckets are keyed by
`(bucket_namespace, name)`, so the same bucket name in two namespaces stays two
rows. Import it into Grafana; it keeps the uid `s3-exporter-table`, so an import
replaces an earlier version.

| Section | Shows |
| --- | --- |
| header | buckets watched and not watched, unreachable, endpoints down, collections overdue, configuration rejected or waiting for a restart, the certificate closest to its end |
| Buckets | one row per bucket, unreachable first: probe latency, errors and main error of the last hour, staleness, size, collection, interval in force, endpoint; and the buckets whose credentials cannot be read |
| Problems | only what needs looking at: reachability of the buckets that failed in the range, the ten slowest probes, errors by reason, the largest buckets, the slowest collections |
| Endpoints | per endpoint: up, DNS, TCP and TLS times, days left on the certificate, failures and where; the phases over time |
| Exporters | per pod: buckets, collections running and overdue, configuration state, CPU, memory, throttling |
| Logs | the exporters' events from Loki |

Variables: the Prometheus and Loki datasources, `namespace`, `bucket`,
`endpoint`, and the Loki *Log selector* (e.g.
`{namespace="s3-exporter", container="s3-exporter"}`).

The CPU, memory and throttling columns join cAdvisor's series on
`(namespace, pod)`: they need a datasource that sees both the container metrics
and the exporters -- on OpenShift, Thanos Querier rather than the user-workload
Prometheus alone.

## Sizing the interval

With the `list` source the cost of a cycle tracks the **number of objects**, not
the number of bytes: a 74 GB bucket holding 1360 objects is cheaper to measure
than a 2 GB one holding 150 000. Budget roughly one request per 1000 objects and
86 us of parsing per object, plus one round-trip per request.

| Objects | Requests | Collection | Share of the default 5 min interval |
| --- | --- | --- | --- |
| 1 000 | 1 | 0.1 s | 0.03 % |
| 10 000 | 10 | 0.9 s | 0.3 % |
| 150 000 | 150 | ~16 s | 5 % |
| 700 000 | 700 | ~60 s | 20 % |

`interval` is in minutes and must be above 0: the exporter refuses to start
otherwise (so do `retry_interval`, `max_concurrent_collections` and
`credentials_refresh`). A collection may use at most a quarter of its bucket's
time: past that, the next one is pushed back to four times its duration, and
`webtech_s3_collect_interval_seconds` shows it.

### Watching many buckets

Measured on the local stack, one exporter watching **103 buckets** (all through
`list`, 0.5 CPU):

| | |
| --- | --- |
| memory | 145 MB at start, 155 MB once every bucket was collected (about 0.65 MB per bucket over a 70 MB base: two S3 clients each) |
| threads | 109: one probe per bucket, plus the scheduler, workers and listeners |
| CPU between collections | under 1 % |
| first collections | all 103 within 6 minutes of the start |
| probe latency | p50 2.7 ms, max 3.8 ms |
| `/metrics` | 215 KB, 2347 lines, served in 158 ms |

What grows with the buckets is the listing. It is CPU-bound -- about 86 µs per
object -- and every worker that lists holds up to one core. Size
`max_concurrent_collections` and the CPU limit from the total number of objects
listed per interval, and watch `webtech_s3_collections_overdue`: above zero,
collections fall behind. A bucket too large to list at all is better probed
with `collect: false`.

The storage sees about one request per 1000 objects per collection: 50 million
objects listed every hour is about 14 requests a second. Worth telling the team
that runs it.

## Build

```bash
make build
```

The image is `python:3.12-slim` on Debian 13 (`REL` in `docker-bake.hcl`). The exporter and its
dependencies live in a virtualenv, `/opt/venv`; the image carries no pip.

## Test

```bash
make unit    # unit tests, no containers (Python 3.12, `pip install -e '.[dev]'`)
make test    # full stack with the prd image, asserts the exposed metrics, then tears down
```

CI runs both before it publishes anything. `make test` builds the **prd** image, the one that is
published; `make up` keeps the dev image, which adds `curl` and `awscli` for local work.

## Local development

```bash
make up      # exporter + SeaweedFS (S3), bucket preloaded from test/assets
make logs
make down
```

### Observability stack

`test/observability` runs the exporter the way production does, both ways: one
instance per bucket, and one for a list of buckets whose credentials come from
Secrets through a Kubernetes API. Both sit behind the same kind of path: a
cluster DNS with OpenShift's search list and `ndots:5`, and a load balancer with
an idle timeout. Prometheus, Loki and Grafana sit behind Traefik, taken from
base-php's `test/demo-infra`.

```text
exporter-{small,medium,large} --DNS--> dnsfault --> CoreDNS
                              --S3---> lb (HAProxy, :8333) --> SeaweedFS
exporter-tls                  --S3---> lb (HAProxy, :8443 TLS) --> SeaweedFS
exporter-multi                --S3---> both
                              --API--> kube-api (Secrets, Roles, the ServiceAccount)
```

`exporter-multi` lists 18 buckets over three application namespaces
(`app-a`, `app-b`, `app-c`): Secrets read through `kube-api` with the key names
production uses, one bucket over TLS, the 50 000-object one, one probed only,
two from `envFrom` variables, and one whose Secret does not exist yet. Its
`collector.yml` is a ConfigMap volume and reloads. `kube-api` is a small fake of
the API server: Bearer token checked (401), a Role per Secret (403 without
one), 404 for a missing Secret. Its state lives in `kube/state` (never
committed), rebuilt by `make obs/kube/reset`.

`exporter-tls` reaches the same storage over TLS, trusting a demo CA through
`AWS_CA_BUNDLE`, so the endpoint check has a handshake to time and to break.
`make obs/up` makes the CA and the certificates once, into `tls/out` (never
committed); `make obs/down` drops them.

```bash
make obs/up       # build, start, fill the buckets with random content (first run: a few minutes)
make obs/urls     # Grafana, Prometheus, Loki, the exporters
make obs/down     # stop and drop the volumes
```

Faults, cleared by `make obs/chaos/off`:

| Target | Effect |
| --- | --- |
| `obs/chaos/dns-drop [RATIO=0.33]` | loses that share of DNS queries |
| `obs/chaos/dns-delay [DELAY_MS=300]` | slows every DNS query down |
| `obs/chaos/dns-servfail` | answers SERVFAIL |
| `obs/chaos/lb-idle [IDLE=25s]` | sets the balancer's idle timeout; under 30s, every probe reconnects |
| `obs/chaos/lb-refuse` | nothing listens on the S3 port any more |
| `obs/chaos/lb-down` | stops the balancer: its address stops answering |
| `obs/chaos/s3-down` | stops the storage: the balancer answers 503 |
| `obs/chaos/tls-wrong-name` | the TLS listener presents a certificate for another name |
| `obs/chaos/tls-expired` | the TLS listener presents an expired certificate |
| `obs/chaos/tls-expiring` | the TLS certificate ends in 10 days: the exporter warns |

The fake cluster, cleared by `make obs/kube/reset` (`NAME` is
`namespace/secret`):

| Target | Effect |
| --- | --- |
| `obs/kube/forbid [NAME=…]` | drops the Role's grant: 403, as a missing RoleBinding |
| `obs/kube/allow [NAME=…]` | grants it again |
| `obs/kube/delete [NAME=…]` | deletes the Secret: 404 |
| `obs/kube/wrong-key [NAME=…]` | a key S3 rejects: the Secret is read again at once |
| `obs/kube/repoint [NAME=… BUCKET=…]` | the Secret names another bucket: watched from scratch |
| `obs/kube/config-add` | adds a bucket to the ConfigMap |
| `obs/kube/config-remove` | removes one |
| `obs/kube/config-broken` | publishes invalid YAML: rejected, the previous list kept |

Grafana comes provisioned with the [dashboard](#dashboard), fed by every
exporter of the stack at once. cAdvisor is relabelled so its series join on
`(namespace, pod)` like the kubelet's, the container ID standing in for the pod
name.

`dnsfault` is a small UDP relay in front of CoreDNS. CoreDNS cannot disturb a
name it resolves itself: its `erratic` plugin runs after `hosts` and `forward`.
The exporters mount their own `/etc/resolv.conf` so their queries reach it
directly, as a pod's reach the cluster DNS, rather than through Docker's
embedded resolver, which answers SERVFAIL on its own after a few seconds.

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
