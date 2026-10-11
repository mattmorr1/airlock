---
status: accepted
---

# Promotion and policies: automatic conditional promotion, validated rollback, operator revoke

The spec reserves pointer promotion for a promotion controller, and the evaluator alone marks a candidate eligible ([§10](../../airlock-evolve-architecture.md)). It checks `constraint_epoch` everywhere, yet nothing in the spec ever increments it, and it says rollback goes to a "previously validated" version without saying how that's known.

## Decisions

- **Promotion is automatic and happens inside the evaluator's completion transaction.** When B's evaluate result is eligible, `complete()` calls A's `promote(evo_id)` in that same transaction.
  - The flip is one compare-and-set on `projects`. It matches on `{_id, active_harness_version_id: baseline, constraint_epoch: e}` and then:
    - sets `active_harness_version_id` to the candidate,
    - adds the candidate to `validated_harness_ids`,
    - increments `harness_generation`, `revision`, and `event_seq`,
    - moves the evolution run to `promoted`,
    - and appends a promotion event to the project stream.
  - If the compare-and-set misses, the evolution run moves to `rejected` with reason `stale_baseline` or `stale_epoch`.
  - When two candidates share a baseline, the first to commit wins and the second is rejected with `stale_baseline`. That's intended.
  - Rejected: operator-triggered promotion, which the spec's role table doesn't give the operator, and a separate promote job, which adds an extra step and a race window.
- **Rollback can only target a previously validated version.** `POST /v1/projects/{id}/rollback {harness_version_id}` (operator) is a compare-and-set on `{_id, validated_harness_ids: target}`.
  - It sets the active version, increments `harness_generation`, and appends an event.
  - The seed version is added to `validated_harness_ids` when the project is registered.
  - Rollback doesn't change the epoch. Evaluations still in flight against the old baseline fail the baseline check anyway.
  - History documents are never modified.
- **Revocation is an operator route.** `POST /v1/projects/{id}/revoke` (operator, via `airlock revoke`) increments `constraint_epoch` and appends an event. The epoch is checked at four points:
  - Approval records the epoch it was granted under.
  - Delegate returns 409 `REAPPROVAL_REQUIRED` if the epoch has moved since approval.
  - Validation acceptance records `inconclusive` if the worker run's pinned epoch is stale.
  - Promotion's compare-and-set fails.
  - Rejected: keeping the checks with no route, which leaves code no user path can reach, and dropping the epoch, which contradicts spec §11.
- **Pinning.** Each worker run pins `harness_version_id` and `constraint_epoch` when it launches. Promotion never changes a pinned run. New delegations read the active pointer. Revocation affects pinned runs immediately, at acceptance.

## Interface to B

- The evaluator writes metrics and eligibility and then calls `promote()` inside its own `complete()`.
- The evaluator never writes `projects`.

## Consequences

- A revoke mid-run is a 20-second demo beat: the in-flight validation comes back `inconclusive`, and the parent's next delegate call needs re-approval.
