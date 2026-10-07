# Container engine: Podman first, Docker as fallback. Override: make image ENGINE=docker
ENGINE ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
IMAGE  ?= localhost/shortforge:latest
TOPIC  ?= the lighthouse keeper who never left
# SELinux relabel for bind mounts on Linux hosts (Fedora/RHEL); macOS podman machine doesn't need it.
SELINUX ?= $(if $(filter Linux,$(shell uname -s)),:Z,)
# Podman's default OCI image format drops HEALTHCHECK; the docker format keeps it.
BUILD_FLAGS ?= $(if $(filter podman,$(notdir $(ENGINE))),--format docker,)

.PHONY: sync lint test e2e run image image-slim serve-container run-container clean

sync:            ## install deps from uv.lock into .venv
	uv sync

lint:
	uv run ruff check src tests

test:            ## unit + integration tests
	uv run pytest -q -m "not e2e"

e2e:             ## full render with real FFmpeg
	uv run pytest -q -m e2e

run:             ## run the pipeline locally (uses Piper automatically if SF_PIPER_MODEL is set)
	uv run $(if $(SF_PIPER_MODEL),--extra tts,) shortforge run "$(TOPIC)"

image:           ## build the container image (with Piper neural voice)
	$(ENGINE) build $(BUILD_FLAGS) -t $(IMAGE) .

image-slim:      ## build without Piper (espeak-ng voice only)
	$(ENGINE) build $(BUILD_FLAGS) --build-arg WITH_PIPER=0 -t $(IMAGE) .

serve-container: ## run the HTTP service in a container on :8080
	$(ENGINE) run --rm -it -p 8080:8080 --env-file $(if $(wildcard .env),.env,/dev/null) \
	  -v shortforge-data:/data $(IMAGE)

run-container:   ## one-shot pipeline run in a container; video lands in ./output
	mkdir -p output
	$(ENGINE) run --rm -it --env-file $(if $(wildcard .env),.env,/dev/null) \
	  $(if $(filter podman,$(notdir $(ENGINE))),--userns=keep-id:uid=10001$(comma)gid=10001,) \
	  -v "$(CURDIR)/output:/data/output$(SELINUX)" $(IMAGE) shortforge run "$(TOPIC)"

clean:
	rm -rf .shortforge output .pytest_cache .ruff_cache

comma := ,
