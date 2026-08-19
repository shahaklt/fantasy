.PHONY: help setup setup-gpu update run refresh build validate calibrate test lint clean

PY ?= python3

help:
	@echo "make setup       install dependencies (CPU)"
	@echo "make setup-gpu   install dependencies with CuPy CUDA (RTX 3060 Ti)"
	@echo "make update      pull the latest code, keeping your downloaded data"
	@echo "make run         start the web app at http://127.0.0.1:8000"
	@echo "make refresh     pull the latest data"
	@echo "make build       rebuild projections and simulations"
	@echo "make validate    check the model against historical seasons"
	@echo "make calibrate   refit simulation constants from data"
	@echo "make test        run the test suite"

setup:
	$(PY) -m pip install -e ".[dev]"

setup-gpu: setup
	$(PY) -m pip install cupy-cuda12x

update:
	./update.sh

run:
	$(PY) -m gridiron.cli serve

refresh:
	$(PY) -m gridiron.cli refresh

build:
	$(PY) -m gridiron.cli build --sims 20000

validate:
	$(PY) scripts/validate_totals.py 3000

calibrate:
	$(PY) scripts/calibrate.py

test:
	$(PY) -m pytest tests/ -q

clean:
	rm -rf data/cache/* data/artifacts/*
