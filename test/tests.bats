#!/usr/bin/env bats
load "helpers/tests"
load "helpers/docker"
load "helpers/dataloaders"

load "lib/batslib"
load "lib/output"

export BATS_CLAIR_LOCAL_SCANNER_CONFIG_VOLUME_NAME=${BATS_CLAIR_LOCAL_SCANNER_CONFIG_VOLUME_NAME:-clair_local_scanner}

export BATS_S3_BUCKET_NAME="s3bucket-example"
export BATS_S3_ENDPOINT_URL="http://localhost:4572"
export BATS_S3_ACCESS_KEY_ID="mock"
export BATS_S3_SECRET_ACCESS_KEY="mock"
export BATS_S3_DEFAULT_REGION="us-east-1"

export AWS_ACCESS_KEY_ID="${BATS_S3_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${BATS_S3_SECRET_ACCESS_KEY}"
export AWS_DEFAULT_REGION="${BATS_S3_DEFAULT_REGION}"

export BATS_DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME:-docker.io/zebby76/prometheus-s3-exporter}:rc"

@test "[$TEST_FILE] Create Docker external volumes (local)" {
  command docker volume create -d local ${BATS_CLAIR_LOCAL_SCANNER_CONFIG_VOLUME_NAME}
}

@test "[$TEST_FILE] Pull all Docker images" {
  command docker-compose -f docker-compose.yml pull s3 
}

@test "[$TEST_FILE] Starting Storage Services (S3)" {
  command docker-compose -f docker-compose.yml up -d s3 
  docker_wait_for_log s3 240 "Ready."
}

@test "[$TEST_FILE] Loading Test Data files Storage services (S3)" {
  run aws s3 mb s3://${BATS_S3_BUCKET_NAME%/} --endpoint-url ${BATS_S3_ENDPOINT_URL}
  assert_output -l -r "make_bucket: $BATS_S3_BUCKET_NAME"

  run aws s3api put-bucket-acl --bucket s3://${BATS_S3_BUCKET_NAME%/} --acl public-read --endpoint-url ${BATS_S3_ENDPOINT_URL}

  run init_s3bucket ${BATS_TEST_DIRNAME%/}/assets/example.tar.gz $BATS_S3_BUCKET_NAME $BATS_S3_ENDPOINT_URL 
  assert_output -l -r 'S3 DATA COPY OK'

}

@test "[$TEST_FILE] Starting 'WebTech S3 Exporter (Prometheus Exporter)' Service" {
  export BATS_S3_ENDPOINT_URL=http://$(docker_ip s3):4572
  command docker-compose up -d exporter
}

@test "[$TEST_FILE] Test 'WebTech S3 Exporter (Prometheus Exporter)' /health" {
  retry 12 5 curl_container exporter :9773/health -s -w %{http_code} -o /dev/null
  assert_output -l 0 $'200'
  retry 12 5 curl_container exporter :9773/health -s
  assert_output -l -r 'OK'
}


@test "[$TEST_FILE] Test 'WebTech S3 Exporter (Prometheus Exporter)' /metrics" {
  retry 12 5 curl_container exporter :9773/metrics -s -w %{http_code} -o /dev/null
  assert_output -l 0 $'200'
  retry 12 5 curl_container exporter :9773/metrics -s
  assert_output -l "webtech_s3_bucket_count_total{name=\"${BATS_S3_BUCKET_NAME}\"} webtech_s3_bucket_count_total 5.000"
}

@test "[$TEST_FILE] Stop all and delete test containers" {
  command docker-compose -f docker-compose.yml stop
  command docker-compose -f docker-compose.yml rm -v -f  
}

@test "[$TEST_FILE] Cleanup Docker external volumes (local)" {
  command docker volume rm ${BATS_CLAIR_LOCAL_SCANNER_CONFIG_VOLUME_NAME}
}
