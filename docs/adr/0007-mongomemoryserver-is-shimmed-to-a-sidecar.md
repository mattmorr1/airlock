---
status: proposed
---

# MongoMemoryServer is shimmed to an atlas-local sidecar

LibreChat's tests start an in-process MongoMemoryServer, a standalone mongod with no change streams. Inside the Grader's container only, a Jest `moduleNameMapper` swaps `mongodb-memory-server` for a shim that gives each test file a fresh database on an `atlas-local` sidecar, so database writes are observed server-side by a change stream (`showExpandedEvents`) that test code cannot bypass. We rejected in-process driver command monitoring, which needs no sidecar but misses any client the code creates outside the patched path.

## Consequences

- The shim also removes MongoMemoryServer's mongod binary download, which fails under the Grader's no-egress network.
- Tests that use MongoMemoryServer APIs beyond `create`, `getUri`, and `stop` break under the shim; spike S3 checks this before this ADR is accepted.
