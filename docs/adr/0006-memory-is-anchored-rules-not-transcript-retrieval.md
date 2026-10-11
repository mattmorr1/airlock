---
status: proposed
---

# Memory is anchored Rules, not transcript retrieval

Atlas memory holds executable tests, per-test outcomes, and Rules anchored to indexed symbols or files; a Rule goes Stale when an Anchor changes and is used only after Promotion. Transcripts contribute candidate Rules extracted from the user's own turns only, never from tool output, and raw transcripts are not stored. We rejected retrieval over raw transcripts because it is the label creep this tool exists to prevent, and because replaying tool output into future plans is persistent prompt injection. We rejected executable-only memory because it can't learn repository conventions.
