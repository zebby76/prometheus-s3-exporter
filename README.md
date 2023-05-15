# prometheus-s3-exporter Docker Container [![Docker Build](https://github.com/Smals-Webtech/prometheus-s3-exporter/actions/workflows/docker-build.yml/badge.svg?branch=master)](https://github.com/Smals-Webtech/prometheus-s3-exporter/actions/workflows/docker-build.yml)

## Prerequisite

You must install `bats`, `make`.

## Build

```sh
make build AWS_CLI_VERSION=<AWS cli Version you want to include> EXPORTER_VERSION=<Version of exporter you want to build>
```

## Test

```sh
make test EXPORTER_VERSION=<Version of exporter you want to build>
```
