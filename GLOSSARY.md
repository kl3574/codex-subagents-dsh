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

**Owned run directory**:
A separate directory created for one run by the lifecycle tool. It contains only
that run's managed temporary material, diagnostics, and closeout records.

**Borrowed input**:
A file supplied to a run that remains owned by its caller. Passing it to Harness
does not grant the lifecycle tool authority to remove it.

**Scratch**:
Disposable working material created inside an owned run directory. It remains
available while the main agent is evaluating or recovering the task.

**Artifact closeout**:
The main agent's explicit decision that a run has been accepted or abandoned,
followed by housekeeping of eligible owned files.
_Avoid_: process cleanup, or automatic acceptance on Harness exit.

**Diagnostics**:
Run observations retained for troubleshooting. They are distinct from final
deliverables, deliberately retained evidence, and Harness session history.

**Recovery protection**:
Retention of a run's scratch and recovery evidence while the run is unfinished,
unaccepted, or awaiting an explicit closeout decision.
