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
HEALTH_URL="${HEALTH_URL:-http://localhost:9774/health}"
READY_URL="${READY_URL:-http://localhost:9774/ready}"
# compose.override.yaml sets COLLECTOR_CONNECTIVITY_INTERVAL=5.
PROBE_INTERVAL="${PROBE_INTERVAL:-5}"

EXPECTED_COUNT=5
EXPECTED_BYTES=4446391
# The deprecated gauge divides by 1000 and truncates.
EXPECTED_KBYTES=4446

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
# Kept until the next major so existing dashboards survive the rename.
assert_equals webtech_s3_bucket_size_kbytes "$(value_of webtech_s3_bucket_size_kbytes)" "${EXPECTED_KBYTES}"
assert_equals webtech_s3_exporter_up "$(value_of webtech_s3_exporter_up)" 1
assert_equals webtech_s3_exporter_status "$(value_of webtech_s3_exporter_status)" 1
assert_present webtech_s3_usage_stale_seconds
assert_present webtech_s3_collect_duration_seconds
assert_equals webtech_s3_collect_interval_seconds "$(value_of webtech_s3_collect_interval_seconds)" 60
assert_equals webtech_s3_bucket_reachable "$(value_of webtech_s3_bucket_reachable)" 1
assert_present webtech_s3_bucket_probe_duration_seconds
assert_present webtech_s3_bucket_probe_age_seconds

# The detected source is published as a label so a fallback stays visible.
if ! grep -q 'webtech_s3_exporter_info{.*source="' <<<"${scrape}"; then
	echo "FAIL: webtech_s3_exporter_info carries no source label" >&2
	exit 1
fi
printf 'ok: %-40s %s\n' "usage source" \
	"$(grep -o 'source="[^"]*"' <<<"${scrape}" | head -1)"

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

# The interesting half: connectivity has to be reported independently of the
# collection, which at a production interval may be an hour old.
echo "Cutting MinIO off to exercise the probe..."
docker compose stop minio >/dev/null 2>&1

if ! wait_for_status "${READY_URL}" 503 $((PROBE_INTERVAL * 6 + 20)); then
	echo "FAIL: ${READY_URL} never reported 503 with the bucket down" >&2
	docker compose start minio >/dev/null 2>&1
	exit 1
fi
printf 'ok: %-40s %s\n' "${READY_URL} (bucket down)" "503"

fetch
assert_equals "webtech_s3_bucket_reachable (down)" \
	"$(value_of webtech_s3_bucket_reachable)" 0
# Figures already collected must survive an outage rather than drop to zero.
assert_equals "webtech_s3_bucket_size_bytes (down)" \
	"$(value_of webtech_s3_bucket_size_bytes)" "${EXPECTED_BYTES}"
# Liveness must stay green: restarting the pod would not fix S3.
assert_equals "${HEALTH_URL} (bucket down)" "$(status_of "${HEALTH_URL}")" 200

echo "Bringing MinIO back..."
docker compose start minio >/dev/null 2>&1
if ! wait_for_status "${READY_URL}" 200 $((PROBE_INTERVAL * 6 + 40)); then
	echo "FAIL: ${READY_URL} never recovered after MinIO came back" >&2
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
