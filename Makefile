.PHONY: help demo setup test compile check preflight graph-smoke graph-validate graph-validate-core graph-run-core replay-check cpp-smoke graph-example run-example replay clean-logs clean-venv

VENV_DIR ?= .venv
PYTHON ?= python3
PIP := $(VENV_DIR)/bin/pip
PYTEST := $(VENV_DIR)/bin/pytest
ROBOT_CORE := $(VENV_DIR)/bin/robot-core
# Prefer venv interpreter when present so scripts can import robot_core.
RUN_PYTHON := $(if $(wildcard $(VENV_DIR)/bin/python),$(VENV_DIR)/bin/python,$(PYTHON))

help:
	@echo "NervLynx developer shortcuts"
	@echo ""
	@echo "Targets:"
	@echo "  make demo       Create venv, install deps, run demo + replay"
	@echo "  make setup      Create venv and install project in dev mode"
	@echo "  make test       Run pytest (implies setup if venv missing)"
	@echo "  make check      Run test + compile (common pre-push gate)"
	@echo "  make preflight  Run graph-validate-core + replay-check + check"
	@echo "  make graph-smoke Run graph-validate-core + graph-run-core"
	@echo "  make graph-validate Validate surveillance graph config and plugins"
	@echo "  make graph-validate-core Validate bundled core example robot packs"
	@echo "  make graph-run-core Run all core packs and write logs/*_trace.jsonl"
	@echo "  make replay-check Run deterministic replay fixture check"
	@echo "  make compile    Byte-compile robot_core and shuttle (syntax check)"
	@echo "  make cpp-smoke  Configure, build, and run C++ smoke_surveillance"
	@echo "  make graph-example Run surveillance example graph (implies setup if venv missing)"
	@echo "  make run-example Run the basic runtime demo"
	@echo "  make replay     Replay the latest demo trace"
	@echo "  make clean-venv Remove local virtual environment"

demo: setup run-example replay

test:
	@if [ ! -x "$(PYTEST)" ]; then $(MAKE) setup; fi
	$(PYTEST) -q

compile:
	$(PYTHON) -m compileall -q robot_core shuttle

check: test compile

preflight: graph-validate-core replay-check check

graph-smoke: graph-validate-core graph-run-core

graph-validate:
	@if [ ! -x "$(ROBOT_CORE)" ]; then $(MAKE) setup; fi
	$(ROBOT_CORE) graph-validate examples/robot_packs/surveillance.yaml

graph-validate-core:
	@if [ ! -x "$(ROBOT_CORE)" ]; then $(MAKE) setup; fi
	$(ROBOT_CORE) graph-validate-core

graph-run-core:
	@if [ ! -x "$(ROBOT_CORE)" ]; then $(MAKE) setup; fi
	$(ROBOT_CORE) graph-run-core --output-dir logs

replay-check:
	@if [ ! -x "$(VENV_DIR)/bin/python" ]; then $(MAKE) setup; fi
	$(RUN_PYTHON) benchmarks/check_deterministic_replay.py \
	  --seed tests/fixtures/replay/seed_trace.jsonl \
	  --expected tests/fixtures/replay/expected_trace.jsonl \
	  --output logs/replay_actual_trace.jsonl

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

clean-logs:
	rm -f logs/*.jsonl logs/**/*.jsonl 2>/dev/null || true

clean-venv:
	rm -rf $(VENV_DIR)
