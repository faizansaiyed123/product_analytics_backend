# Demo branch

This branch contains a standalone in-memory simulator of the Product Analytics API.

The production application on `main` still uses PostgreSQL, Redis, real authentication/session security, ingestion, exports, and workers. The `demo` backend intentionally avoids those dependencies.

Run the simulator with:

```bash
uvicorn app.demo.main:app --host 0.0.0.0 --port 8000
```

The frontend `demo` branch is self-contained and does not require this backend. This backend exists so the same API contract can also be demonstrated or tested separately.

Seeded demo behavior covers authentication, organizations and projects, API keys, event/person exploration, analytics queries, cohorts, reports, dashboards, alerts, notifications, usage, and exports. Mutations are stored in process for the lifetime of the demo server.