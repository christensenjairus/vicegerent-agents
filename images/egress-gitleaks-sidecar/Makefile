IMAGE := harbor.hahomelabs.com/vicegerent/egress-gitleaks-sidecar
TAG   ?= v0.1.0
REF   := $(IMAGE):$(TAG)

GO        ?= go
DOCKER    ?= docker
# Set PLATFORM (e.g. linux/arm64) to cross-build; empty = native.
PLATFORM  ?=
PLAT_FLAG := $(if $(PLATFORM),--platform $(PLATFORM),)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: fmt
fmt: ## Format Go code
	$(GO) fmt ./...

.PHONY: fmt-check
fmt-check: ## Fail if any file needs gofmt (CI parity)
	@test -z "$$(gofmt -l .)" || { echo "gofmt needed:"; gofmt -l .; exit 1; }

.PHONY: vet
vet: ## Run go vet
	$(GO) vet ./...

.PHONY: test
test: ## Run the full test suite
	$(GO) test ./...

.PHONY: check
check: fmt-check vet test ## fmt-check + vet + test (what CI runs)

.PHONY: build
build: ## Build the binary locally to ./bin
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags="-s -w" -o bin/egress-gitleaks-sidecar .

.PHONY: image
image: ## Build the container image ($(REF))
	$(DOCKER) build $(PLAT_FLAG) -t $(REF) .

.PHONY: push
push: ## Push the image ($(REF)) to Harbor
	$(DOCKER) push $(REF)

.PHONY: release
release: check image push ## check, build image, push (override TAG=...)

.PHONY: clean
clean: ## Remove build artifacts
	rm -rf bin
