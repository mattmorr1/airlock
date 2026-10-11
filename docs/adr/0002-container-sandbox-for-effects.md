# Container sandbox enforces Effect manifests

Each Task's tests run in a fresh Docker container, and `docker diff` plus a network restricted to an internal Docker network give the observed effects that the Grader compares against the Task's Effect manifest. Attribution is per process, which matches manifests being per Task. We rejected in-process fakes (code bypasses them by importing `os`/`fs` directly) and syscall tracing (Linux-only; macOS SIP blocks dtrace, and the team runs Macs).

## Consequences

- The repository must be copied into the container, not bind-mounted: writes through a bind mount land on the host and never appear in `docker diff`.
- An internal network permits sidecar services (such as a test database) while blocking all external egress.
