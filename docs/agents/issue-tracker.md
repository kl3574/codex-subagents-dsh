# Issue tracker: GitHub

Specs and tickets live in `kl3574/codex-subagents-dsh` GitHub Issues.
Use connected GitHub tools where available, or `gh --repo` / `gh api` for
operations the connector does not expose. Search existing issues before creating
one; read the complete body and comments before implementing a ticket.

Publish agreed technical scope and acceptance criteria. Keep credentials, raw
task inputs, private transcripts, and machine-specific evidence outside issues.
For multiline CLI bodies, write a temporary UTF-8 file and use `--body-file`.
Remove that owned temporary file after the request result is known.

Use GitHub sub-issues for a spec's tickets and native issue dependencies for
blocking edges. If the API does not support an operation, record `Part of #N` and
`Blocked by: #N` explicitly in issue bodies. A ticket becomes ready when all of
its blockers are complete. Label roles are defined in `triage-labels.md`.

PRs as a request surface: no.
