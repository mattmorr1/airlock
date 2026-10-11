---
status: accepted
---

# Events and SSE: sequence is the cursor, the change stream is only a doorbell

The spec says change streams wake the SSE broadcaster, that clients reconnect using stored event sequences, and also that resume tokens should be persisted, with a rebuild if a token expires ([§7.11](../../airlock-evolve-architecture.md)). [0001](0001-mongo-setup.md) already makes `(stream_id, sequence)` a durable, totally ordered cursor, so a resume token would be a second cursor over the same data.

## Decisions

- **Events are appended inside the transition's own transaction.** Each append increments `event_seq` with `$inc` on the stream's owner, the task or the project, and inserts `{stream_id, sequence, type, actor_id, payload, created_at}`.
  - Concurrent writers to one task serialize on the task document. A WriteConflict is retried by `with_transaction`. With 2-3 workers per task, that costs milliseconds.
- **One change stream per process, used only as a doorbell.**
  - It watches inserts on `events`, projected down to `stream_id`, and sets an `asyncio.Event` for that stream. The data itself is never read from the change stream.
  - If the stream errors, the broadcaster reopens it and wakes every client, and each client re-reads from its own cursor.
  - No resume tokens are persisted.
  - Rejected: spec-style resume tokens, which add storage and an expiry fallback without making anything more correct. Also rejected: pure polling, which costs 2N queries a second and drops the change-stream showcase.
- **SSE handler.** `GET /v1/tasks/{id}/events` and `GET /v1/projects/{id}/events` share one handler built on Starlette's `StreamingResponse`, with no `sse-starlette`.
  - Its loop: read `{stream_id, sequence > cursor}` in order, emit `id: <sequence>`, then wait on the stream's event or a 15-second timeout. A timeout sends a heartbeat comment.
  - Reconnects resume from `Last-Event-ID`.
- **Long-poll.** `GET /v1/tasks/{id}?wait_seconds=N` (N ≤ 50) uses the same doorbell to return as soon as the task's stream moves. It backs `airlock_status` ([0008](0008-plugin-and-mcp.md)).

## Consequences

- "Append-only" is enforced by the API and the database role, not cryptographically.
- A missed doorbell costs at most 15 seconds of latency, never a lost event.
