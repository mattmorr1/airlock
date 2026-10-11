---
status: accepted
---

# Mongo setup: async driver, enum-owned schema contract, stream-keyed events

`mongo/setup.py` creates the eleven collections from [spec §7-8](../../airlock-evolve-architecture.md), and it's the contract Person B builds against, so B can start in parallel. The spec leaves three things open: sync vs async PyMongo, where the document shape is defined, and how events with no task get a sequence.

## Decisions

- **`pymongo.AsyncMongoClient` everywhere, not sync PyMongo in FastAPI's thread pool.** Every open SSE stream would hold one of AnyIO's 40 default worker threads, so a few dozen tabs or reconnects would starve every sync route. Motor is end-of-life. Both the API and the job-worker use the async client, and there are no sync `def` routes. Callbacks passed to `with_transaction` run again on `TransientTransactionError`, so they must do only database work: no HTTP, no git, no in-memory side effects.
- **State `StrEnum`s and ID prefixes in `controller/states.py` are the A/B contract, and `setup.py` hand-writes open `$jsonSchema` validators from them.**
  - Validators enforce required ids, `schema_version`, BSON Date fields, and the state enums, with `validationLevel: strict` and `validationAction: error`. Extra fields are allowed.
  - Generating the validators from Pydantic was rejected. Pydantic emits JSON Schema 2020-12, but Mongo takes a draft-4 subset with `bsonType` and no `$ref`, and `datetime` comes out as `string/date-time` rather than `bsonType: date`, so we'd need a converter.
  - Closed schemas (`additionalProperties: false`) were rejected because every field B adds would need a `collMod` coordinated through A.
- **IDs are `f"{prefix}_{ObjectId()}"`.** They sort by time and come from `bson`, which ships with PyMongo.
- **Events are keyed by `stream_id` (a task_id or a project_id) with a unique `(stream_id, sequence)` index.** The per-stream counter is an `event_seq` field on the owning task or project document, incremented with `$inc` inside the transaction that makes the state change.
  - The spec's `(task_id, sequence)` index can't sequence promotion, rollback, or revocation events, because those belong to a project, not a task.
  - Two partial indexes were rejected because clients would then need to know which cursor field applies.
  - A `counters` collection was rejected because it would be a twelfth collection.
- **`setup.py` is idempotent.** It runs `create_collection` when a collection is missing and `collMod` otherwise, then `create_indexes`. The index on `(project_id, actor_id, source_event_id)` is a partial unique index, limited to documents that have a `source_event_id`.
- **`projects` gains two fields:** `event_seq`, and `validated_harness_ids` for rollback ([0007](0007-promotion-and-policies.md)). **`tasks` gains `event_seq`.** Incrementing `event_seq` never touches `revision`, so appending an event never makes a compare-and-set stale.

## Interface to B

- Import states and ID prefixes from `controller/states.py`, and never write state strings by hand.
- Fields beyond the validated core are allowed, but enum values and ids are enforced by the database.

## Consequences

- Atlas cloud is the one shared store for every real run, so provenance and harness learning are visible to all team members. Tests use the same cluster through `AIRLOCK_MONGO_URI`, but each test run gets its own `airlock_test_<uuid>` database, which is dropped at teardown and never touches `airlock`. Mongomock was rejected because it has neither transactions nor change streams, and a local container because it needs Docker on every machine. The costs are network latency per operation and M0 throughput limits on race tests.
- Validators don't check relationships between documents (task ownership, DAG consistency). The controller checks those ([0005](0005-state-machine.md)).
