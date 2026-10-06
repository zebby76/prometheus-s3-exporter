<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->
<!-- Keep a Changelog repeats "### Added" and "### Fixed" under every version, which MD024 flags by
     default. siblings_only narrows it to duplicates under the SAME parent, which is the case that
     actually signals a mistake. Scoped to this file: a .markdownlint.yml at the root would replace
     super-linter's bundled defaults wholesale and re-enable rules the other documents rely on. -->

# Changelog

All notable changes to this image are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
the image's own `x.y.z`.

This file is the durable copy of what a release says. `make retag` and `make notes` regenerate the
GitHub release notes from the previous tag, which discards anything hand-written in them; what is
written here survives.

Releases up to and including `4.2.0` predate this file and are documented only by their generated
notes.

## [Unreleased]

### Changed

- The image is built on Debian 13 (`python:3.12-slim-trixie`) instead of Debian 12.
- The exporter runs from a virtualenv, and the image carries no pip: copying `site-packages` over
  the base image's left two pip installs mixed together, whose vendored libraries were the image's
  Python vulnerability findings (4 HIGH, now 0). The image is 292 MB instead of 339.
- CI runs the unit tests and the smoke test, against the prd image, and publishes only when they
  pass. `make test` builds the prd target, not dev.
- The release tooling refuses untracked files and no longer tags after a failed signature;
  `make lint` names its default branch.
- The test stack's S3 storage moved from MinIO to SeaweedFS (#82).
