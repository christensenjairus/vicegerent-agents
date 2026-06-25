IMAGE := harbor.hahomelabs.com/vicegerent/agentgateway
# Tag matches the upstream version it is built from, so the docker datasource on
# the gateway image ref (apps/.../gateway.yaml) tracks it as clean semver and
# Renovate auto-detects the next build. The registry path (vicegerent vs
# ghcr.io/agentgateway) is what marks this as the patched build.
AGW_VERSION ?= v1.3.1
TAG         ?= $(AGW_VERSION)
REF         := $(IMAGE):$(TAG)

DOCKER    ?= docker
# Set PLATFORM (e.g. linux/arm64) to cross-build; empty = native.
PLATFORM  ?=
PLAT_FLAG := $(if $(PLATFORM),--platform $(PLATFORM),)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: image
image: ## Build the container image ($(REF))
	$(DOCKER) build $(PLAT_FLAG) --build-arg AGW_VERSION=$(AGW_VERSION) -t $(REF) .

.PHONY: push
push: ## Push the image ($(REF)) to Harbor
	$(DOCKER) push $(REF)

.PHONY: release
release: image push ## Build image, push (override AGW_VERSION=... / TAG=...)
