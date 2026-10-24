# Artifact-lifecycle test boundaries

Status: accepted by the user on 2026-10-24 (Q5). ADR-0001 records the accepted
ownership, closeout, recovery, and opportunistic collection policy (Q1-Q4).
Acceptance evidence will be recorded separately as implementation proceeds.

Baseline: `a924afd53ba523da238757c6274e2f2f853ca1fb` (v0.1.1).

## Public boundaries

| Boundary | Observable evidence | What it does not prove |
| --- | --- | --- |
| Execution CLI (`run_dsh.py`) | Exit code, one JSON result, run identity, file effects, unchanged borrowed input, compatibility of existing result fields | Whether a model's answer is correct or the main agent has accepted it |
| Management CLI (`manage_dsh.py`, proposed closeout/retention operations) | Preview without writes; permitted deletion and retained-path reasons; refusal on live/unknown/replaced targets; repeat-operation behavior | Protection against a privileged adversary controlling the entire machine |
| Installer CLI (`install.py`) | Fresh installation and upgrade in temporary destinations; installed entrypoints, receipts, preserved configuration and v0.1.1 launcher inheritance | Real account/model availability or task acceptance |
| One native Codex-to-Harness smoke flow | Real launch, result readback, main-agent acceptance, closeout, and a same-session follow-up with protected output still present | Exhaustive failure/race coverage or non-Linux platform acceptance |

## Existing test infrastructure to extend

- `scripts/test_run_dsh.py` already launches a fake Harness through the real CLI
  in `TemporaryDirectory` and checks stdin transport, structured errors, log
  bounds, process cleanup, and session-selection continuity.
- `scripts/test_supervision.py` already covers real subprocess status/control
  roundtrips, stale observations, path replacement, and PID reuse. A reported
  terminal state is separate from verified process identity, so terminal-state
  observations alone must not serve as filesystem deletion authority.
- `scripts/test_install.py` uses isolated destinations and fake executables. It
  already checks rollback, preserved unknown configuration, idempotent upgrades,
  and the v0.1.1 removal of managed launcher model pins.
- `.github/workflows/ci.yml` runs syntax checks and the offline tests on Python
  3.10 and 3.12. New offline behavioral tests should join this suite.

## Acceptance scenarios

1. A successful Harness result remains available while task acceptance is
   pending. Existing result semantics stay compatible.
2. Accepted closeout removes only eligible owned scratch, preserves final output,
   and emits one bounded receipt; repeating it is harmless.
3. Failed, cancelled, timed-out, and unknown executions follow the selected
   recovery policy instead of inheriting success semantics.
4. Borrowed inputs and paths outside the selected ownership boundary remain
   unchanged, including links and a target replaced after preview.
5. Retention handles the seven-day boundary with deterministic time, preserving
   active work, recovery material, sessions, and retained evidence.
6. A concurrent resume or writer cannot lose its files to a stale cleanup plan.
7. Automatic collection is bounded, reaches eligible runs across repeated
   passes, and reports failures without changing the current task result.
8. A partial filesystem failure has an explicit retryable outcome; a cleanup
   error does not rewrite the original execution or acceptance result.
9. A v0.1.1 installation upgrades without changing model/profile selection,
   credentials, permissions, proxy policy, or unrelated user files. Installing
   the feature itself performs no historical garbage collection.

The retention clock starts at explicit closeout, as accepted in ADR-0001.
Production command names and option schema remain implementation choices. Real-provider
validation stays separate from the offline suite; a timeout is reported without
automatically replaying the model request.
