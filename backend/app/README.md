# Backend layout

The API package is organized by responsibility:

- `main.py` builds the FastAPI app and starts database initialization through its lifespan.
- `routers/` contains HTTP endpoints, grouped by business area.
- `schemas.py` defines API request and response shapes.
- `models.py` defines SQLAlchemy entities and their relationships.
- `auth.py` contains authentication and authorization helpers.
- `database/connection.py` owns the SQLite path, SQLAlchemy engine, session factory, and request-scoped session dependency.
- `database/migrations.py` owns schema upgrades and one-time catalog migration.
- `database/catalog.py` contains the opening inventory used by the catalog migration.
- `assets/` contains runtime assets such as PDF fonts.

## Request and data flow

The desktop or browser frontend calls FastAPI routes in `routers/`. Each route receives a database session from `database/connection.py`, reads or writes SQLAlchemy models, and returns a schema-validated response. Product creation commits to SQLite before returning; the frontend then refreshes the product list from the API.

On service startup, the app creates missing tables, applies idempotent SQLite schema changes, then records the catalog migration in `app_migrations`. The catalog is not reseeded on later starts, so billing stock changes and user-created products are retained.

By default, SQLite is stored at `backend/adinath.db`. `ADINATH_DB_PATH` can select a different file for tests or deployment.
