#!/usr/bin/make -f

.DEFAULT_GOAL := help
.PHONY: help build test unit lint up up-wait down logs

help: # Show help for each of the Makefile recipes.
	@echo "Prometheus S3 Exporter Build / Test"
	@echo "---------------------------"
	@echo "CURRENT_USERNAME: ${CURRENT_USERNAME}"
	@echo "CURRENT_HOMEDIR: ${CURRENT_HOMEDIR}"
	@echo "CURRENT_DIR: ${CURRENT_DIR}"
	@echo ""
	@echo "Image name: ${DOCKER_IMAGE_NAME}"
	@echo "Image platform: ${DOCKER_PLATFORM}"
	@echo "Image builder: ${DOCKER_BUILDER}"
	@echo "Image output: ${DOCKER_OUTPUT}"
	@echo ""
	@echo "METRICS:       http://localhost:9773/metrics"
	@echo "HEALTH:        http://localhost:9774/healthz"
	@echo "---------------------------"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo ""
	@grep -E '(^\S*:.*?##.*$$)|(^##)' Makefile | awk 'BEGIN {FS = ":.*?## "}{printf "\033[32m%-30s\033[0m %s\n", $$1, $$2}' | sed -e 's/\[32m##/[33m/'

# —— Environment ——————————————————————————————————————————————————————————————————————————————————————————————————————

CURRENT_USERNAME            := $(shell id -u -n)
CURRENT_HOMEDIR             := $${HOME}
CURRENT_DIR                 := $(shell pwd)

# bake reads DOCKER_IMAGE_NAME from the environment, and make does not export a variable it
# assigned itself, so without this the value below would only ever reach the help text while bake
# fell back to its own default. Exported, this file is the single source of truth. The value has to
# stay in the registry-less form bake uses, so local tags match the ones CI publishes.
DOCKER_IMAGE_NAME           ?= zebby76/prometheus-s3-exporter
# The export has to come after the assignment: `export VAR` on its own defines VAR as empty, which
# would make the `?=` above a no-op.
export DOCKER_IMAGE_NAME

PYTHON                      ?= python3

DOCKER_PLATFORM             ?= linux/amd64
DOCKER_BUILDER              ?= default
DOCKER_OUTPUT               ?= type=image

# —— Docker Compose Stack —————————————————————————————————————————————————————————————————————————————————————————————

COMPOSE_SERVICE             ?= exporter
SERVICE                     ?=

build: ## build [ options ]
	@$(MAKE) _docker-bake/default

# The smoke test runs the prd image, the one CI publishes: compose.prod.yaml
# overrides the dev target compose.override.yaml builds for local work. Exported,
# so the sub-makes and the smoke script's own `docker compose` calls see the
# same files.
test: export COMPOSE_FILE = compose.yaml:compose.override.yaml:compose.prod.yaml
test: ## test : start the stack health-gated with the prd image, assert the exposed metrics, then tear it down
	@# Start from a clean volume: fixtures left by an interrupted run would be
	@# counted alongside test/assets and fail the assertions.
	@$(MAKE) down
	@$(MAKE) up-wait || { docker compose logs $(COMPOSE_SERVICE); $(MAKE) down; exit 1; }
	@$(MAKE) _assert-metrics || { docker compose logs $(COMPOSE_SERVICE); $(MAKE) down; exit 1; }
	@$(MAKE) down

unit: ## unit : run the unit tests (no network, no containers)
	@$(PYTHON) -m pytest test/unit -q

lint: ## lint : run super-linter locally over the whole codebase
	@# -it only when there is a terminal, so the target stays scriptable.
	@docker run --rm $$([ -t 0 ] && echo -it) \
		-e RUN_LOCAL=true \
		-e DEFAULT_BRANCH=main \
		-e VALIDATE_ALL_CODEBASE=true \
		-e LINTER_RULES_PATH=/ \
		-e IGNORE_GITIGNORED_FILES=true \
		-e VALIDATE_MARKDOWN_PRETTIER=false \
		-e VALIDATE_DOCKERFILE_HADOLINT=false \
		-e VALIDATE_PYTHON_PYLINT=false \
		-e VALIDATE_PYTHON_MYPY=false \
		-e VALIDATE_PYTHON_PYINK=false \
		-e VALIDATE_JSCPD=false \
		-e "FILTER_REGEX_EXCLUDE=_version.py|versioneer.py" \
		-e "KUBERNETES_KUBECONFORM_OPTIONS=-schema-location default -schema-location https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
		-v $(CURRENT_DIR):/tmp/lint \
		ghcr.io/super-linter/super-linter:slim-v7.4.0

