.PHONY: up down seed logs reset test
up:
	docker compose up -d --build
seed:
	python3 scripts/seed.py
logs:
	docker compose logs -f backend frontend
down:
	docker compose down
reset:
	docker compose down -v
	docker compose up -d --build postgres minio backend frontend
	sleep 6
	DATABASE_URL=postgresql+psycopg://coldlineage:coldlineage@localhost:5433/coldlineage python3 scripts/seed.py
test:
	python3 scripts/smoke_test.py
