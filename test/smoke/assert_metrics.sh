#!/usr/bin/env bash
#
# Smoke assertions against a running stack.
#
# The figures come from test/assets, which compose uploads into the bucket
# before the exporter is allowed to start: 5 objects totalling 4446391 bytes.

set -o nounset
set -o errexit
set -o pipefail

METRICS_URL="${METRICS_URL:-http://localhost:9773/metrics}"
HEALTH_URL="${HEALTH_URL:-http://localhost:9774/healthz}"
READY_URL="${READY_URL:-http://localhost:9774/readyz}"
# compose.override.yaml sets COLLECTOR_CONNECTIVITY_INTERVAL=5.
PROBE_INTERVAL="${PROBE_INTERVAL:-5}"

EXPECTED_COUNT=5
EXPECTED_BYTES=4446391

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-90}"

scrape=""

fetch() {
	scrape="$(curl -fsS --max-time 10 "${METRICS_URL}")"
}

# Value of a gauge, labelled or not; empty when the metric is absent.
value_of() {
	awk -v name="$1" '
		$0 ~ "^" name "([{ ])" { print $NF; found = 1; exit }
		END { if (!found) print "" }
	' <<<"${scrape}"
}

assert_equals() {
	local label="$1" actual="$2" expected="$3"
	if [[ -z ${actual} ]]; then
		echo "FAIL: ${label} is absent from ${METRICS_URL}" >&2
		return 1
	fi
	# Values are exposed as floats ("5.0"), so compare numerically.
	if ! awk -v a="${actual}" -v b="${expected}" 'BEGIN { exit !(a == b) }'; then
		echo "FAIL: ${label} = ${actual}, expected ${expected}" >&2
		return 1
	fi
	printf 'ok: %-40s = %s\n' "${label}" "${actual}"
}

assert_present() {
	local label="$1"
	if [[ -z $(value_of "${label}") ]]; then
		echo "FAIL: ${label} is absent from ${METRICS_URL}" >&2
		return 1
	fi
	printf 'ok: %-40s present (%s)\n' "${label}" "$(value_of "${label}")"
}

# The health endpoint comes up before the first collection finishes, so wait for
# the collector to publish real figures rather than asserting on an empty gauge.
echo "Waiting for the first collection (up to ${TIMEOUT_SECONDS}s)..."
deadline=$((SECONDS + TIMEOUT_SECONDS))
until fetch && [[ $(value_of webtech_s3_bucket_count_total) != "" ]] &&
	awk -v v="$(value_of webtech_s3_bucket_count_total)" 'BEGIN { exit !(v > 0) }'; do
	if ((SECONDS >= deadline)); then
		echo "FAIL: no usage collected within ${TIMEOUT_SECONDS}s" >&2
		echo "--- last scrape ---" >&2
		echo "${scrape}" >&2
		exit 1
	fi
	sleep 2
done

fetch

assert_equals webtech_s3_bucket_count_total "$(value_of webtech_s3_bucket_count_total)" "${EXPECTED_COUNT}"
assert_equals webtech_s3_bucket_size_bytes "$(value_of webtech_s3_bucket_size_bytes)" "${EXPECTED_BYTES}"
# Deprecated in 3.2.0, removed in 5.0.0.
if [[ -n $(value_of webtech_s3_bucket_size_kbytes) ]]; then
	echo "FAIL: webtech_s3_bucket_size_kbytes is still exposed" >&2
	exit 1
fi
assert_equals webtech_s3_exporter_up "$(value_of webtech_s3_exporter_up)" 1
assert_equals webtech_s3_exporter_status "$(value_of webtech_s3_exporter_status)" 1
assert_present webtech_s3_usage_stale_seconds
assert_present webtech_s3_collect_duration_seconds
# The shipped collector.yml collects every 5 minutes.
assert_equals webtech_s3_collect_interval_seconds "$(value_of webtech_s3_collect_interval_seconds)" 300
assert_equals webtech_s3_bucket_reachable "$(value_of webtech_s3_bucket_reachable)" 1
assert_present webtech_s3_bucket_probe_duration_seconds
assert_present webtech_s3_bucket_probe_age_seconds
assert_present webtech_s3_bucket_probe_retries_total
assert_present webtech_s3_bucket_probe_errors_total
assert_present webtech_s3_bucket_probe_connections_total
# The DNS is checked per endpoint now, with TCP: once for every bucket on it.
if [[ -n $(value_of webtech_s3_bucket_dns_duration_seconds) ]]; then
	echo "FAIL: webtech_s3_bucket_dns_duration_seconds is still exposed" >&2
	exit 1
