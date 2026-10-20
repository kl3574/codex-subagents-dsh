<!-- codex-subagents-dsh:begin (managed block; edit outside these markers) -->
## Default delegation routing: DeepSeek Harness

For scoped work that benefits from delegation, use `codex-subagents-dsh`. The main agent
defines the task, permissions, cwd, acceptance criteria, and independently verifies the result.

- Read `{{SKILL_MD}}` before delegating. The launcher runs `{{RUN_DSH}}`.
- Bridge config: `{{BRIDGE_JSON}}`. The defaults are profile `codex-dsh`,
  `deepseek-official / deepseek-flash / max`, and `default_patches=[]`; use the
  actual bridge values when they differ. Change execution mode, profile or patches
  for a task only when the user explicitly requests it.
- Use an ordinary native Codex subagent launcher. Pass the full task, absolute cwd, allowed
  operations, acceptance criteria, and optional profile, ordered patches, session, timeout,
  and the main-agent-chosen status file.
- Keep `explorer` read-only and `worker` limited to authorized changes. The launcher prepares
  the task, starts the process, waits, queries or cancels through the owner entry point, and
  returns evidence. It does not recursively delegate or write the target artifact itself.
- Report Harness completion separately from task acceptance. Do not automatically replay an
  unknown model request or treat a heartbeat as task progress. Without a native subagent tool,
  native delegation is NOT_VERIFIED.
- This routing applies to all task and agent types. Report missing tools, credentials,
  permissions or Harness failures; never silently replace Harness with GPT execution.
  Preserve the existing main model, permissions, approval and proxy policies.
<!-- codex-subagents-dsh:end -->
