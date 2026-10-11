# MongoDB Atlas is the shared memory store

Tests, per-test outcomes, and Rules live in MongoDB Atlas because the team works across several machines and the hackathon requires Atlas. Atlas only stores; it never executes tests, which always run in the Grader's container (ADR-0002). We rejected a SQLite file in the repository, which is simpler and needs no network, because it can't be shared across machines without committing mutable state to git.
