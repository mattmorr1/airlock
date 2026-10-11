---
status: proposed
---

# Orchestration is deterministic code over the Agent SDK

After plan approval, a Node process drives the pipeline and spawns the hidden-test generator and Subagents through the Claude Agent SDK; the main Claude session does not orchestrate them with its Agent tool. LLMs do exactly four things: plan, generate Hidden tests, implement Tasks, and extract candidate Rules. Every gate decision (red, flake, scope, effects, merge) is code. An LLM orchestrator can misjudge or skip a gate, which is label creep applied to the tool itself, and its decisions can't be replayed from the event log.
