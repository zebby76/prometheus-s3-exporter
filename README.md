# prometheus-s3-exporter ![Continuous Docker Image Build](https://github.com/Smals-Webtech/prometheus-s3-exporter/workflows/Continuous%20Docker%20Image%20Build/badge.svg)

## Build

```
docker build --build-arg VERSION_ARG=snapshot \
             --build-arg RELEASE_ARG=snapshot \
             --build-arg BUILD_DATE_ARG=snapshot \
             --build-arg VCS_REF_ARG=snapshot \
             --tag docker.io/smalswebtech/prometheus-s3-exporter:rc .
```

## Test

```
bats test/tests.bats
```

