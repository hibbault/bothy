# Bothy — common tasks. Running `make` with no arguments checks everything.

GO   ?= go
BIN  := bin/bothy
DIST := dist

# Platforms that get release binaries. Keep in step with
# .github/workflows/release.yml.
PLATFORMS := linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS := -s -w -X main.version=$(VERSION)

.DEFAULT_GOAL := check
.PHONY: help build dist test vet fmt fmt-check check cover devnet real mismatch down clean swarm swarm-check

help:
	@echo "build     compile the binary to $(BIN)"
	@echo "dist      cross-compile release binaries into $(DIST)/"
	@echo "check     gofmt check, vet and test (the default)"
	@echo "devnet    the whole stack in containers, mock engine, no GPU"
	@echo "real      the devnet with a real Ollama instead (needs a GPU)"
	@echo "mismatch  devnet plus a second host serving different weights"
	@echo "cover     tests with a coverage summary"
	@echo "fmt       rewrite files with gofmt"
	@echo "clean     remove build output"
	@echo "swarm     build the experimental task runner (opt-in, not in releases)"
	@echo "swarm-check  gofmt, vet and test it, tagged (not part of check)"

build:
	$(GO) build -ldflags '$(LDFLAGS)' -o $(BIN) ./cmd/bothy

# The same artifacts the release workflow attaches to a tag, so that "install is
# one binary" can be checked locally before it is promised to anyone.
dist:
	@rm -rf $(DIST)
	@mkdir -p $(DIST)
	@set -e; for p in $(PLATFORMS); do \
		os=$${p%/*}; arch=$${p#*/}; \
		out=$(DIST)/bothy-$$os-$$arch; \
		if [ "$$os" = windows ]; then out=$$out.exe; fi; \
		echo "  $$out"; \
		GOOS=$$os GOARCH=$$arch CGO_ENABLED=0 \
			$(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $$out ./cmd/bothy; \
	done
	@cd $(DIST) && rm -f SHA256SUMS && \
		(sha256sum bothy-* 2>/dev/null || shasum -a 256 bothy-*) > SHA256SUMS
	@echo "  $(DIST)/SHA256SUMS"

# The swarm is a plugin, not a feature: it lives behind `-tags swarm`, nothing
# from it is in the default build, and it is deliberately absent from `check` and
# from every release artifact. See docs/swarm.md. Building it is a separate,
# explicit act, which is what keeps the promise.
swarm:
	$(GO) build -tags swarm -ldflags '$(LDFLAGS)' -o $(BIN)-swarm ./cmd/bothy

swarm-check: fmt-check
	$(GO) vet -tags swarm ./...
	$(GO) test -tags swarm ./internal/swarm/...

test:
	$(GO) test ./...

vet:
	$(GO) vet ./...

fmt:
	gofmt -w .

fmt-check:
	@unformatted="$$(gofmt -l .)"; \
	if [ -n "$$unformatted" ]; then \
		echo "these files are not gofmt'd:"; \
		echo "$$unformatted"; \
		exit 1; \
	fi

check: fmt-check vet test

cover:
	$(GO) test -cover ./...

# The profile is set explicitly here rather than read from .env, so that a fresh
# clone behaves the same whether or not anyone copied .env.example across.
devnet:
	COMPOSE_PROFILES=mock docker compose up --build

real:
	COMPOSE_PROFILES=real docker compose up --build

mismatch:
	COMPOSE_PROFILES=mock,mismatch docker compose up --build

down:
	docker compose down

clean:
	rm -rf bin $(DIST)
