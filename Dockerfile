# syntax=docker/dockerfile:1.15

ARG REL_ARG

FROM python:3.12-slim-${REL_ARG:-trixie} AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

FROM base AS builder

COPY . /app

# The exporter and its dependencies go into a virtualenv with no pip of its own;
# the base image's pip installs into it. The prd stage copies the virtualenv
# alone. Copying site-packages over the base image's instead left two pip
# installs mixed together in the image -- and pip, which nothing runs, carried
# the image's Python vulnerability findings in its vendored libraries.
# git: versioneer derives the version from the tags.
RUN --mount=type=cache,target=/root/.cache/pip \
    apt-get -y update ; \
    apt-get -y install --no-install-recommends git ; \
    python -m venv --without-pip /opt/venv ; \
    pip --python /opt/venv/bin/python install .

FROM base AS prd

COPY --from=builder /opt/venv /opt/venv
COPY config/ /app/config/

ENV PATH=/opt/venv/bin:${PATH}

# Not needed at runtime: the base image's pip only adds what a scanner reports.
RUN /usr/local/bin/python3 -m pip uninstall --yes --root-user-action=ignore pip ; \
    addgroup --gid 1001 default ; \
    adduser --system --uid 1001 --gid 1001 --disabled-login --no-create-home default ;

USER default

EXPOSE 9773/tcp
EXPOSE 9774/tcp

ENTRYPOINT ["python3", "-u", "/opt/venv/bin/exporter"]

# python3 rather than curl: curl is only installed in the dev stage, so the
# production image reported unhealthy forever.
HEALTHCHECK --start-period=2s --interval=1m --timeout=5s --retries=5 \
        CMD python3 -c "import urllib.request as u; u.urlopen('http://localhost:9774/healthz', timeout=4)"

FROM prd AS dev

USER root 

RUN apt-get -y update ; \
    apt-get -y install --no-install-recommends awscli curl ; \
    apt-get -y clean ; \
    rm -rf /var/lib/apt/lists/*

USER default