up: ## up : start the whole stack in the background (builds the dev image, loads the bucket)
	@docker compose up -d --build

# --build is not optional here: compose reuses an existing image when the
# sources change, so without it `make test` happily validates stale code.
up-wait: ## up-wait : start the exporter and its fixtures, blocking until everything is healthy
	@docker compose up -d --build --wait --wait-timeout 180 $(COMPOSE_SERVICE)

down: ## down : stop the stack and drop its volumes
	@docker compose down -v --remove-orphans

logs: ## logs [ SERVICE=... ] : follow the stack logs
	@docker compose logs -f $(SERVICE)

# The health endpoint answers before the first collection completes, so the
# assertions poll /metrics rather than trusting the health gate alone.
_assert-metrics:
	@./test/smoke/assert_metrics.sh

# —— Observability stack ——————————————————————————————————————————————————————————————————————————————————————————————
#
# test/observability: one exporter per bucket behind a cluster-like DNS and a load balancer, with Prometheus, Loki and
# Grafana behind Traefik. Its own compose project, so it never touches the smoke-test stack above.

OBS_DIR                     := test/observability
OBS                         := docker compose --project-directory $(OBS_DIR) -f $(OBS_DIR)/compose.yaml
OBS_FAULT                   := $(OBS_DIR)/dnsfault/state/fault.conf
OBS_TLS                     := $(OBS_DIR)/tls/out
# The fake cluster's state (Secrets, grants, the multi-bucket exporter's ConfigMap), see kube/state.py.
OBS_KUBE                    := docker run --rm -u "$$(id -u):$$(id -g)" --env-file $(OBS_DIR)/.env \
                               -v "$(CURDIR)/$(OBS_DIR)/kube:/kube" -v "$(CURDIR)/$(OBS_TLS):/tls:ro" \
                               docker.io/library/python:3.12-slim-trixie python3 /kube/state.py
# Prometheus discovers its targets through the Docker socket, whose group differs from one host to the next.
export DOCKER_GID           ?= $(shell stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)
# Only the storage endpoint's queries are disturbed, search-list expansions included.
MATCH                       ?= storage\.internal
RATIO                       ?= 0.33
DELAY_MS                    ?= 300
IDLE                        ?= 25s

.PHONY: obs/up obs/down obs/seed obs/ps obs/logs obs/urls

##
## Observability stack (test/observability):
##

obs/up: ## obs/up : build the dev image, start the stack, fill the buckets, wait until everything is up
	@[ -f $(OBS_FAULT) ] || printf 'mode=off\n' > $(OBS_FAULT)
	@# A new CA invalidates every certificate in use: whatever loaded the old ones is recreated with the new.
	@if [ ! -f $(OBS_TLS)/kube-api.pem ]; then \
		docker run --rm -u "$$(id -u):$$(id -g)" -v "$(CURDIR)/$(OBS_DIR)/tls:/tls" \
			--entrypoint sh docker.io/library/haproxy:3.2 /tls/make-certs.sh && \
		$(OBS) rm -sf lb kube-api exporter-tls exporter-multi >/dev/null 2>&1; \
		rm -rf $(OBS_DIR)/kube/state; \
	fi
	@$(OBS_KUBE) init
	@$(OBS) up -d --build --wait --wait-timeout 600
	@$(MAKE) --no-print-directory obs/urls

obs/down: ## obs/down : stop the stack and drop its volumes (buckets, metrics, logs)
	@$(OBS) down -v --remove-orphans
	@rm -f $(OBS_FAULT)
	@rm -rf $(OBS_TLS) $(OBS_DIR)/kube/state

obs/seed: ## obs/seed [ SEED_BUCKETS=name:count:min:max,... ] : create or complete the demo buckets
	@$(OBS) run --rm $${SEED_BUCKETS:+-e BUCKETS=$$SEED_BUCKETS} seed

obs/ps: ## obs/ps : show the stack's containers
	@$(OBS) ps -a

obs/logs: ## obs/logs [ SERVICE=... ] : follow the stack logs
	@$(OBS) logs -f $(SERVICE)

