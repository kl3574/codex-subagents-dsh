# codex-subagents-dsh

A portable Codex skill that delegates scoped work to a local **DeepSeek Harness**
headless process through an ordinary Codex subagent launcher. The main agent owns
the task boundary and independently verifies the result; the launcher only starts
the process and returns structured evidence.

Version: **v0.1.1**. Ordinary runs have no default total deadline. Quiet periods
produce observations; explicit budgets, cancellation and confirmed execution errors
have separate outcomes.

## What it provides

- A short delegation workflow in [`SKILL.md`](SKILL.md).
- A bounded Python launcher (`scripts/run_dsh.py`) and readiness diagnostics
  (`scripts/doctor.py`).
- Two independent headless profiles, `codex-dsh` and `codex-dsh-review`, initialized
  from the **real installed `@deepseek-ai/dsh` headless template** only when missing.
- Default model selection: `deepseek-official / deepseek-flash / max`, with
  `default_patches=[]`.
- A preserve-first installer with source hashes, private receipts, backups, and
  explicit update/migration gates.
- Optional global Codex routing: a managed block in the global `AGENTS.md` plus
  `default`, `explorer`, and `worker` launcher instructions. It is off by default.

Neither the installer nor the launcher installs npm packages or modifies a shell
profile. The installer never downloads anything or copies `.env` files; if an existing
skill destination contains a `.env` file, it refuses to copy it. Move that file out and retry.

## Requirements and support

| Platform | Status |
|---|---|
| Linux/POSIX, Python 3.10+ | Supported and tested with the offline installer suite |
| Node 22.19+ | Required by the supported headless launcher; the installer validates it |
| `@deepseek-ai/dsh` 0.2.1-rc.2 | Template/profile initialization is verified; real model acceptance is NOT_RUN. The installer never installs or upgrades it |
| Windows | Not supported: the current wrapper refuses execution and process-tree cleanup is unverified |
| macOS | Not verified |

You need a working `dsh` installation before installing this skill. The installer
discovers a real `@deepseek-ai/dsh` npm package entry and deliberately skips desktop
shims that force their own `DSH_HOME`. It never downloads or installs a latest
version.

## Install

The installer performs no network access. The commands below are the only download
steps, so review and run them yourself. Replace the repository URL with the canonical
release URL if you use a fork or mirror.

```bash
git clone --branch v0.1.1 --depth 1 https://github.com/kl3574/codex-subagents-dsh.git
cd codex-subagents-dsh
python3 scripts/install.py --migrate-progress
```

`--migrate-progress` is required when an existing `bridge.json` uses schema 1. It
backs up the old bridge, removes the old default 300-second hard deadline, preserves
other values, and writes schema 2. Fresh installations do not need it.

Custom locations:

```bash
python3 scripts/install.py \
  --dest "$HOME/.agents/skills/codex-subagents-dsh" \
  --config-home "$HOME/.dsh-codex"
```

Pass an explicit Node/package entry when discovery cannot find one:

```bash
python3 scripts/install.py \
  --dsh-command-json '["/absolute/path/to/node","/absolute/path/to/@deepseek-ai/dsh/lib/bin.js"]'
```

The two array items must be absolute paths. The installer records the resolved
interpreter and entry in the private bridge; it does not reinstall or update `dsh`.

Updating an existing installation is transactional:

```bash
git -C codex-subagents-dsh fetch --tags
git -C codex-subagents-dsh checkout v0.1.1
python3 codex-subagents-dsh/scripts/install.py --migrate-progress
```

If the installer reports that a managed file was modified, review the reported paths
and re-run with `--update`. That flag explicitly authorizes backing up and replacing
those files; it is not needed for a clean source update. A pre-v0.1.0 skill directory
has no installer receipt, so its first upgrade normally also needs `--update` after
you review the conflict list.

### Optional global routing

```bash
python3 scripts/install.py --enable-global-routing
```

This updates the managed block in `~/.codex/AGENTS.md` and the
`description`/`developer_instructions` of `~/.codex/agents/{default,explorer,worker}.toml`.
Managed launchers have their root-level `model`, `model_reasoning_effort`, and
`service_tier` pins removed, including pins preserved by v0.1.0. The native launcher
uses Codex's resolved defaults: explicit spawn settings, then `[agents]` defaults,
then the parent session. The actual task model remains in the Harness profile,
defaulting to `deepseek-official / deepseek-flash / max`.