fi

# Every reason is published from the start, at zero, so increase() sees the
# first error of each.
for reason in dns connect_timeout connect read_timeout http_5xx http_4xx other; do
	if ! grep -q "^webtech_s3_bucket_probe_errors_total{.*reason=\"${reason}\"" <<<"${scrape}"; then
		echo "FAIL: webtech_s3_bucket_probe_errors_total has no reason=\"${reason}\" series" >&2
		exit 1
	fi
done
echo "ok: every probe error reason is published"

# Bucket metrics carry bucket_namespace, never namespace: Prometheus sets that
# one on every target and would rename the exporter's to exported_namespace.
if ! grep -q '^webtech_s3_bucket_size_bytes{bucket_namespace="default-namespace",name="my-bucket"}' <<<"${scrape}"; then
	echo "FAIL: webtech_s3_bucket_size_bytes is not labelled {bucket_namespace, name}" >&2
	exit 1
fi
if grep -q '^webtech_s3_[a-z_]*{namespace=' <<<"${scrape}"; then
	echo "FAIL: a metric still carries a namespace label" >&2
	exit 1
fi
echo "ok: bucket metrics are labelled bucket_namespace"
assert_equals webtech_s3_bucket_configured "$(value_of webtech_s3_bucket_configured)" 1
if ! grep -q '^webtech_s3_buckets{state="configured"} 1' <<<"${scrape}"; then
	echo "FAIL: webtech_s3_buckets{state=\"configured\"} is not 1" >&2
	exit 1
fi
echo "ok: one bucket configured"

# The detected source is published as a label so a fallback stays visible.
if ! grep -q 'webtech_s3_exporter_info{.*source="' <<<"${scrape}"; then
	echo "FAIL: webtech_s3_exporter_info carries no source label" >&2
	exit 1
