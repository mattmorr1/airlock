# target-app (MongoDB items app under test)

Purpose-built FastAPI + MongoDB app: users own items via `owner_id`. It is the
repository-under-test for the Airlock demo — Airlock delegates building the
atomic bulk-update feature on top of this baseline.

- `app/routes/items.py` — single-item CRUD (`GET/POST/GET/PATCH /api/v1/items`)
- `app/routes/auth.py` — `POST /api/v1/login/access-token`
- `app/auth.py` — HMAC bearer token, `owner_id` authorization boundary
- `app/db.py` — its OWN MongoDB connection + seed (Alice, Bob, items)

**The baseline deliberately has no `PATCH /api/v1/items/bulk`.** Workers A/B/C
add it:
- A: idempotency collection + unique index + validator
- B: transactional `bulk_update` service
- C: endpoint, validation, response

The demo bug: a flawed bulk implementation updates Alice's item, then hits
Bob's item and returns 403 — but Alice's row is already changed. A status-only
test passes; the database is corrupted. Airlock's `rows_unchanged_on_rejection`
invariant catches it.

Env:
- `TARGET_MONGO_URI` (default local replica set), `TARGET_DB` (default `target_app`)
- `TARGET_SECRET` (token signing), `TARGET_SEED_PREFIX` (fresh item ids per run)

Seeded users: `alice@example.com`/`alice-pw`, `bob@example.com`/`bob-pw`.

This app uses its **own** MongoDB, separate from Airlock's control plane; it
never receives Airlock credentials.