obs/urls: ## obs/urls : print the stack's entry points
	@port=$$(grep -E '^OBS_HTTP_PORT=' $(OBS_DIR)/.env | cut -d= -f2); p=$${OBS_HTTP_PORT:-$$port}; s=$$([ "$$p" = 80 ] || echo ":$$p"); \
	 echo "Grafana     http://grafana.localhost$$s  (admin/admin)"; \
	 echo "Dashboard   http://grafana.localhost$$s/d/s3-exporter-table"; \
	 echo "Prometheus  http://prometheus.localhost$$s"; \
	 echo "Loki        http://loki.localhost$$s"; \
	 echo "Alloy       http://alloy.localhost$$s"; \
	 echo "Exporters   http://{small,medium,large,tls,multi}.exporter.localhost$$s/metrics"; \
	 echo "Traefik     http://localhost:$${OBS_TRAEFIK_PORT:-8888}"; \
	 echo "DNS fault   $$(tr '\n' ' ' < $(OBS_FAULT) 2>/dev/null || echo 'mode=off')"

.PHONY: obs/chaos/dns-drop obs/chaos/dns-delay obs/chaos/dns-servfail obs/chaos/s3-down obs/chaos/lb-down \
	obs/chaos/lb-refuse obs/chaos/lb-idle obs/chaos/tls-wrong-name obs/chaos/tls-expired obs/chaos/tls-expiring \
	obs/chaos/off

obs/chaos/dns-drop: ## obs/chaos/dns-drop [ RATIO=0.33 MATCH=... ] : lose that share of DNS queries (resolver timeout)
	@printf 'mode=drop\nratio=$(RATIO)\nmatch=$(MATCH)\n' > $(OBS_FAULT)
	@echo "DNS: dropping $(RATIO) of the queries matching $(MATCH)"

obs/chaos/dns-delay: ## obs/chaos/dns-delay [ DELAY_MS=300 MATCH=... ] : slow every matching DNS query down
	@printf 'mode=delay\ndelay_ms=$(DELAY_MS)\nmatch=$(MATCH)\n' > $(OBS_FAULT)
	@echo "DNS: delaying the queries matching $(MATCH) by $(DELAY_MS)ms"

obs/chaos/dns-servfail: ## obs/chaos/dns-servfail [ RATIO=1 MATCH=... ] : answer SERVFAIL (fast DNS failure)
	@printf 'mode=servfail\nratio=$(if $(filter command line,$(origin RATIO)),$(RATIO),1)\nmatch=$(MATCH)\n' > $(OBS_FAULT)
	@echo "DNS: SERVFAIL on the queries matching $(MATCH)"

obs/chaos/s3-down: ## obs/chaos/s3-down : stop the storage behind the load balancer (the LB answers 503)
	@$(OBS) stop seaweedfs

obs/chaos/lb-down: ## obs/chaos/lb-down : stop the load balancer (its address no longer answers)
	@$(OBS) stop lb

obs/chaos/lb-refuse: ## obs/chaos/lb-refuse : keep the LB up with no listener on the S3 port (connection refused)
	@LB_BIND_PORT=8334 $(OBS) up -d --force-recreate lb
	@echo "LB: nothing listens on 8333 any more"

obs/chaos/lb-idle: ## obs/chaos/lb-idle [ IDLE=25s ] : set the LB idle timeout; under 30s every probe reconnects and resolves
	@LB_IDLE_TIMEOUT=$(IDLE) $(OBS) up -d --force-recreate lb
	@echo "LB: idle timeout $(IDLE)"

obs/chaos/tls-wrong-name: ## obs/chaos/tls-wrong-name : the LB's TLS listener presents a certificate for another name
	@LB_TLS_CERT=wrong-name.pem $(OBS) up -d --force-recreate lb
	@echo "LB: TLS certificate for other.storage.internal"

obs/chaos/tls-expired: ## obs/chaos/tls-expired : the LB's TLS listener presents an expired certificate
	@LB_TLS_CERT=expired.pem $(OBS) up -d --force-recreate lb
	@echo "LB: expired TLS certificate"

obs/chaos/tls-expiring: ## obs/chaos/tls-expiring : the LB's TLS certificate ends in 10 days (the exporter warns at 14)
	@LB_TLS_CERT=expiring.pem $(OBS) up -d --force-recreate lb
	@echo "LB: TLS certificate ending in 10 days"

obs/chaos/off: ## obs/chaos/off : clear the DNS fault, restart the storage and the LB with its default idle timeout
	@printf 'mode=off\n' > $(OBS_FAULT)
	@$(OBS) up -d seaweedfs
	@$(OBS) up -d --force-recreate lb
	@echo "All faults cleared"

