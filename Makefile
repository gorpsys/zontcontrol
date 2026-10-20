.PHONY: setup lint test check release-check package package-check yandex-archive cloud-package-check clean

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
	"$(PYTHON)" -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .

package-check: package
	"$(PYTHON)" -m tests.test_installed_package --wheel dist/*.whl

yandex-archive:
	"$(PYTHON)" -m scripts.build_yandex_archive

cloud-package-check:
	"$(PYTHON)" -m scripts.check_cloud_package

clean:
	"$(PYTHON)" -m scripts.clean_build
