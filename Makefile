.PHONY: setup lint test check release-check package clean

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
VENV_PYTHON ?= python3

setup:
	"$(VENV_PYTHON)" -m venv .venv
	.venv/bin/python -m pip install -e .

lint:
	"$(PYTHON)" -m scripts.check_project

test:
	"$(PYTHON)" -m unittest discover -s tests

check: lint test

release-check:
	"$(PYTHON)" -m scripts.release_version check

package: clean
	"$(PYTHON)" -m pip wheel --no-deps --wheel-dir dist .

clean:
	"$(PYTHON)" -m scripts.clean_build
