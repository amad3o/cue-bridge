# One-command development setup. Run `make` to see what is here.
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help setup setup-db setup-ui test test-ui lint check agent eval-beatgrid

help:
	@echo "make setup     create .venv and install the dependencies and dev tools"
	@echo "make setup-db  also install the optional Rekordbox database packages (needs: brew install sqlcipher)"
	@echo "make setup-ui  install the terminal screen's Node packages (needs Node 20+)"
	@echo "make test      run the Python tests"
	@echo "make test-ui   typecheck and test the terminal screen"
	@echo "make lint      run ruff"
	@echo "make check     lint, then both test suites (what CI runs)"
	@echo "make eval-beatgrid ARGS=\"<music folder> --limit 100\"  score the beatgrid analyzer against Serato's grids"
	@echo "make agent     chat with CueBridge's own agent (needs OPENAI_API_KEY)"

$(PY):
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

setup: $(PY)
	$(PY) -m pip install -e ".[dev]"
	# keep the virtualenv out of Dropbox: it is large, regenerable and slow to sync
	xattr -w com.dropbox.ignored 1 $(VENV) 2>/dev/null || true

setup-db: setup
	CFLAGS="-I$$(brew --prefix sqlcipher)/include -I$$(brew --prefix sqlcipher)/include/sqlcipher" \
	LDFLAGS="-L$$(brew --prefix sqlcipher)/lib" SQLCIPHER_PATH="$$(brew --prefix sqlcipher)" \
	$(PY) -m pip install -e ".[rekordbox-db]"

setup-ui:
	cd src/ui && npm install
	# keep node_modules out of Dropbox: it is large, regenerable and slow to sync
	xattr -w com.dropbox.ignored 1 src/ui/node_modules 2>/dev/null || true

src/ui/node_modules:
	$(MAKE) setup-ui

test: $(PY)
	.venv/bin/python -m pytest -q

test-ui: src/ui/node_modules
	cd src/ui && npm run typecheck && npm test

lint: $(PY)
	.venv/bin/ruff check src tests

check: lint test test-ui

# The Ink screen starts the Python agent (src/cuebridge/agent/) as a child process.
agent: $(PY) src/ui/node_modules
	cd src/ui && npm start --silent

# Not part of `make check`: it decodes audio, so it is slow. Needs the beatgrid extra and a music folder Serato has analysed.
eval-beatgrid: $(PY)
	$(PY) evals/beatgrid_vs_serato.py $(ARGS)
