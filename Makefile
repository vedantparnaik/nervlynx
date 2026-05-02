.PHONY: help demo setup test cpp-smoke graph-example run-example replay clean-venv

VENV_DIR ?= .venv
PYTHON ?= python3
PIP := $(VENV_DIR)/bin/pip
PYTEST := $(VENV_DIR)/bin/pytest
ROBOT_CORE := $(VENV_DIR)/bin/robot-core

help:
	@echo "NervLynx developer shortcuts"
	@echo ""
	@echo "Targets:"
	@echo "  make demo       Create venv, install deps, run demo + replay"
	@echo "  make setup      Create venv and install project in dev mode"
	@echo "  make test       Run pytest (implies setup if venv missing)"
	@echo "  make cpp-smoke  Configure, build, and run C++ smoke_surveillance"
	@echo "  make graph-example Run surveillance example graph (implies setup if venv missing)"
	@echo "  make run-example Run the basic runtime demo"
	@echo "  make replay     Replay the latest demo trace"
	@echo "  make clean-venv Remove local virtual environment"

demo: setup run-example replay

test:
	@if [ ! -x "$(PYTEST)" ]; then $(MAKE) setup; fi
	$(PYTEST) -q

cpp-smoke:
	cmake -S cpp_core -B cpp_core/build
	cmake --build cpp_core/build
	./cpp_core/build/smoke_surveillance

graph-example:
	mkdir -p logs
	@if [ ! -x "$(ROBOT_CORE)" ]; then $(MAKE) setup; fi
	$(ROBOT_CORE) run-graph examples/robot_packs/surveillance.yaml --output logs/graph_example_trace.jsonl

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
