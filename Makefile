#!/usr/bin/make -f

ifneq (,$(wildcard ./.build.env))
    include .build.env
    export
endif

GIT_HASH ?= $(shell git log --format="%h" -n 1)
BUILD_DATE ?= $(shell date -u +'%Y-%m-%dT%H:%M:%SZ')

# Default Prometheus Exporter version (if no .build.env file provided)
EXPORTER_VERSION ?= 3.0.2

# Default Docker image name (if no .build.env file provided)
DOCKER_IMAGE_NAME ?= docker.io/smalswebtech/prometheus-s3-exporter

# Default AWS Cli version (if no .build.env file provided)
AWS_CLI_VERSION ?= 1.27.133

_BUILD_ARGS_TAG ?= rc

.DEFAULT_GOAL := help
.PHONY: help build buildah test test-with-podman

help: # Show help for each of the Makefile recipes.
	@grep -E '^[a-zA-Z0-9 -]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m$$(echo $$l | cut -f 1 -d':')\033[00m:$$(echo $$l | cut -f 2- -d'#')\n"; done

build: # Build [exporter] Docker images
	@$(MAKE) -s _builder \
		-e _BUILD_ARGS_TAG="${EXPORTER_VERSION}" 

_builder: 
	@echo "Build [${DOCKER_IMAGE_NAME}:${_BUILD_ARGS_TAG}] Docker image ..."
	@docker build --progress=plain --no-cache \
		--build-arg VERSION_ARG="${EXPORTER_VERSION}" \
		--build-arg RELEASE_ARG="${_BUILD_ARGS_TAG}" \
		--build-arg BUILD_DATE_ARG="${BUILD_DATE}" \
		--build-arg VCS_REF_ARG="${GIT_HASH}" \
		--build-arg AWS_CLI_VERSION_ARG=${AWS_CLI_VERSION} \
		--tag ${DOCKER_IMAGE_NAME}:${_BUILD_ARGS_TAG} -f Dockerfile .

buildah: # Build [exporter] OCI image (buildah)
	@$(MAKE) -s _buildaher \
		-e _BUILD_ARGS_TAG="${EXPORTER_VERSION}" 

_buildaher: 
	@echo "Build [${DOCKER_IMAGE_NAME}:${_BUILD_ARGS_TAG}] OCI image ..."
	@buildah bud --no-cache --pull-always --force-rm --squash \
		--build-arg VERSION_ARG="${EXPORTER_VERSION}" \
		--build-arg RELEASE_ARG="${_BUILD_ARGS_TAG}" \
		--build-arg BUILD_DATE_ARG="${BUILD_DATE}" \
		--build-arg VCS_REF_ARG="${GIT_HASH}" \
		--build-arg AWS_CLI_VERSION_ARG=${AWS_CLI_VERSION} \
		--tag ${DOCKER_IMAGE_NAME}:${_BUILD_ARGS_TAG} -f Dockerfile .

test: # Test [exporter] Docker images
	@$(MAKE) -s _tester \
		-e _TEST_ARGS_TAG="${EXPORTER_VERSION}" 

_tester: 
	@$(MAKE) -s _bats \
		-e DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME}:${_TEST_ARGS_TAG}"

_bats:
	@echo "Test [${DOCKER_IMAGE_NAME}] Docker image ..."
	@bats test/tests.bats

test-with-podman: # Test [exporter] Docker images (with Podman)
	@$(MAKE) -s _tester_with_podman \
		-e _TEST_ARGS_TAG="${EXPORTER_VERSION}"

_tester_with_podman:
	@$(MAKE) -s _bats_with_podman \
		-e DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME}:${_TEST_ARGS_TAG}"

_bats_with_podman:
	@echo "Test [${DOCKER_IMAGE_NAME}] Docker image with Podman ..."
	@bats test/tests-podman.bats