fi
printf 'ok: %-40s %s\n' "usage source" \
	"$(grep -o 'source="[^"]*"' <<<"${scrape}" | head -1)"

# One endpoint check per endpoint, here plain http: DNS and TCP, no TLS.
ENDPOINT='endpoint="http://seaweedfs:8333"'
deadline=$((SECONDS + PROBE_INTERVAL * 2 + 10))
until [[ -n $(value_of webtech_s3_endpoint_up) ]]; do
	if ((SECONDS >= deadline)); then
		echo "FAIL: webtech_s3_endpoint_up never appeared" >&2
		exit 1
	fi
	sleep 2
	fetch
done
for metric in endpoint_up endpoint_dns_duration_seconds endpoint_tcp_connect_seconds; do
	if ! grep -q "^webtech_s3_${metric}{${ENDPOINT}}" <<<"${scrape}"; then
		echo "FAIL: webtech_s3_${metric}{${ENDPOINT}} is absent" >&2
		exit 1
	fi
done
assert_equals webtech_s3_endpoint_up "$(value_of webtech_s3_endpoint_up)" 1
if [[ -n $(value_of webtech_s3_endpoint_tls_handshake_seconds) ]]; then
	echo "FAIL: a plain http endpoint reports a TLS handshake" >&2
	exit 1
fi
if ! grep -q "^webtech_s3_exporter_info{.*${ENDPOINT}" <<<"${scrape}"; then
	echo "FAIL: webtech_s3_exporter_info does not name the bucket's endpoint" >&2
	exit 1
fi
echo "ok: the endpoint is checked, DNS and TCP"

# HTTP status of a URL, without failing the script on a 5xx.
status_of() {
	curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$1"
}

# Poll until the endpoint reports the wanted status, or give up.
wait_for_status() {
	local url="$1" wanted="$2" limit="$3"
	local deadline=$((SECONDS + limit))
	until [[ $(status_of "${url}") == "${wanted}" ]]; do
		if ((SECONDS >= deadline)); then
			return 1
		fi
		sleep 1
	done
}

assert_equals "${READY_URL} (bucket up)" "$(status_of "${READY_URL}")" 200

# One client that connects and sends nothing must not hold the others up. The
# listeners used to serve one request at a time, with no timeout: a port scan
# or a TCP-only check blocked /healthz until the liveness probe restarted the pod.
health_hostport="${HEALTH_URL#*://}"
health_hostport="${health_hostport%%/*}"
exec 3<>"/dev/tcp/${health_hostport%:*}/${health_hostport##*:}"
assert_equals "${HEALTH_URL} (idle client open)" "$(status_of "${HEALTH_URL}")" 200
exec 3<&-

# The interesting half: connectivity has to be reported independently of the
# collection, which at a production interval may be an hour old.
echo "Cutting the S3 storage off to exercise the probe..."
docker compose stop seaweedfs >/dev/null 2>&1

if ! wait_for_status "${READY_URL}" 503 $((PROBE_INTERVAL * 6 + 20)); then
	echo "FAIL: ${READY_URL} never reported 503 with the bucket down" >&2
	docker compose start seaweedfs >/dev/null 2>&1
	exit 1
fi
printf 'ok: %-40s %s\n' "${READY_URL} (bucket down)" "503"

fetch
assert_equals "webtech_s3_bucket_reachable (down)" \
	"$(value_of webtech_s3_bucket_reachable)" 0
# Figures already collected must survive an outage rather than drop to zero.
assert_equals "webtech_s3_bucket_size_bytes (down)" \
	"$(value_of webtech_s3_bucket_size_bytes)" "${EXPECTED_BYTES}"
# The stopped container's name no longer resolves: the endpoint says where.
deadline=$((SECONDS + PROBE_INTERVAL * 2 + 10))
until [[ $(value_of webtech_s3_endpoint_up) == 0* ]]; do
	if ((SECONDS >= deadline)); then
		echo "FAIL: webtech_s3_endpoint_up never dropped to 0 with the storage down" >&2
		docker compose start seaweedfs >/dev/null 2>&1
		exit 1
	fi
	sleep 2
	fetch
done
ready_body="$(curl -s --max-time 10 "${READY_URL}")"
if ! grep -q '"endpoints": {"http://seaweedfs:8333": {"state": "down"' <<<"${ready_body}"; then
	echo "FAIL: ${READY_URL} does not show the endpoint down: ${ready_body}" >&2
	docker compose start seaweedfs >/dev/null 2>&1
	exit 1
fi
printf 'ok: %-40s %s\n' "endpoint (down)" \
	"$(grep -o '"phase": "[a-z]*"' <<<"${ready_body}")"
# Liveness must stay green: restarting the pod would not fix S3.
assert_equals "${HEALTH_URL} (bucket down)" "$(status_of "${HEALTH_URL}")" 200

echo "Bringing the S3 storage back..."
docker compose start seaweedfs >/dev/null 2>&1
if ! wait_for_status "${READY_URL}" 200 $((PROBE_INTERVAL * 6 + 40)); then
	echo "FAIL: ${READY_URL} never recovered after the S3 storage came back" >&2
	exit 1
fi
printf 'ok: %-40s %s\n' "${READY_URL} (recovered)" "200"

fetch
assert_equals "webtech_s3_bucket_reachable (recovered)" \
	"$(value_of webtech_s3_bucket_reachable)" 1

health="$(curl -fsS --max-time 10 "${HEALTH_URL}")"
if [[ ${health} != '{"status": true}' ]]; then
	echo "FAIL: health endpoint returned ${health}" >&2
	exit 1
fi
printf 'ok: %-40s %s\n' "${HEALTH_URL}" "${health}"

echo "All smoke assertions passed."
