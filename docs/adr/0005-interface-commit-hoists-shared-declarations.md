---
status: proposed
---

# Interface commit hoists shared declarations

Every Stub, schema field, and type member that more than one Task touches lands in the Interface commit before any Task starts, so Tasks write only function bodies. Without this, the Task that owns a shared declaration conflicts with every Task that uses it, and the partition collapses into a serial chain. The cost is that the Contract must anticipate shared declarations up front: a Subagent that adds one fails the scope check, and the run returns to planning.
