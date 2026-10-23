# Task artifacts and closeout

Baseline: `a924afd53ba523da238757c6274e2f2f853ca1fb` (v0.1.1).
Sources: the maintenance conversation, `scripts/run_dsh.py`,
`scripts/manage_dsh.py`, `scripts/supervision.py`, and the existing CLI tests.

## Findings

1. Process cleanup and artifact cleanup are different outcomes. `_cleanup_owned`
   terminates owned processes; it does not dispose of task inputs or scratch
   directories. The current result's `cleanup` field describes that process work.
2. `BoundedLog` bounds and rotates one invocation's logs. There is no cumulative
   retention policy or explicit post-acceptance artifact closeout operation.
3. The skill asks launchers to create task files even though the wrapper already
   supports stdin. The caller-owned task file has no disposal contract.
4. A terminal status value is useful observation, not sufficient deletion
   authority. `verify_status_document` reports terminal state separately from
   process identity; future filesystem operations must establish ownership and
   current activity independently.
5. The disk audit found artifacts from several applications and an older workflow
   system. It did not establish that the present DSH wrapper produced those large
   historical caches. Those caches are not a justified automatic migration target.
6. Auxiliary discovery runs in this conversation sometimes reached their explicit
   time budgets without a final answer. They are not acceptance evidence and must
   not be replayed merely to obtain a successful cleanup result.

## Improvements to decide

- Make task ownership, acceptance, and artifact closeout explicit at the public
  CLI boundary; keep the existing process-cleanup result compatible.
- Prefer stdin for ephemeral input. Give generated scratch one owned location
  and distinguish it from borrowed files and retained deliverables.
- Define when diagnostics become eligible for seven-day retention, and how
  unfinished work, recovery, and deliberately retained evidence stay protected.
- Turn ownership, path replacement, concurrency, and repeat execution rules into
  deterministic CLI tests. Keep judgement about the value of evidence with the
  main agent rather than a filename heuristic.
- Write one compact closeout receipt. Avoid a new collection of per-file reports
  and duplicate snapshots as a side effect of cleanup.

## Baseline validation

The existing offline suite on this baseline ran 117 tests: 109 passed and 8 were
conditionally skipped (`python3 -m unittest discover -s scripts -p 'test_*.py' -v`).
This is baseline evidence, not acceptance of the proposed feature or real-provider
integration. Existing CI runs syntax checks and the offline suite on Python 3.10
and 3.12; new behavioral tests should join that workflow.
