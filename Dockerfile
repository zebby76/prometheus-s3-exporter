# syntax=docker/dockerfile:1.15

ARG REL_ARG

FROM python:3.12-slim-${REL_ARG:-bookworm} AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

FROM base AS builder

COPY . /app

RUN --mount=type=cache,target=/root/.cache/pip \
    apt-get -y update ; \
    apt-get -y install --no-install-recommends git ; \
    pip install --upgrade pip setuptools wheel ; \
    pip install .

FROM base AS prd

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/exporter /usr/local/bin/exporter

RUN addgroup --gid 1001 default ; \
    adduser --system --uid 1001 --gid 1001 --disabled-login --no-create-home default ;

USER default

EXPOSE 9773/tcp
EXPOSE 9774/tcp

ENTRYPOINT ["python3", "-u", "/usr/local/bin/exporter"]

HEALTHCHECK --start-period=2s --interval=1m --timeout=5s --retries=5 \
        CMD curl --fail --header "Host: localhost" http://localhost:9774/health || exit 1

FROM prd AS dev

USER root 

RUN apt-get -y update ; \
    apt-get -y install --no-install-recommends awscli curl ; \
    apt-get -y clean ; \
    rm -rf /var/lib/apt/lists/*

USER default