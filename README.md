# mongohack: Airlock control plane

Airlock plans a coding request, has a human approve the contract, and runs each assignment as an isolated worker while the parent Claude Code session coordinates. The design is in [airlock-evolve-architecture.md](airlock-evolve-architecture.md), the terminology in [CONTEXT.md](CONTEXT.md), and the decisions in [docs/adr](docs/adr).

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.example .env   # then set AIRLOCK_MONGO_URI
```

The tests and the API use the shared Atlas cluster. Get `AIRLOCK_MONGO_URI` from the team privately, and make sure your IP is on the cluster's Network Access list.

## Tests

```sh
uv run --env-file .env pytest                     # everything
uv run --env-file .env pytest tests/test_e2e.py   # control-plane end-to-end
```

Each test session creates a throwaway `airlock_test_<uuid>` database and drops it afterwards. Without `AIRLOCK_MONGO_URI`, the DB-backed tests are **skipped, not failed**, so check the summary for `skipped`.

## Running live

Fill in the tokens and `AIRLOCK_ROOTS` in `.env`, then:

```sh
uv run --env-file .env python -m mongo.setup              # create collections, validators, and indexes
uv run --env-file .env uvicorn api.main:app               # API on 127.0.0.1:8000
```

In another shell, load the plugin (hooks plus the MCP bridge) into Claude Code with the same `.env` exported:

```sh
set -a; source .env; set +a
claude --plugin-dir ./plugin
```

Then run `/airlock:run <project_id> <base_artifact_id> <request>` in that session, and approve the planned contract from the prompt with `! uv run airlock approve <task_id>`. See [ADR 0008](docs/adr/0008-plugin-and-mcp.md) for how the hooks gate the session.
