#!/usr/bin/make -f

ifneq (,$(wildcard ./.build.env))
    include .build.env
    export
endif

LIB = ./Dockerfiles
DOCKERFILE=Dockerfile.in

GIT_HASH ?= $(shell git log --format="%h" -n 1)
BUILD_DATE ?= $(shell date -u +'%Y-%m-%dT%H:%M:%SZ')

# Default Prometheus Exporter version (if no .build.env file provided)
EXPORTER_VERSION ?= 3.0.2

# Default Docker image name (if no .build.env file provided)
DOCKER_IMAGE_NAME ?= docker.io/smalswebtech/prometheus-s3-exporter

# Default AWS Cli version (if no .build.env file provided)
AWS_CLI_VERSION ?= 1.27.133

CONTAINER_ENGINE ?= docker
CONTAINER_TARGET_IMAGE_FORMAT ?= docker

_BUILD_ARGS_TAG ?= rc

.DEFAULT_GOAL := help
.PHONY: help build test Dockerfile

help: # Show help for each of the Makefile recipes.
	@grep -E '^[a-zA-Z0-9 -]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m$$(echo $$l | cut -f 1 -d':')\033[00m:$$(echo $$l | cut -f 2- -d'#')\n"; done

build: # Build [exporter] Docker images
	@$(MAKE) -s _build-prd 

_build-%: 
	@$(MAKE) -s _builder \
		-e _BUILD_ARGS_TAG="$(EXPORTER_VERSION)-$*" \
		-e _BUILD_ARGS_TARGET="$*"

_builder: _dockerfile
    ifeq ($(CONTAINER_ENGINE),podman)
		@echo "Building $(CONTAINER_TARGET_IMAGE_FORMAT) image format with buildah"
		@buildah bud --no-cache --pull-always --force-rm --squash \
			--build-arg VERSION_ARG="${EXPORTER_VERSION}" \
			--build-arg RELEASE_ARG="${_BUILD_ARGS_TAG}" \
			--build-arg BUILD_DATE_ARG="${BUILD_DATE}" \
			--build-arg VCS_REF_ARG="${GIT_HASH}" \
			--build-arg AWS_CLI_VERSION_ARG=${AWS_CLI_VERSION} \
			--format ${CONTAINER_TARGET_IMAGE_FORMAT} \
			--target ${_BUILD_ARGS_TARGET} \
			--tag ${DOCKER_IMAGE_NAME}:${_BUILD_ARGS_TAG} .
    else
		@echo "Building $(CONTAINER_TARGET_IMAGE_FORMAT) image format with docker"
		@docker build --no-cache --force-rm --progress=plain \
			--build-arg VERSION_ARG="${EXPORTER_VERSION}" \
			--build-arg RELEASE_ARG="${_BUILD_ARGS_TAG}" \
			--build-arg BUILD_DATE_ARG="${BUILD_DATE}" \
			--build-arg VCS_REF_ARG="${GIT_HASH}" \
			--build-arg AWS_CLI_VERSION_ARG=${AWS_CLI_VERSION} \
			--target ${_BUILD_ARGS_TARGET} \
			--tag ${DOCKER_IMAGE_NAME}:${_BUILD_ARGS_TAG} .
    endif

test: # Test [exporter] Docker images
	@$(MAKE) -s _tester-prd

_tester-%: 
	@$(MAKE) -s _bats \
		-e DOCKER_IMAGE_NAME="${DOCKER_IMAGE_NAME}:${EXPORTER_VERSION}-$*" \
		-e CONTAINER_ENGINE="${CONTAINER_ENGINE}" 

_bats:
	@echo "Test [${DOCKER_IMAGE_NAME}] Docker image ..."
	@bats test/tests.bats

Dockerfile: # generate Dockerfile
	@$(MAKE) -s _dockerfile

_dockerfile: $(LIB)/*.m4
	sed -e 's/# include(\(.*\))/include(\1)/g' $(LIB)/$(DOCKERFILE) | m4 -I $(LIB) > $(DOCKERFILE:.in=)