VOLUME=$(shell basename $(PWD))

develop: clean build migrations.upgrade run

up:
	docker compose up -d --build backend worker

migrate:
	docker compose run --rm migrate

health:
	@docker compose ps
	@curl -fsS http://localhost:8080/healthz && echo " backend ok"
	@curl -fsS http://localhost:4566/_localstack/health > /dev/null && echo " localstack ok"

clean:
	docker compose rm -vf

build:
	docker compose build

run:
	docker compose up

frontend-shell:
	docker compose run frontend \
	  sh

backend-shell:
	docker compose run worker \
	  sh

python-shell:
	docker compose run worker \
	  poetry run flask shell

postgres.data.delete: clean
	docker volume rm $(VOLUME)_postgres

postgres.start:
	docker compose up -d postgres
	docker compose exec postgres \
	  sh -c 'while ! nc -z postgres 5432; do sleep 0.1; done'

migrations.blank: postgres.start
	docker compose run worker \
	  poetry run flask db revision

migrations.create: postgres.start
	docker compose run worker \
	  poetry run flask db migrate

migrations.upgrade: postgres.start
	docker compose run worker \
	  poetry run flask db upgrade

migrations.heads: postgres.start
	docker compose run worker \
	  poetry run flask db heads
