FROM alpine:3.12 AS xinetd-builder

ARG XINETD_VERSION="2.3.15.4"
ARG XINETD_RELEASE_DL="https://github.com/openSUSE/xinetd/releases/download/${XINETD_VERSION}/xinetd-${XINETD_VERSION}.tar.xz"
WORKDIR "/tmp/xinetd/src"

RUN mkdir /build -p \
    && wget -O ../xinetd.tar.xz "${XINETD_RELEASE_DL}" \
    && cd ../ && tar xf xinetd.tar.xz  --strip 1 --directory src \
    && cd src \
    && apk add --update --no-cache alpine-sdk autoconf automake \
    && aclocal \
    && automake \
    && ./configure --prefix /build \
    && make \
    && make install \
    && rm /build/share -rf \
    && rm /build/etc/xinetd.d/*

FROM docker.io/alpine:3.12

ARG VERSION_ARG=""
ARG RELEASE_ARG=""
ARG BUILD_DATE_ARG=""
ARG VCS_REF_ARG=""
ARG AWS_CLI_VERSION_ARG=""

LABEL be.smals.webtech.monitoring.build-date=$BUILD_DATE_ARG \
      be.smals.webtech.monitoring.name="WebTech Monitoring Prometheus S3 Exporter" \
      be.smals.webtech.monitoring.description="Return metrics about one S3 bucket." \
      be.smals.webtech.monitoring.url="https://wwww.smals.be" \
      be.smals.webtech.monitoring.vcs-ref=$VCS_REF_ARG \
      be.smals.webtech.monitoring.vcs-url="https://github.com/Smals-Webtech/prometheus-s3-exporter-docker" \
      be.smals.webtech.monitoring.vendor="sebastian.molle@smals.be" \
      be.smals.webtech.monitoring.version=$VERSION_ARG \
      be.smals.webtech.monitoring.release=$RELEASE_ARG \
      be.smals.webtech.monitoring.schema-version="1.0"

USER root

ENV VERSION=${VERSION_ARG} \
    PATH=/opt/bin:/usr/local/bin:/usr/bin:${PATH} \
    HOME=/home/default \
    AWS_CLI_VERSION=${AWS_CLI_VERSION_ARG} \
    AWS_CLI_DOWNLOAD_URL="https://github.com/aws/aws-cli/archive" 

COPY --from=xinetd-builder /build /

COPY bin/ /usr/local/bin/
COPY metrics/ /opt/bin/
COPY etc/ /etc/

RUN mkdir -p /opt/bin /opt/src /opt/etc \
    && chmod +x /usr/local/bin/apk-list \
                /usr/local/bin/container-entrypoint \
                /usr/local/bin/wait-for-it \
                /usr/local/bin/http-wrapper \
    && echo "Add default user ..." \
    && adduser -D -u 1001 -g default -s /sbin/nologin default \
    && chown -R 1001:0 /opt \
    && chmod -R ug+rw /opt \
    && find /opt -type d -exec chmod ug+x {} \; \
    && echo "Install Alpine packages ..." \
    && apk add --update --virtual .exporter-rundeps findutils binutils coreutils bash tzdata supervisor curl ca-certificates openssl libressl python3 \
    && touch /var/log/supervisord.log \
    && touch /var/run/supervisord.pid \
    && chown -Rf 1001:0 /var/log/supervisord.log /etc/supervisord.conf /var/run/supervisord.pid \
    && chmod -R ug+rw /var/log/supervisord.log /etc/supervisord.conf /var/run/supervisord.pid \
    && echo "Configure Timezone ..." \
    && cp /usr/share/zoneinfo/Europe/Brussels /etc/localtime \
    && echo "Europe/Brussels" > /etc/timezone \
    && echo "Download and install aws-cli ..." \
    && mkdir -p /tmp/aws-cli \
    && curl -sSfLk ${AWS_CLI_DOWNLOAD_URL}/${AWS_CLI_VERSION}.tar.gz | tar -xzC /tmp/aws-cli --strip-components=1 \
    && cd /tmp/aws-cli \
    && python3 setup.py install \
    && cd /opt && rm -Rf /tmp/aws-cli /var/cache/apk/* 

USER 1001

EXPOSE 9773/tcp
EXPOSE 9774/tcp

ENTRYPOINT ["container-entrypoint"]

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