.PHONY: obs/kube/reset obs/kube/forbid obs/kube/allow obs/kube/delete obs/kube/wrong-key obs/kube/repoint \
	obs/kube/config-add obs/kube/config-remove obs/kube/config-broken

# The multi-bucket exporter's fake cluster. NAME is namespace/secret, e.g. NAME=app-a/app-a-uploads-s3.
NAME                        ?= app-a/app-a-uploads-s3
BUCKET                      ?= bucket-small

obs/kube/reset: ## obs/kube/reset : put back every Secret, Role and the multi-bucket exporter's ConfigMap
	@$(OBS_KUBE) reset

obs/kube/forbid: ## obs/kube/forbid [ NAME=ns/secret ] : drop the Role's grant on a Secret (403, a missing RoleBinding)
	@$(OBS_KUBE) forbid $(NAME)

obs/kube/allow: ## obs/kube/allow [ NAME=ns/secret ] : grant it again
	@$(OBS_KUBE) allow $(NAME)

obs/kube/delete: ## obs/kube/delete [ NAME=ns/secret ] : delete a Secret (404)
	@$(OBS_KUBE) delete $(NAME)

obs/kube/wrong-key: ## obs/kube/wrong-key [ NAME=ns/secret ] : put a key S3 rejects in a Secret
	@$(OBS_KUBE) wrong-key $(NAME)

obs/kube/repoint: ## obs/kube/repoint [ NAME=ns/secret BUCKET=... ] : point a Secret at another bucket
	@$(OBS_KUBE) repoint $(NAME) $(BUCKET)

obs/kube/config-add: ## obs/kube/config-add : add a bucket to the multi-bucket exporter's ConfigMap
	@$(OBS_KUBE) config-add

obs/kube/config-remove: ## obs/kube/config-remove : remove a bucket from it
	@$(OBS_KUBE) config-remove

obs/kube/config-broken: ## obs/kube/config-broken : publish a collector.yml that is not valid YAML
	@$(OBS_KUBE) config-broken

# —— Docker build —————————————————————————————————————————————————————————————————————————————————————————————————————

_docker-bake/%:
	@echo "\n-- Running Docker bake --\n"
	@docker buildx bake --progress=plain \
		--set *.platform=${DOCKER_PLATFORM} \
		--set *.output=${DOCKER_OUTPUT} \
		--builder ${DOCKER_BUILDER} \
		${*}

##
## Options: DOCKER_PLATFORM="linux/amd64,linux/arm64" DOCKER_BUILDER="cloud-remote" DOCKER_OUTPUT="type=registry" DOCKER_IMAGE_NAME="zebby76/prometheus-s3-exporter"
##

# —— Release & Tag ————————————————————————————————————————————————————————————————————————————————————————————————————

.PHONY: release-info sync release retag tag-restore notes build-version

# Canonical remote: 'upstream' when working from a fork, else 'origin' (the real repo).
# NB: this copy never names the parent repo's remote `upstream`, precisely so releases keep
# targeting origin (zebby76). Naming it `upstream` would push tags to the parent.
RELEASE_REMOTE              ?= $(shell git remote | grep -qx upstream && echo upstream || echo origin)
# Fork remote we ALSO push branches to (no-op when it equals the canonical remote).
FORK_REMOTE                 ?= origin
# GitHub owner/repo slug derived from the canonical remote URL (for `gh --repo`).
REPO                        ?= $(shell git config --get remote.$(RELEASE_REMOTE).url | sed -E 's#^(git@[^:]+:|https?://[^/]+/)##; s#\.git$$##')

# Commit/ref a tag should point at (default: current HEAD).
REF                         ?= HEAD
# Previous tag for release notes (default: auto-detected as the next-lower semver tag).
SINCE                       ?=
# Set YES=1 to skip confirmation prompts on destructive operations.
YES                         ?=

