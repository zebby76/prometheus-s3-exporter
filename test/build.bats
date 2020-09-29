#!/usr/bin/env bats
load "helpers/tests"
load "helpers/docker"
load "helpers/dataloaders"

load "lib/batslib"
load "lib/output"

export DOCKER_IMAGE_VERSION=${VERSION:-snapshot}
export RELEASE_NUMBER=${RELEASE_NUMBER:-snapshot}
export BUILD_DATE=${BUILD_DATE:-snapshot}
export VCS_REF=${VCS_REF:-snapshot}
export AWS_CLI_VERSION=${AWS_CLI_VERSION:-1.16.207}
export BATS_CLAIR_LOCAL_SCANNER_CONFIG_VOLUME_NAME=${BATS_CLAIR_LOCAL_SCANNER_CONFIG_VOLUME_NAME:-clair_local_scanner}

export BATS_DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME:-docker.io/zebby76/prometheus-s3-exporter}:rc"

docker-compose -f docker-compose.yml build --compress --pull --no-cache exporter

@test "[$TEST_FILE] Check Docker images build" {
  run docker inspect --type=image ${BATS_DOCKER_IMAGE_NAME}
  [ "$status" -eq 0 ]
}
