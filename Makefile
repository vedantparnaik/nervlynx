.PHONY: help demo setup run-example replay clean-venv

VENV_DIR ?= .venv
PYTHON ?= python3
PIP := $(VENV_DIR)/bin/pip
ROBOT_CORE := $(VENV_DIR)/bin/robot-core

help:
	@echo "NervLynx developer shortcuts"
	@echo ""
	@echo "Targets:"
	@echo "  make demo       Create venv, install deps, run demo + replay"
	@echo "  make setup      Create venv and install project in dev mode"
	@echo "  make run-example Run the basic runtime demo"
	@echo "  make replay     Replay the latest demo trace"
	@echo "  make clean-venv Remove local virtual environment"

demo: setup run-example replay

setup:
	$(PYTHON) -m venv $(VENV_DIR)
	$(PIP) install -U pip
	$(PIP) install -e ".[dev]"

run-example:
	mkdir -p logs
	$(ROBOT_CORE) run-example --output logs/robot_core_trace.jsonl

replay:
	$(ROBOT_CORE) replay logs/robot_core_trace.jsonl

clean-venv:
	rm -rf $(VENV_DIR)