A stale native model can be rejected by the signed-in account before the launcher
has a chance to start Harness. Inheriting the launcher settings avoids that stale
per-role dependency; it does not check account model availability or change global
`[agents]` defaults. See the official [Codex subagent configuration](https://learn.chatgpt.com/docs/agent-configuration/subagents).

Every changed file is backed up, and unrelated keys and nested tables are preserved.
Unrecognized custom launchers remain untouched and produce a `PARTIAL` report with
an explicit migration command. To replace their routing instructions and remove
their native model pins after reviewing the existing roles, use:

```bash
python3 scripts/install.py --enable-global-routing --adopt-existing-launchers
```

This adopts only `default`, `explorer`, and `worker`; their original files are
backed up before replacement. The adoption flag requires `--enable-global-routing`.
It does not relax the checks on unsafe paths or change the main model, credentials,
permissions, or proxy settings. Re-running the installer is idempotent.

A legacy whole-file AGENTS.md with the old title is
migrated only when its old section is fully recognized; unknown instructions abort without
changing the file. Without this flag, the installer does not read or write global Codex
routing files.

If a native launcher still reports an unsupported model, check explicit spawn
overrides, global `[agents]` defaults, and project-scoped agent files. That startup
failure is distinct from Harness authentication or a DeepSeek model-request failure;
do not automatically replay the task as a workaround.

## What the installer writes

| Path | Purpose |
|---|---|
| `~/.agents/skills/codex-subagents-dsh/` (or `--dest`) | Complete skill tree; unknown user files in an existing destination are preserved |
| `~/.dsh-codex/bridge.json` (or `--config-home/bridge.json`) | Private schema-2 bridge; `0600` |
| `~/.dsh-codex/profiles/codex-dsh/` | Headless profile with `deepseek-official / deepseek-flash / max` |
| `~/.dsh-codex/profiles/codex-dsh-review/` | Independent review profile with the same model selection |
| `~/.dsh-codex/patches/max.yml` | Optional explicit max overlay, not referenced by `default_patches` |
| `~/.dsh-codex/install-receipt.json` | Private source/installed hashes and backup metadata; `0600` |
| `~/.dsh-codex/backups/` | Retained installation and global-routing backups |

Existing profiles and patches are never reset. Only missing profile directories are
initialized. `.env` files are neither copied into the skill nor modified in the
private config directory.

## Verification

Run the isolated installer suite first. It uses temporary HOME/dest/config and fake
`dsh` commands; it does not touch daily config and does not require network or a
model:

```bash
python3 scripts/test_install.py -v
```

Readiness checks do not call a model:

```bash
python3 "$HOME/.agents/skills/codex-subagents-dsh/scripts/doctor.py" \
  --config "$HOME/.dsh-codex/bridge.json" --cwd "$PWD"
```

The wrapper boundary suite includes an optional official `--dump-config` lane:

```bash
python3 "$HOME/.agents/skills/codex-subagents-dsh/scripts/test_run_dsh.py" \
  --official-config "$HOME/.dsh-codex/bridge.json" -v
```

These checks cover installation, protocol boundaries, and configuration composition.
The release also received separate real DeepSeek validation through native Codex
launchers: normal completion after silence warnings, same-session continuation,
live status observation and requested cancellation. Output hashes and cleanup were
checked independently. Simulated tests cover failures that were not induced against
the real API, including log exhaustion and post-final hangs. See the release notes
for the tested version and results. `harness_completed` does not mean task acceptance.

## Run a task

The full operating guide, progress semantics, error codes, and recovery rules are in
[`references/usage.md`](references/usage.md). A minimal invocation:

```bash
python3 "$HOME/.agents/skills/codex-subagents-dsh/scripts/run_dsh.py" \
  --config "$HOME/.dsh-codex/bridge.json" \
  --cwd /absolute/task/workspace \
  --task-file /absolute/private-task.txt
```

Add `--profile`, repeated `--patch`, `--session-id`, `--timeout`, and the
main-agent-chosen status file as needed. Explicit `--timeout` is a hard budget;
bridge `hard_timeout_seconds` defaults to `null`, so ordinary runs are not silently
killed by a wall-clock deadline.

## Repository layout

```text
SKILL.md                 short Codex delegation workflow
LICENSE, VERSION         installed with the skill for release identification
references/usage.md      full operation, errors, progress, and support notes
agents/openai.yaml       skill metadata
scripts/install.py       preserve-first installer and migration
scripts/test_install.py  isolated fake-dsh installer tests
scripts/run_dsh.py       headless launcher with schema-2 supervision
scripts/supervision.py   bounded status/control helpers
scripts/manage_dsh.py    owner status and cancel-request entry point
scripts/doctor.py        readiness checks without a model request
examples/                public managed-block and launcher templates
```

The installer treats a source archive as a plain directory; it does not require
`.git`.
