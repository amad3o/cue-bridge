# One-command development setup. Run `make` to see what is here.
VENV := backend/.venv
PY := $(VENV)/bin/python

.PHONY: help setup setup-db test lint check agent

help:
	@echo "make setup     create backend/.venv and install the dependencies and dev tools"
	@echo "make setup-db  also install the optional Rekordbox database packages (needs: brew install sqlcipher)"
	@echo "make test      run the tests"
	@echo "make lint      run ruff"
	@echo "make check     lint, then test (what CI runs)"
	@echo "make agent     chat with CueBridge's own agent (needs OPENAI_API_KEY)"

$(PY):
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

setup: $(PY)
	$(PY) -m pip install -r backend/requirements-dev.txt

setup-db: setup
	CFLAGS="-I$$(brew --prefix sqlcipher)/include -I$$(brew --prefix sqlcipher)/include/sqlcipher" \
	LDFLAGS="-L$$(brew --prefix sqlcipher)/lib" SQLCIPHER_PATH="$$(brew --prefix sqlcipher)" \
	$(PY) -m pip install -r backend/requirements-rekordbox-db.txt

test: $(PY)
	cd backend && .venv/bin/python -m pytest -q

lint: $(PY)
	cd backend && .venv/bin/ruff check cuebridge tests

check: lint test

agent: $(PY)
	cd backend && .venv/bin/python -m cuebridge.agent
