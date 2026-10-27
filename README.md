# Product Analytics Backend

FastAPI backend for a self-hosted product analytics platform. It provides authenticated event ingestion, PostgreSQL-backed analytics queries, organizations and projects, saved reports and dashboards, cohorts, alerts, data exports, background jobs, and API documentation.

## Key features

### Event ingestion
- Single-event and batch ingestion authenticated with project API keys.
- Event validation, normalization, duplicate handling, and identity resolution.
- Support for identified and anonymous users, event and person properties, sessions, and event definitions.
- Configurable request, batch, property, and rate limits.

### Analytics
- Trends with filters, breakdowns, comparisons, and configurable intervals.
- Funnels with conversion windows and breakdowns.
- Retention analysis.
- Session analytics and paginated session data.
- Event and page path analysis.
- Cohort definitions and membership queries.
- Dashboard overview data and query result caching.

### Application features
- Registration, login, logout, password changes, password reset, and email verification.
- Organizations, memberships, invitations, projects, roles, and project API keys.
- Reports, dashboards, cohorts, alerts, notifications, and data exports.
- Synchronous CSV/JSON exports and asynchronous export jobs processed by the worker.

## Tech stack and architecture

- Python 3.12
- FastAPI and Uvicorn
- Pydantic and Pydantic Settings
- SQLAlchemy asyncio with PostgreSQL via `asyncpg`
- Alembic for database migrations
- Redis for rate limiting and analytics result caching
- Prometheus client instrumentation for request and ingestion metrics
- Argon2 password hashing
- HTTP-only session cookies with CSRF protection for dashboard authentication
- HMAC-SHA256 hashing for stored project API keys
- Role-based access control with `owner`, `admin`, `member`, and `viewer` roles
- Background worker processes for exports, alert evaluation, cohort refreshes, retention processing, usage handling, and cleanup tasks
- Security middleware for request IDs, body-size limits, security headers, CORS, and API cache control

The analytics layer uses typed query models and a repository abstraction over PostgreSQL. Database changes are managed through Alembic.

## Companion repository

Frontend: https://github.com/faizansaiyed123/product_analytics_frontend

## Quick start

### Prerequisites

- Python 3.12
- PostgreSQL
- Redis

The CI workflow uses PostgreSQL 16 and Redis 7.

### Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Configure the required application environment, then apply the database migrations:

```bash
alembic upgrade head
```

Start the API:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

FastAPI exposes the interactive API documentation at `/docs` and `/redoc`.

### Docker

Build the backend image with:

```bash
docker build -t product-analytics-backend .
```

The Docker image runs the Alembic migrations before starting Uvicorn. PostgreSQL and Redis are external services and must be provided through the application environment.

## Environment variables

The repository does not contain a committed `.env.example`. The application settings are defined in `app/core/config.py`; the Dockerfile also supports `WORKERS`.

```text
ENVIRONMENT
DATABASE_URL
REDIS_URL
JWT_SECRET
API_KEY_SECRET
CORS_ORIGINS
LOG_LEVEL
LOG_JSON
COOKIE_SECURE
SESSION_TTL_DAYS
ALLOW_REGISTRATION
PUBLIC_APP_URL
EMAIL_VERIFICATION_TTL_HOURS
PASSWORD_RESET_TTL_MINUTES
SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
SMTP_FROM
MAX_BODY_BYTES
MAX_BATCH_SIZE
MAX_EVENT_NAME_LENGTH
MAX_PROPERTIES_PER_EVENT
MAX_PROPERTY_VALUE_BYTES
DEFAULT_RATE_LIMIT_PER_SECOND
AUTH_RATE_LIMIT_PER_MINUTE
TRUST_PROXY_HEADERS
COUNTRY_HEADER
QUERY_CACHE_TTL_SECONDS
MAX_QUERY_RANGE_DAYS
QUERY_STATEMENT_TIMEOUT_MS
DB_POOL_SIZE
DB_MAX_OVERFLOW
DB_POOL_TIMEOUT
DB_POOL_RECYCLE
UVICORN_WORKERS
EXPORT_DIR
EXPORT_TTL_HOURS
WEBHOOK_ALLOW_PRIVATE
METRICS_TOKEN
DEMO_EMAIL
DEMO_PASSWORD
WORKERS
```

## Tests

Run the backend test suite with:

```bash
python -m pytest -q
```

The test suite covers authentication, organizations and projects, event ingestion, rate limiting, trends, funnels, retention, sessions, cohorts, dashboards, reports, alerts, notifications, and export-related behavior.

The repository's CI workflow runs the tests against PostgreSQL and Redis and applies the real Alembic migrations first: [backend-tests.yml](./.github/workflows/backend-tests.yml).

## Project structure

```text
app/
├── analytics/        Typed analytics queries and PostgreSQL analytics implementation
├── api/v1/            FastAPI API routes
├── core/              Configuration, security, middleware, rate limiting, Redis, logging
├── db/                SQLAlchemy database setup
├── models/            SQLAlchemy models
├── repositories/      Repository package
├── schemas/           Pydantic request and response schemas
├── services/          Authentication, ingestion, analytics, alerts, exports, projects, and audit services
└── workers/           Background worker and task processing

migrations/            Alembic migration environment and database revisions
scripts/               Development data seeding
tests/                 Pytest suite
Dockerfile             Container image definition
alembic.ini            Alembic configuration
pyproject.toml         Ruff, pytest, and mypy configuration
requirements.txt       Runtime dependencies
requirements-dev.txt   Development and test dependencies
.github/workflows/     GitHub Actions CI
```
