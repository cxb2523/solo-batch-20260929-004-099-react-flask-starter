# The React + Flask Template &middot; [![Version Badge](https://img.shields.io/badge/version-1.0.0-brightgreen)](#)

A React starter project with Flask backend that works with [Docker](https://www.docker.com), [Docker Compose](https://docs.docker.com/compose),
and [Shipyard](https://shipyard.build) out of the box.

## Includes

- [React](https://github.com/facebook/react) - JavaScript library for building user interfaces
- [Material-UI](https://github.com/mui-org/material-ui) - React components for faster and simpler web development
- [Flask](https://github.com/pallets/flask) - lightweight WSGI web application framework
- [Jinja](https://github.com/pallets/jinja) + [Bootstrap](https://pythonhosted.org/Flask-Bootstrap) (from CDN)
- [uWSGI](https://github.com/unbit/uwsgi) - entrypoint
- [Celery](https://github.com/celery/celery) (with example heartbeat task configured) - distributed task queue
- [Flask-SQLAlchemy](https://github.com/pallets/flask-sqlalchemy) - ORM toolkit
- [LocalStack](https://github.com/localstack/localstack) - fully functional local AWS cloud stack

## Dependencies

- [Docker](https://www.docker.com) & [Docker Compose](https://docs.docker.com/compose) - to build and run the app
- [Make](https://www.gnu.org/software/make/manual/make.html) - to easily run commands needed for development

## Getting Started

- Run `make develop` at the root of this project.
- Visit the app at http://localhost:3000.
- Visit http://localhost:8080/api/v1/files to list objects in LocalStack s3 bucket.
- Make your code changes! The app will reload whenever you save.

## Async upload pipeline (Celery + LocalStack S3)

Uploads can run in two modes, switched by `UPLOAD_MODE` (default `celery`,
see `backend/src/settings.py`):

- `celery` — `POST /api/v1/files/upload/` enqueues `src.tasks.upload_file`,
  records an `UploadTask` row (`pending -> running -> done/failed`) and
  returns `202` with a `task_id`.
- `sync` — the request uploads inline and returns the S3 key directly.

Watch tasks move through the state machine at http://localhost:8080/status
(auto-refreshes every 2s, shows status, attempts, failure reason and the
resulting S3 key). The same data is available as JSON at
`/api/v1/tasks/` and `/api/v1/tasks/<task_id>`.

### Run it

```sh
cd backend && poetry run pytest -q   # tests, must be green
make up                              # build + start web, worker, migrate, deps
make health                          # compose ps + backend/localstack probes
curl -F "file=@README.md" -H "X-Idempotency-Key: demo-1" \
     http://localhost:8080/api/v1/files/upload/
# open http://localhost:8080/status and watch pending -> running -> done
```

### Design tradeoffs

1. **Who migrates: neither web nor worker — a dedicated one-shot `migrate`
   service.** Both `backend` and `worker` declare
   `depends_on: migrate: service_completed_successfully`, so the schema
   exists before either starts and they never race each other running DDL.
   Re-running a migration is idempotent by construction: Alembic records
   the head revision in the `alembic_version` table, so a second
   `flask db upgrade` (or `make migrate`, or a `migrate` service restart)
   is a no-op. Postgres also executes each migration in a transaction, so
   a crashed migration never leaves a half-created table.

2. **Retries reuse the same S3 key.** The object key is derived from the
   idempotency key (`uploads/<idempotency_key>/<filename>`), not from a
   timestamp, so any number of retries/duplicate deliveries converge on a
   single object; the worker also does a `head_object` first and skips the
   `put_object` when the object already exists. Retry policy:
   `max_retries=3` (4 attempts total) with exponential backoff
   (`retry_backoff=2` → 2s, 4s, 8s, capped at 60s, jitter off for
   deterministic demos). Only after the last retry does the row move to
   `failed` with the exception message; intermediate attempts stay
   `running` so the status never flaps backwards.

3. **LocalStack not ready → immediate 503.** The submit endpoint probes
   S3 with a short-timeout client (1s, no SDK retries) and fails fast with
   `503 localstack is not ready` instead of long-polling. Rationale: the
   caller keeps control of retry timing, the web worker never parks on a
   dead dependency, and no `UploadTask` row is created for work that was
   never accepted — so `/status` only ever shows real, enqueueable tasks.

### Concurrency guarantees

- **One object per idempotency key.** `upload_task.idempotency_key` has a
  unique constraint; a duplicate submission (even racing concurrently)
  gets the existing `task_id` back with `deduplicated: true` and no new
  row or Celery message is created. Combined with the deterministic S3
  key, exactly one object lands in the bucket.
- **No status regression.** `UploadTask.advance()` applies transitions
  with a conditional `UPDATE ... WHERE status IN (<allowed predecessors>)`.
  Terminal states (`done`/`failed`) are never valid predecessors, so under
  concurrent workers a task can only move `pending -> running ->
  done/failed` (with `running -> running` allowed to bump the attempt
  counter on retries) — never backwards.
