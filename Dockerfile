FROM python:3.9-slim

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
    VERSION=${VERSION_ARG}-${RELEASE_ARG}

COPY src/ /opt/src
COPY bin/ /opt/bin

RUN mkdir -p /opt/bin /opt/src /opt/etc \
    && apt-get -y update \
    && apt-get -y install --no-install-recommends build-essential dh-autoreconf \
    && echo "Add default user ..." \
    && addgroup --gid 1001 default \
    && adduser --system --uid 1001 --gid 1001 --disabled-login --no-create-home default \
    && echo "Configure application ..." \
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
