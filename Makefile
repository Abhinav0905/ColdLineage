# ColdLineage
#
#   make demo        one command: stack up, warehouse seeded, DataHub populated, smoke test
#   make up          start postgres, minio, backend, frontend
#   make seed        (re)build the synthetic warehouse in Postgres
#   make datahub     push the estate, lineage, consumer SQL and policy into DataHub
#   make examples    regenerate examples/ (artifacts + cassettes) from a real run
#   make test        unit tests + the consumer-SQL oracle self-check
#   make smoke       end-to-end assertions against a running API
#
# PORTS. DataHub is NOT part of this compose file -- it is a real external system,
# started with `datahub docker quickstart`. On a machine where 8080 and 3000 are already
# taken, republish GMS on 8090 and run the UI on 3100:
#
#   DATAHUB_GMS_URL=http://host.docker.internal:8090 UI_PORT=3100 make up
#
# or put those two lines in .env, which docker compose reads automatically. THE FRONTEND
# MUST BE ON 3100 on this machine -- 3000 is occupied. Everything below assumes the UI at
# http://localhost:3100, the API at http://localhost:8000 and GMS at http://localhost:8090.

PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
PG_DSN ?= postgresql://coldlineage:coldlineage@localhost:5433/coldlineage
GMS    ?= http://localhost:8090
API    ?= http://localhost:8000/api
UI_PORT ?= 3100

.PHONY: up down logs reset seed datahub ingest examples test smoke demo

# -- stack -----------------------------------------------------------------

up:
	UI_PORT=$(UI_PORT) docker compose up -d --build
	@echo "  API  http://localhost:8000/docs"
	@echo "  UI   http://localhost:$(UI_PORT)"

down:
	docker compose down

logs:
	docker compose logs -f backend frontend

reset:
	docker compose down -v
	UI_PORT=$(UI_PORT) docker compose up -d --build postgres minio backend frontend
	sleep 6
	$(PYTHON) scripts/seed_warehouse.py --dsn "$(PG_DSN)"

# -- data ------------------------------------------------------------------

# Synthetic rows, measured sizes. Idempotent: every table is dropped and rebuilt.
seed:
	$(PYTHON) scripts/seed_warehouse.py --dsn "$(PG_DSN)"

# Structured property definitions, the native Postgres connector, then lineage,
# consumer SQL, usage and policy values. Safe to re-run; every write is an upsert.
datahub:
	DATAHUB_GMS_URL=$(GMS) COLDLINEAGE_PG_DSN="$(PG_DSN)" PYTHON=$(PYTHON) \
	  ./scripts/bootstrap_datahub.sh

# Just the ColdLineage layer (lineage, Query entities, usage, policy) without
# re-running the connector.
ingest:
	DATAHUB_GMS_URL=$(GMS) COLDLINEAGE_PG_DSN="$(PG_DSN)" \
	  $(PYTHON) scripts/ingest_datahub.py

# -- artifacts -------------------------------------------------------------

# Reseeds, resets run state, records cassettes, drives the live API and writes
# every file under examples/. Needs Postgres, MinIO and a live GMS.
examples:
	$(PYTHON) scripts/record_examples.py --dsn "$(PG_DSN)" --gms $(GMS)

# -- tests -----------------------------------------------------------------

# Offline. Neither of these needs a database, an API or DataHub:
#   test_window_extraction.py -- the SQL window extractor against 20 hand-checked cases
#   consumers.py              -- every consumer statement parses as PostgreSQL and its
#                                declared expectation is internally consistent
test:
	$(PYTHON) backend/tests/test_window_extraction.py
	$(PYTHON) scripts/consumers.py

# End-to-end, against a running API. Takes its expectations from scripts/consumers.py,
# which the backend never reads -- so a match proves the window really was parsed out
# of DataHub. --no-execute keeps it read-only.
smoke:
	$(PYTHON) scripts/smoke_test.py --base $(API)

# -- the whole thing -------------------------------------------------------

demo: up seed ingest
	@echo "waiting for the API..."
	@until curl -sf $(API)/health > /dev/null; do sleep 2; done
	$(PYTHON) scripts/smoke_test.py --base $(API) --no-execute
	@echo
	@echo "Open http://localhost:$(UI_PORT)"
