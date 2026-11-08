SHELL := bash

.DEFAULT_GOAL := help
.PHONY: help sync fmt fmt-check lint lint-check type test docs docs-serve man check ci build install install-completions install-man uninstall upgrade clean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

sync:  ## Install or update dependencies
	uv sync --all-groups

fmt:  ## Format code
	uv run ruff format .

fmt-check:  ## Check formatting without changing files
	uv run ruff format --check .

lint:  ## Lint and apply safe fixes
	uv run ruff check --fix .

lint-check:  ## Lint without changing files
	uv run ruff check .

type:  ## Type-check the project
	uv run ty check

test:  ## Run tests
	uv run pytest

docs:  ## Build HTML documentation
	uv run sphinx-build -W -b html docs docs/_build/html

docs-serve:  ## Serve documentation with live reload
	uv run sphinx-autobuild docs docs/_build/html

man:  ## Build the man page
	uv run sphinx-build -W -b man docs docs/_build/man

check: fmt-check lint-check type test docs  ## Run all checks without changing files

ci:  ## Install locked dependencies and run all checks
	uv sync --locked --all-groups
	$(MAKE) check

build:  ## Build the wheel and sdist
	uv build

install:  ## Install the app, bash completions, and man page
	uv tool install --editable --force .
	$(MAKE) install-completions install-man

install-completions:  ## Generate and install bash completions
	uv run tesla --completions

MANPAGE := docs/_build/man/tesla.1
MANDIR ?= $(if $(XDG_DATA_HOME),$(XDG_DATA_HOME),$(HOME)/.local/share)/man/man1

install-man: man  ## Install the man page
	mkdir -p "$(MANDIR)"
	install -m 644 "$(MANPAGE)" "$(MANDIR)/tesla.1"

uninstall:  ## Remove the installed tool
	uv tool uninstall tesla

upgrade:  ## Upgrade dependencies and the lockfile
	uv sync --upgrade --all-groups

clean:  ## Remove build and test artifacts
	rm -rf build dist docs/_build .pytest_cache .ruff_cache .ty_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