# Shell snippet: print the semver tag immediately below VERSION (works whether or not VERSION is already a tag).
PREV_TAG = (git tag --sort=-v:refname | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$$'; echo $(VERSION)) | sort -rV | uniq | awk -v v=$(VERSION) '$$0==v{p=1;next} p{print;exit}'

##
## Release & Tag (VERSION=x.y.z, REF=HEAD, SINCE=x.y.z, YES=1 to skip prompts; remote auto-detected):
##

release-info: ## release-info : show detected canonical remote, repo slug, branch and latest tags
	@echo "canonical remote : $(RELEASE_REMOTE)"
	@echo "fork remote      : $(FORK_REMOTE)"
	@echo "gh repo slug     : $(REPO)"
	@echo "current branch   : $$(git rev-parse --abbrev-ref HEAD)"
	@echo "latest tags      : $$(git tag --sort=-v:refname | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$$' | head -5 | tr '\n' ' ')"

sync: ## sync : fast-forward main from the canonical remote (no fork here, so the fork push is a no-op)
	@git fetch $(RELEASE_REMOTE) --no-tags --prune
	@cur=$$(git rev-parse --abbrev-ref HEAD); \
	 for b in main; do \
	   git show-ref --verify --quiet refs/heads/$$b || continue; \
	   git rev-parse --verify --quiet $(RELEASE_REMOTE)/$$b >/dev/null || continue; \
	   if ! git merge-base --is-ancestor $$b $(RELEASE_REMOTE)/$$b; then echo "skip $$b (not fast-forwardable)"; continue; fi; \
	   if [ "$$b" = "$$cur" ]; then git merge --ff-only $(RELEASE_REMOTE)/$$b >/dev/null; else git branch -f $$b $(RELEASE_REMOTE)/$$b; fi; \
	   echo "FF $$b -> $$(git rev-parse --short $$b)"; \
	 done
	@if [ "$(FORK_REMOTE)" != "$(RELEASE_REMOTE)" ]; then \
	   for b in main; do git show-ref --verify --quiet refs/heads/$$b && git push $(FORK_REMOTE) $$b || true; done; \
	 fi

release: ## release VERSION=x.y.z : prepare commit + signed tag + push + GitHub release (new version)
	@[ -n "$(VERSION)" ] || { echo "VERSION=x.y.z required" >&2; exit 1; }
	@b=$$(git rev-parse --abbrev-ref HEAD); case "$$b" in main|[0-9]*.[0-9]*) ;; *) echo "release only from main or x.y (on $$b)" >&2; exit 1;; esac
	@git rev-parse "$(VERSION)" >/dev/null 2>&1 && { echo "tag $(VERSION) already exists -> use 'make retag VERSION=$(VERSION)'" >&2; exit 1; } || true
	@# `git commit -a` stages tracked files only, so a new file that was never added
	@# would be released without ever being committed. --exclude-standard honours
	@# .git/info/exclude, so a deliberately untracked file does not trip this.
	@u=$$(git ls-files --others --exclude-standard); [ -z "$$u" ] || { \
		echo "untracked files present -- 'git commit -a' would SKIP them:" >&2; \
		echo "$$u" | sed 's/^/  /' >&2; \
		echo "git add them (or exclude them) before releasing" >&2; exit 1; }
	@if [ -z "$(YES)" ]; then printf "Release $(VERSION) from $$(git rev-parse --abbrev-ref HEAD) to $(RELEASE_REMOTE) ($(REPO))? [y/N] "; read a; [ "$$a" = y ] || [ "$$a" = Y ] || { echo Aborted >&2; exit 1; }; fi
	@# Only a genuinely clean tree may skip the commit. `git commit ... || echo` used
	@# to swallow every failure: a locked GPG agent made the signature fail, printed
	@# "No changes to commit", and the target carried on to tag a tree with no release
	@# commit in it. Measured: the old form exits 0 on a signing failure, this one 128.
	@if [ -n "$$(git status --porcelain --untracked-files=no)" ]; then \
		git commit -S -a -m "chore: prepare release $(VERSION)"; \
	else \
		echo "Nothing to commit -- tagging the current HEAD."; \
	fi
	@git tag -s -m "Version $(VERSION)" $(VERSION)
	@b=$$(git rev-parse --abbrev-ref HEAD); \
	 git push $(RELEASE_REMOTE) $$b; \
	 git push $(RELEASE_REMOTE) refs/tags/$(VERSION); \
	 [ "$(FORK_REMOTE)" != "$(RELEASE_REMOTE)" ] && git push $(FORK_REMOTE) $$b || true
	@prev=$$( $(PREV_TAG) ); \
	 latest=""; [ "$$(git rev-parse --abbrev-ref HEAD)" = "main" ] && latest="--latest"; \
	 gh release create $(VERSION) --repo $(REPO) --generate-notes $$latest $${prev:+--notes-start-tag $$prev} --verify-tag && echo "release $(VERSION) created"

