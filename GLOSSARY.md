# Codex subagents DSH

This context covers delegated task execution and the evidence returned to the
main agent. Execution completion and task acceptance are separate judgments.

## Language

**Main agent**:
The task owner that sets scope and independently decides whether the result meets
the requested outcome.

**Native launcher**:
A Codex subagent whose responsibility is to start and observe Harness, then return
its result to the main agent.

**Run**:
One invocation of Harness through the wrapper, with its own execution identity
and terminal outcome.

**Session**:
Harness conversation state that can continue across multiple runs.
_Avoid_: run, when referring to resumable conversation state.

**Task acceptance**:
The main agent's evidence-based judgment that a delegated result satisfies its
acceptance criteria.
_Avoid_: treating Harness completion as task acceptance.

**Process cleanup**:
Termination and reaping of processes owned by a run.
_Avoid_: using this term to imply that task files or artifacts were removed.
