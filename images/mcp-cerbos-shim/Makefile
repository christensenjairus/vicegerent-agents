IMAGE := harbor.hahomelabs.com/vicegerent/mcp-cerbos-shim
TAG   ?= v0.12.0
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
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags="-s -w" -o bin/mcp-cerbos-shim .

.PHONY: image
image: ## Build the container image ($(REF))
	$(DOCKER) build $(PLAT_FLAG) -t $(REF) .

.PHONY: push
push: ## Push the image ($(REF)) to Harbor
	$(DOCKER) push $(REF)

.PHONY: release
release: check image push ## check, build image, push (override TAG=...)

.PHONY: proto
proto: ## Regenerate gRPC stubs (only when proto/ext_mcp.proto changes)
	@command -v protoc-gen-go >/dev/null      || { echo "missing protoc-gen-go: go install google.golang.org/protobuf/cmd/protoc-gen-go@latest"; exit 1; }
	@command -v protoc-gen-go-grpc >/dev/null || { echo "missing protoc-gen-go-grpc: go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest"; exit 1; }
	protoc --proto_path=proto \
		--go_out=proto/gen --go_opt=paths=source_relative \
		--go-grpc_out=proto/gen --go-grpc_opt=paths=source_relative \
		proto/ext_mcp.proto

.PHONY: clean
clean: ## Remove build artifacts
	rm -rf bin
