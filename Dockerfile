# syntax=docker/dockerfile:1.3
FROM python:3.11-slim-buster

ARG AWS_CLI_VERSION_ARG=""
ARG VERSION_ARG=""
ARG RELEASE_ARG=""
ARG BUILD_DATE_ARG=""
ARG VCS_REF_ARG=""

LABEL be.smals.webtech.monitoring.build-date=$BUILD_DATE_ARG \
      be.smals.webtech.monitoring.name="WebTech Monitoring Prometheus S3 Exporter" \
      be.smals.webtech.monitoring.description="Expose S3 bucket sizes as Prometheus Metrics." \
      be.smals.webtech.monitoring.url="https://wwww.smals.be" \
      be.smals.webtech.monitoring.vcs-ref=$VCS_REF_ARG \
      be.smals.webtech.monitoring.vcs-url="https://github.com/Smals-Webtech/prometheus-s3-exporter-docker" \
      be.smals.webtech.monitoring.vendor="sebastian.molle@smals.be" \
      be.smals.webtech.monitoring.version=$VERSION_ARG \
      be.smals.webtech.monitoring.release=$RELEASE_ARG \
      be.smals.webtech.monitoring.schema-version="1.0"

USER root

WORKDIR /opt/src

ENV PATH=/opt/bin:${PATH} \
    VERSION=${VERSION_ARG}-${RELEASE_ARG} \
    AWS_CLI_VERSION=${AWS_CLI_VERSION_ARG} \
    AWS_CLI_DOWNLOAD_URL="https://github.com/aws/aws-cli/archive" 

COPY src/ /opt/src
COPY bin/ /opt/bin

RUN ln -s /opt/bin/apk-list /usr/local/bin/apk-list \
    && ln -s /opt/bin/apt-list /usr/local/bin/apt-list \
    && ln -s /opt/bin/pip-list /usr/local/bin/pip-list \
    && mkdir -p /opt/bin /opt/src /opt/etc \
    && apt-get -y update \
    && apt-get -y install --no-install-recommends build-essential dh-autoreconf curl groff \
    && echo "Add default user ..." \
    && addgroup --gid 1001 default \
    && adduser --system --uid 1001 --gid 1001 --disabled-login --no-create-home default \
    && echo "Download and install aws-cli ..." \
    && mkdir -p /tmp/aws-cli \
    && curl -sSfLk ${AWS_CLI_DOWNLOAD_URL}/${AWS_CLI_VERSION}.tar.gz | tar -xzC /tmp/aws-cli --strip-components=1 \
    && cd /tmp/aws-cli \
    && python3 setup.py install \
    && rm -Rf /tmp/aws-cli /var/cache/apk/* \
    && echo "Configure application ..." \
    && cd /opt/src \
    && pip3 install -e . \
    && chown 1001:0 -Rf /opt/bin /opt/src \
    && chmod -Rf ug+rw /opt/bin /opt/src \
    && chmod -Rf +x /opt/bin \
    && find /opt/bin -type d -exec chmod ug+x {} \; \
    && find /opt/src -type d -exec chmod ug+x {} \; \
    && echo "Cleanup ..." \
    && apt-get -y autoremove --purge build-essential dh-autoreconf \
    && apt-get -y clean \
    && rm -rf /var/lib/apt/lists/*

USER 1001

EXPOSE 9773/tcp
EXPOSE 9774/tcp

ENTRYPOINT ["python3", "-u", "/usr/local/bin/exporter"]

HEALTHCHECK --start-period=10s --interval=1m --timeout=5s --retries=5 \
        CMD curl --fail --header "Host: localhost" http://localhost:9774/health || exit 1
