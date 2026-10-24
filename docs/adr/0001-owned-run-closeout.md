# Owned run directories and explicit artifact closeout

Accepted by the user on 2026-10-24 (Q1-Q4).

The lifecycle tool manages only separate run directories that it creates. It
does not adopt repository files, borrowed inputs, final deliverables, or old
scattered caches. This limits automated deletion authority to known ownership,
at the cost of leaving historical unmanaged artifacts for separate maintenance.

The main agent explicitly closes a run after independent task acceptance and
delivery, or explicitly abandons it. Harness success and process cleanup never
imply task acceptance or authorize artifact deletion. Failed, timed-out,
cancelled, unknown, and otherwise unaccepted runs retain scratch and recovery
evidence until this decision; closeout is part of the main agent's normal task
workflow, not a new per-run user approval prompt.

Ordinary diagnostics become eligible for collection seven days after explicit
closeout. Final deliverables, deliberately retained evidence, and native Harness
session history stay outside that automatic retention policy. Keeping these
lifetimes separate preserves recovery without giving a log sweeper authority
over conversation history or project evidence.

New managed-run creation and explicit closeout opportunistically collect small
batches of eligible diagnostics. A preview and an explicit collection command
are also available. Collection skips active, unknown, unclosed, and protected
material; its failures are reported separately without changing the task result.
This provides routine upkeep without relying on a separate background service.
