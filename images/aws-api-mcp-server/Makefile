IMAGE := harbor.hahomelabs.com/vicegerent/aws-api-mcp-server
TAG   ?= v0.1.0
REF   := $(IMAGE):$(TAG)

DOCKER    ?= docker
# Set PLATFORM (e.g. linux/arm64) to cross-build; empty = native.
PLATFORM  ?=
PLAT_FLAG := $(if $(PLATFORM),--platform $(PLATFORM),)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: image
image: ## Build the container image ($(REF))
	$(DOCKER) build $(PLAT_FLAG) -t $(REF) .

.PHONY: push
push: ## Push the image ($(REF)) to Harbor
	$(DOCKER) push $(REF)

.PHONY: release
release: image push ## build image, push (override TAG=... / PLATFORM=...)
