# trusted-evals (fixed independent scenarios and invariants)

The trusted suite the runner always controls. Candidate (worker) code cannot
remove these checks, and a generated scenario cannot declare itself passed —
the trusted inspectors (`runner/inspectors.py`) decide.

- `scenarios.py` — fixed scenario definitions, tagged `happy_path` / `error_path`.
- `driver.py` — executed *inside the bounded runner*. Drives the app over HTTP,
  reads final DB state, captures the log, and emits raw observations as JSON.
  Makes no judgments.

Separation of authority:
- The driver runs in the runner and emits **raw** observations.
- The trusted inspectors run controller-side and turn observations into checks.
- A harness config may only select scenarios (by tag) and inspectors; it cannot
  weaken an invariant.

The red-team scenario `login_unknown_user_exception` (tag `error_path`) is the
one that triggers the planted credential leak. A happy-path-only harness never
runs it, and without the `sensitive_logs` inspector the leak escapes — which is
exactly what the harness-evolution loop learns to fix.
