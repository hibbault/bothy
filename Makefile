# Bothy — common tasks. Running `make` with no arguments checks everything.

PY  := python
PKG := bothy

.DEFAULT_GOAL := check
.PHONY: help check test compile e2e devnet real mismatch down clean

help:
	@echo "check     byte-compile every module, then run the tests (the default)"
	@echo "test      run the test suite"
	@echo "compile   byte-compile every module and test, which is the closest"
	@echo "          thing Python has to a build step"
	@echo "e2e       the whole stack over real HTTP: mock engine, registry,"
	@echo "          two hosts, a client -- what CI runs, runnable here"
	@echo "devnet    the same stack in containers, mock engine, no GPU"
	@echo "real      the devnet with a real Ollama instead (needs a GPU)"
	@echo "mismatch  devnet plus a second host serving different weights"
	@echo "down      stop the devnet"
	@echo "clean     remove bytecode caches"

# There is nothing to build. That is the point of the port as much as it was of
# the Go binary: the tree runs from a checkout, so this is not a build so much as
# a check that every module at least parses before the tests try to import them.
compile:
	$(PY) -m compileall -q $(PKG) tests

test:
	$(PY) -m unittest discover -s tests

# The one thing a laptop cannot check by reading: that real processes on real
# ports still talk to each other. It is a script rather than a block of YAML so
# that CI runs exactly what a person can run, and so that the two cannot drift.
e2e:
	sh scripts/e2e.sh

check: compile test

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
	rm -rf $(PKG)/__pycache__ tests/__pycache__
