#!/usr/bin/env bats
load "helpers/tests"
load "helpers/podman"

load "lib/batslib"
load "lib/output"

export BATS_OPENSHIFT_NAMESPACE="default-namespace"

export BATS_S3_BUCKET_NAME="s3bucket-example"
export BATS_S3_ENDPOINT_URL="http://minio:9000"
export BATS_S3_ACCESS_KEY_ID="mock"
export BATS_S3_SECRET_ACCESS_KEY="SecretAccessKey"
export BATS_S3_DEFAULT_REGION="us-east-1"

export BATS_DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME:-docker.io/smalswebtech/prometheus-s3-exporter:rc}"

@test "[$TEST_FILE] Starting Storage Services (S3)" {
  command podman-compose -f ${BATS_TEST_DIRNAME%/}/docker-compose.yml up -d minio
  podman_wait_for_healthy minio 120
}

@test "[$TEST_FILE] Create Storage S3 Bucket." {

  export BATS_S3_ENDPOINT_URL=http://$(podman_ip minio):9000

  run podman run --rm -t --network docker_default \
                      -e AWS_ACCESS_KEY_ID="${BATS_S3_ACCESS_KEY_ID}" \
                      -e AWS_SECRET_ACCESS_KEY="${BATS_S3_SECRET_ACCESS_KEY}" \
                      -e AWS_DEFAULT_REGION="${BATS_S3_DEFAULT_REGION}" \
      docker.io/amazon/aws-cli s3 mb s3://${BATS_S3_BUCKET_NAME%/} --endpoint-url ${BATS_S3_ENDPOINT_URL}

  assert_output -l -r "make_bucket: ${BATS_S3_BUCKET_NAME%%/*}"

}

@test "[$TEST_FILE] Loading Test Data files Storage services (S3)" {

  export BATS_S3_ENDPOINT_URL=http://$(podman_ip minio):9000

  run podman run --rm -t --network docker_default \
                      -e AWS_ACCESS_KEY_ID="${BATS_S3_ACCESS_KEY_ID}" \
                      -e AWS_SECRET_ACCESS_KEY="${BATS_S3_SECRET_ACCESS_KEY}" \
                      -e AWS_DEFAULT_REGION="${BATS_S3_DEFAULT_REGION}" \
      docker.io/amazon/aws-cli s3 put-bucket-acl --bucket s3://${BATS_S3_BUCKET_NAME%/} --acl public-read --endpoint-url ${BATS_S3_ENDPOINT_URL}

  run mkdir -p /tmp/assets
  run tar xvzf ${BATS_TEST_DIRNAME%/}/assets/example.tar.gz --strip-components=1 -C /tmp/assets

  run podman run --rm -t --network docker_default \
                      -e AWS_ACCESS_KEY_ID="${BATS_S3_ACCESS_KEY_ID}" \
                      -e AWS_SECRET_ACCESS_KEY="${BATS_S3_SECRET_ACCESS_KEY}" \
                      -e AWS_DEFAULT_REGION="${BATS_S3_DEFAULT_REGION}" \
                      -v /tmp/assets:/tmp/assets:ro \
      docker.io/amazon/aws-cli s3 sync /tmp/assets s3://${BATS_S3_BUCKET_NAME%/}/assets --endpoint-url ${BATS_S3_ENDPOINT_URL}

  assert_output -l -r ".*upload: .* to s3://${BATS_S3_BUCKET_NAME%/}.*"

}

@test "[$TEST_FILE] Starting 'WebTech S3 Exporter (Prometheus Exporter)' Service" {
  export BATS_S3_ENDPOINT_URL=http://$(podman_ip minio):9000
  command podman-compose -f ${BATS_TEST_DIRNAME%/}/docker-compose.yml up -d exporter
}

@test "[$TEST_FILE] Test 'WebTech S3 Exporter (Prometheus Exporter)' /health" {
  retry 12 5 curl_podman_container exporter :9774/health -s -w %{http_code} -o /dev/null
  assert_output -l 0 $'200'
  retry 12 5 curl_podman_container exporter :9774/health -s
  assert_output -l "{\"status\": true}"
}

@test "[$TEST_FILE] Test 'WebTech S3 Exporter (Prometheus Exporter)' /metrics" {
  retry 12 5 curl_podman_container exporter :9773/metrics -s -w %{http_code} -o /dev/null
  assert_output -l 0 $'200'
  retry 12 5 curl_podman_container exporter :9773/metrics -s
  assert_output -l "webtech_s3_exporter_up 1.0"
  assert_output -l "webtech_s3_exporter_status{name=\"${BATS_S3_BUCKET_NAME}\",namespace=\"${BATS_OPENSHIFT_NAMESPACE}\"} 1.0"
  assert_output -l "webtech_s3_bucket_count_total{name=\"${BATS_S3_BUCKET_NAME}\",namespace=\"${BATS_OPENSHIFT_NAMESPACE}\"} 5.0"
}

@test "[$TEST_FILE] Test 'WebTech S3 Exporter (Prometheus Exporter)' aws cli" {

  run podman run --rm -t \
                      -e OPENSHIFT_NODE_HOSTNAME="node1.openshift.cloud.vm" \
                      -e OPENSHIFT_NAMESPACE="${BATS_OPENSHIFT_NAMESPACE}" \
                      -e AWS_ACCESS_KEY_ID="${BATS_S3_ACCESS_KEY_ID}" \
                      -e AWS_SECRET_ACCESS_KEY="${BATS_S3_SECRET_ACCESS_KEY}" \
                      -e AWS_DEFAULT_REGION="${BATS_S3_DEFAULT_REGION}" \
                      -e AWS_ENDPOINT_URL="${BATS_S3_ENDPOINT_URL}" \
                      -e S3_BUCKET_NAME="${BATS_S3_BUCKET_NAME}" \
                      --entrypoint aws \
      ${BATS_DOCKER_IMAGE_NAME} --version

  assert_output -l -r "aws-cli/.* Python/.* .* botocore/.*"

}

@test "[$TEST_FILE] Stop all and delete test containers" {
  command podman-compose -f ${BATS_TEST_DIRNAME%/}/docker-compose.yml down -v
}
