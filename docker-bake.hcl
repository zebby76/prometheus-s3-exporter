group "default" {
  targets = ["prd","dev"]
}

variable "REL" {
  default = "bookworm"
}

variable "DOCKER_IMAGE_NAME" {
  default = "zebby76/prometheus-s3-exporter"
}

variable "DOCKER_IMAGE_VERSION" {
  default = "snapshot"
}

variable "DOCKER_IMAGE_LATEST" {
  default = true
}

variable "GIT_HASH" {}

function "tag" {
  params = [version, tgt]
  result = [
    version == "" ? "" : "${DOCKER_IMAGE_NAME}:${trimprefix("${version}${tgt == "dev" ? "-dev" : ""}", "latest-")}",
  ]
}

# cleanTag ensures that the tag is a valid Docker tag
# see https://github.com/distribution/distribution/blob/v2.8.2/reference/regexp.go#L37
function "clean_tag" {
  params = [tag]
  result = substr(regex_replace(regex_replace(tag, "[^\\w.-]", "-"), "^([^\\w])", "r$0"), 0, 127)
}

# semver adds semver-compliant tag if a semver version number is passed, or returns the revision itself
# see https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
function "semver" {
  params = [rev]
  result = __semver(_semver(regexall("^v?(?P<major>0|[1-9]\\d*)\\.(?P<minor>0|[1-9]\\d*)\\.(?P<patch>0|[1-9]\\d*)(?:-(?P<prerelease>(?:0|[1-9]\\d*|\\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\\.(?:0|[1-9]\\d*|\\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\\.[0-9a-zA-Z-]+)*))?$", rev)))
}

function "_semver" {
    params = [matches]
    result = length(matches) == 0 ? {} : matches[0]
}

function "__semver" {
    params = [v]
    result = v == {} ? [clean_tag(DOCKER_IMAGE_VERSION)] : v.prerelease == null ? [v.major, "${v.major}.${v.minor}", "${v.major}.${v.minor}.${v.patch}"] : ["${v.major}.${v.minor}.${v.patch}-${v.prerelease}"]
}

target "default" {
  name = "${tgt}"

  matrix = {
    tgt = ["prd","dev"]
  }

  context    = "."
  dockerfile = "Dockerfile"
  target     = tgt

  # zebbox runs on arm64 Raspberry Pis, so the image has to be a manifest list. The workflow
  # already builds one runner per platform (native ubuntu-24.04-arm for linux/arm*), pushes each
  # arch by digest and assembles the list with `imagetools create` — listing the platform here is
  # all it takes to light that path up.
  platforms  = [
    "linux/amd64",
    "linux/arm64"
  ]

  args = {
    REL_ARG                    = "${REL}"
  }

  labels = {
    "net.zebbox.monitoring.build-date"     = "${timestamp()}"
    "net.zebbox.monitoring.name"           = "zebbox Monitoring Prometheus S3 Exporter"
    "net.zebbox.monitoring.description"    = "Expose S3 bucket sizes as Prometheus Metrics."
    "net.zebbox.monitoring.url"            = "https://www.zebbox.net"
    "net.zebbox.monitoring.vcs-ref"        = GIT_HASH
    "net.zebbox.monitoring.vcs-url"        = "https://github.com/zebby76/prometheus-s3-exporter"
    "net.zebbox.monitoring.vendor"         = "sebastian.molle@gmail.com"
    "net.zebbox.monitoring.version"        = DOCKER_IMAGE_VERSION
    "net.zebbox.monitoring.release"        = GIT_HASH
    "net.zebbox.monitoring.schema-version" = "1.0"
  }

  tags = distinct(flatten([
      DOCKER_IMAGE_LATEST ? tag("latest", tgt) : [],
      tag(GIT_HASH == "" || DOCKER_IMAGE_VERSION != "snapshot" ? "" : "sha-${substr(GIT_HASH, 0, 7)}", tgt),
      DOCKER_IMAGE_VERSION == "snapshot" ? [tag("snapshot", tgt)] : [for v in semver(DOCKER_IMAGE_VERSION) : tag(v, tgt)]
    ])
  )

  attest = [
    {
      type = "provenance"
      mode = "max"
    },
    {
      type = "sbom"
    }
  ]

}