# Hidden test feedback is opaque test id and Failure class only

When a Hidden test fails, the Subagent learns only an opaque test id (`H-<task>-<n>`) and its Failure class (assertion, exception, or timeout), never expected values, diffs, stack traces, or the test's real name, and it gets a Retry budget of 2 before the Task escalates to the human. Full output leaks expected values and voids the holdout after one retry; descriptive names like `rejects_negative_qty` leak the property under test; a bare pass/fail count gives the Subagent nothing to act on.