retag: ## retag VERSION=x.y.z [REF=HEAD] : move an existing tag, fire the tag build, refresh the release
	@[ -n "$(VERSION)" ] || { echo "VERSION=x.y.z required" >&2; exit 1; }
	@b=$$(git rev-parse --abbrev-ref HEAD); case "$$b" in main|[0-9]*.[0-9]*) ;; *) echo "retag only from main or x.y (on $$b)" >&2; exit 1;; esac
	@sha=$$(git rev-parse --short "$(REF)"); \
	 echo "retag $(VERSION) -> $$sha on $(RELEASE_REMOTE) ($(REPO))"; \
	 if [ -z "$(YES)" ]; then printf "Proceed (delete+recreate tag, triggers a DockerHub rebuild)? [y/N] "; read a; [ "$$a" = y ] || [ "$$a" = Y ] || { echo Aborted >&2; exit 1; }; fi
	@if [ "$$(git rev-parse $(REF))" = "$$(git rev-parse HEAD)" ]; then \
	   b=$$(git rev-parse --abbrev-ref HEAD); \
	   git push $(RELEASE_REMOTE) $$b; \
	   [ "$(FORK_REMOTE)" != "$(RELEASE_REMOTE)" ] && git push $(FORK_REMOTE) $$b || true; \
	 fi
	@git push $(RELEASE_REMOTE) :refs/tags/$(VERSION) 2>/dev/null || true
	@git tag -d $(VERSION) 2>/dev/null || true
	@git tag -s -m "Version $(VERSION)" $(VERSION) $(REF)
	@git push $(RELEASE_REMOTE) refs/tags/$(VERSION)
	@echo "tag $(VERSION) pushed -> tag build triggered"
	@if gh release view $(VERSION) --repo $(REPO) >/dev/null 2>&1; then \
	   gh release edit $(VERSION) --repo $(REPO) --draft=false >/dev/null; \
	   prev=$$( $(PREV_TAG) ); \
	   if [ -n "$$prev" ]; then \
	     gh api repos/$(REPO)/releases/generate-notes -f tag_name=$(VERSION) -f previous_tag_name=$$prev --jq .body | gh release edit $(VERSION) --repo $(REPO) --notes-file - >/dev/null && echo "release $(VERSION) re-published, notes regenerated from $$prev"; \
	   else echo "release $(VERSION) re-published (no previous tag for notes)"; fi; \
	 else echo "no GitHub release $(VERSION) (skip refresh)"; fi

tag-restore: ## tag-restore VERSION=x.y.z : force-push the LOCAL tag to the canonical remote (fix a moved tag)
	@[ -n "$(VERSION)" ] || { echo "VERSION=x.y.z required" >&2; exit 1; }
	@git rev-parse "$(VERSION)" >/dev/null 2>&1 || { echo "no local tag $(VERSION)" >&2; exit 1; }
	@sha=$$(git rev-parse --short "$(VERSION)^{commit}"); \
	 echo "force-push local tag $(VERSION) ($$sha) -> $(RELEASE_REMOTE) ($(REPO))"; \
	 if [ -z "$(YES)" ]; then printf "Proceed? [y/N] "; read a; [ "$$a" = y ] || [ "$$a" = Y ] || { echo Aborted >&2; exit 1; }; fi
	@git push --force $(RELEASE_REMOTE) refs/tags/$(VERSION)
	@echo "restored (note: a single tag push may NOT rebuild if that commit was already built -> use 'make build-version VERSION=$(VERSION)')"

notes: ## notes VERSION=x.y.z [SINCE=x.y.z] : regenerate GitHub release notes
	@[ -n "$(VERSION)" ] || { echo "VERSION=x.y.z required" >&2; exit 1; }
	@prev="$(SINCE)"; [ -n "$$prev" ] || prev=$$( $(PREV_TAG) ); \
	 [ -n "$$prev" ] || { echo "no previous tag found; pass SINCE=x.y.z" >&2; exit 1; }; \
	 gh api repos/$(REPO)/releases/generate-notes -f tag_name=$(VERSION) -f previous_tag_name=$$prev --jq .body | gh release edit $(VERSION) --repo $(REPO) --notes-file - >/dev/null && echo "notes for $(VERSION) regenerated from $$prev"

build-version: ## build-version VERSION=x.y.z : trigger a docker build via workflow_dispatch (main path)
	@[ -n "$(VERSION)" ] || { echo "VERSION=x.y.z required" >&2; exit 1; }
	@gh workflow run docker.yml --repo $(REPO) -f version=$(VERSION) && echo "workflow_dispatch sent for $(VERSION) (runs the 'main' path, not the 'tag' path)"
