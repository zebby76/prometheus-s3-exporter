# Prometheus S3 Exporter

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

## Build

```bash
docker bake
```

## Local Development (in-VPN)

```bash
python -m venv dev-venv
source ./dev-venv/bin/activate
pip install -e .
python exporter/__main__.py -v
```


## Lint

```bash
docker run -it --rm \
-e "VALIDATE_ALL_CODEBASE=false" \
-e "VALIDATE_YAML_PRETTIER=true" \
-e "FILTER_REGEX_EXCLUDE=_version.py|versioneer.py" \
-e RUN_LOCAL=true \
-v $(pwd):/tmp/lint \
ghcr.io/super-linter/super-linter:slim-v7.4.0
```

```bash
docker run -it --rm \
-e "VALIDATE_ALL_CODEBASE=true" \
-e "IGNORE_GITIGNORED_FILES=true" \
-e "VALIDATE_MARKDOWN_PRETTIER=false" \
-e "VALIDATE_DOCKERFILE_HADOLINT=false" \
-e "VALIDATE_PYTHON_PYLINT=false" \
-e "VALIDATE_PYTHON_MYPY=false" \
-e "VALIDATE_PYTHON_PYINK=false" \
-e "VALIDATE_JSCPD=false" \
-e "FILTER_REGEX_EXCLUDE=_version.py|versioneer.py" \
-e RUN_LOCAL=true \
-v $(pwd):/tmp/lint \
ghcr.io/super-linter/super-linter:slim-v7.4.0
```