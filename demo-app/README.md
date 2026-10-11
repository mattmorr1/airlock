# demo-app (target application under test)

Minimal login/sessions service used as the repository-under-test for Airlock.
It is the *subject* of delegated coding work and behavior validation — not part
of the controller.

- `src/auth/service.py` — `login()` / `create_session()`
- `src/auth/routes.py` — `POST /login`, `GET /healthz`
- `db/migrations/` — `users` and `sessions` schema

Contract: a valid login creates exactly one session; an invalid login creates
no session; no credential material in application logs.

The baseline deliberately contains a planted defect: an unknown-email login
goes down an exception path that logs the raw password. The HTTP response still
looks like an ordinary rejected login, so only log inspection catches it. This
is the escaped failure the harness-evolution loop is built to detect.

Env:
- `APP_DB`  — SQLite file path (default `/tmp/demo-app/auth.db`)
- `APP_LOG` — log file path (default: stderr)

Seeded user: `alice@example.com` / `correct-horse-battery`.
