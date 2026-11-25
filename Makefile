PYTHON ?= python3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup rtg tools test test-all lint lint-python contract results challengers services stop demo quickstart clean

setup:  ## create the virtualenv, install Python extras and fetch the separate exchange dependency
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet -e ".[all]"
	$(BIN)/python scripts/bootstrap_rtg.py

rtg:  ## fetch and verify the separately licensed Ready Trader Go dependency
	$(PYTHON) scripts/bootstrap_rtg.py

quickstart:  ## run the local surveillance example; needs setup, no services or credentials
	$(BIN)/vigil simulate --session 2 --scenario rogues --match-id quickstart --no-regulatory

tools:  ## fetch Kafka and Foundry into .tools/ (checksums verified)
	scripts/bootstrap_tools.sh

test:  ## the suite; tests that need Kafka, anvil, the network or the real exchange skip if unavailable
	$(BIN)/pytest

test-all: services  ## everything, including the public APIs and a match on the real exchange
	VIGIL_TEST_NETWORK=1 VIGIL_TEST_LIVE=1 $(BIN)/pytest
	cd contracts && ../.tools/foundry/forge test

lint-python:
	$(BIN)/ruff check src tests scripts tools

lint: lint-python
	cd contracts && ../.tools/foundry/forge fmt --check && ../.tools/foundry/forge lint

contract:  ## run the Solidity tests and refresh the ABI and bytecode shipped in the package
	cd contracts && ../.tools/foundry/forge test
	$(BIN)/vigil chain build

results:  ## re-measure everything reported in docs/RESULTS.md and regenerate it
	$(BIN)/vigil train
	$(BIN)/vigil backtest
	$(BIN)/vigil calibrate
	$(BIN)/python scripts/render_docs.py

challengers:  ## fit the experimental versions of Vigil and measure them against base Vigil (docs/RESULTS.md)
	$(BIN)/vigil challenge fit
	$(BIN)/vigil challenge run
	$(BIN)/python scripts/render_docs.py

services:  ## start the local Kafka broker and Ethereum node
	scripts/kafka_local.sh start
	scripts/anvil_local.sh start

stop:
	-scripts/kafka_local.sh stop
	-scripts/anvil_local.sh stop

demo: services  ## the whole pipeline end to end (about three minutes)
	scripts/demo.sh

clean:  ## remove generated reports, runs and caches (keeps the toolchains and the virtualenv)
	rm -rf artifacts .pytest_cache .ruff_cache contracts/out contracts/cache

setup-locked:  ## use the release dependency lock (requires uv)
	uv sync --frozen --extra all
	$(BIN)/python scripts/bootstrap_rtg.py

faults:  ## isolated real-broker fault tests (requires tools and Java)
	VIGIL_TEST_KAFKA_FAULTS=1 $(BIN)/pytest tests/test_kafka_faults.py

reproduce:  ## verify the captured-match release fingerprint
	$(BIN)/vigil reproduce --check docs/results/reproduction.json
