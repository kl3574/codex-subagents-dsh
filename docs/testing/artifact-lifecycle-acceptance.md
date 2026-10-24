# Artifact lifecycle acceptance — 2026-10-24

Scope: issues #1–#4. Baseline: `a924afd53ba523da238757c6274e2f2f853ca1fb`
(v0.1.1). Final runtime under test: `7a11aa1ce0ebd9f6cebee6513ae75f9aa2bdc112`.
This records development-branch acceptance, not a new release or a merge to main.

## Direct verification

| Lane | Result | Evidence and boundary |
| --- | --- | --- |
| Syntax | PASS | `python3 -m compileall -q scripts` |
| Complete offline suite | PASS | `python3 -m unittest discover -s scripts -p 'test_*.py'`: 141 tests, 133 passed, 8 conditional skips; 33.094 seconds on Python 3.10 |
| Lifecycle CLI tests | PASS | 22 tests through real CLI/file effects or public `execute`; controllable fake Harness and injected filesystem faults |
| Installer suite | PASS | 35 isolated tests, including missing runtime dependency rejection and simulated legacy receipt upgrade |
| Actual v0.1.1 source upgrade | PASS | Installed `git archive a924afd…` into temporary destinations with the fake installer fixture, upgraded to final source, checked bridge/profile/sentinel preservation, idempotence, and installed prepare/finalize; temporary tree removed |
| Local installation | PASS | Preserve-first installer installed the final source; runtime modules matched source bytes; main Codex configuration, bridge, profiles and credential-file hashes were unchanged; installer retained rollback backups |
| Skill and launcher validation | PASS | Skill validator passed; independent TOML parsing confirmed default/explorer/worker inherit native model settings and carry owned-run instructions |
| Native Codex → real Harness | PASS, bounded return-value lane | Default profile and patches, original permissions; result `323` independently checked by the main agent, evidence retained, explicit accepted closeout emptied scratch |
| Same-session follow-up | PASS | Official Harness accepted the same session and completed turn 2 with `324`; prior accepted evidence retained its hash; new run closed separately |
| Final installed-runtime continuation | PASS | After review fixes and reinstallation, the same session completed turn 3 with `FINAL_VALIDATION_OK`; zero tool calls/errors; final closeout and protected-evidence checks passed |

The return-value requests contained acceptance information, so these runs establish
official session continuity, output validation and artifact lifetime, not blind
memory recall. Live credentials/model requests are excluded from the offline suite.
The final installed `artifacts.py` SHA-256 was
`50023f7c327358f031a23cc74fe108df34851f9ad30058e8d681df5331e9bfd6`.

All six managed validation/review runs reached explicit closeout with empty scratch.
Their retained evidence and recent diagnostics remain in their respective areas.
Four earlier, unmanaged review/implementation invocations had only their explicitly
owned task/status files removed after main-agent acceptance or abandonment; compact
receipts, Harness logs and native histories were retained. Private tasks, raw session
transcripts, credentials and machine-specific installation reports are not published.

## Preserved RED evidence and fixes

These failures were observed before their corresponding fixes, then verified GREEN:

| Observable failure | Final behavior |
| --- | --- |
| prepare / managed execution / finalize / GC rejected as unknown arguments | Public lifecycle commands work through the actual CLI |
| Abandoned, never-executed run failed repeated closeout | Same decision returns the same closeout record |
| Gracefully cancelled/timed-out run was incorrectly classified UNKNOWN | Verified stopped execution remains pending acceptance and recoverable |
| Corrupt closeout object raised without a JSON result | Structured refusal preserves files |
| Managed result log said unmanaged | Logged artifact state agrees with returned result |
| Final receipt-write failure returned a generic usage failure | Retryable partial closeout retains the acceptance decision and keeps retention pending |
| prepare during slow GC lost its candidate registration | GC releases the index lock during scanning/deletion; closeout repairs failed registration idempotently |
| Partial GC left no durable progress record | Retry accumulates committed deletion counts and preserves partial status |
| Unexpected result-log failure left the lease BUSY in a long-lived process | Resource unwinding releases lease/log handles and restores signal handlers |
| Installer accepted a source missing artifacts.py | SOURCE_INCOMPLETE before destination/config writes |

Further boundary coverage includes exact seven-day expiry, preview without writes,
stale plans, changed directories, symlinks/hardlinks, active/unknown/unclosed/protected
exclusions, bounded fair passes, short writes and incomplete index tails, and
housekeeping failures that preserve the primary operation's result.

## Standards review

An independent native-launcher/Harness review assessed the integration diff
`2e5eabb...fcd6287`. It returned four findings; the main agent verified them directly:

1. Concurrent registration could be lost and short index writes lacked recovery.
   Reproduced, fixed with short metadata locking, complete writes, tail recovery,
   bounded registration waiting and idempotent registration on closeout.
2. Partial GC counts/status were not durable. Reproduced and fixed. Snapshot removal
   remains shared; closeout decisions and retention eligibility remain separate.
3. A late exception could leave a lease held in the public in-process API.
   Reproduced and fixed with resource unwinding.
4. finalize inferred the configured home by walking parents. Replaced with the
   home validated from the manifest.

Standards: four integration findings addressed; concurrency/record persistence were
the most consequential. A prior #2-only review also identified corrupt-record and
documentation issues, now fixed; its missing-GC observation belonged to later #3.

## Spec review

A separate native-launcher/Harness review of the same integration diff returned:

1. SKILL's optional status-path wording did not require the prepare-returned path.
   Corrected to distinguish managed and legacy invocations.
2. The global routing example omitted mandatory `--run-dir` for finalize.
   Corrected both accepted and abandoned forms.
3. Concurrent registration could omit an eligible run from future collection.
   Independently reproduced and fixed as above.

Spec: three integration findings addressed; candidate omission was the most
consequential. The earlier #2-only Spec review reached its 600-second hard budget
without a final answer: **NOT_VERIFIED**, not a clean review. It was not replayed.
The main agent verified #2 against its acceptance criteria through direct tests.
The later #3/#4 review was a distinct integration task. Delegated reports had
upstream event-projection truncation and were treated as findings to verify, not
as complete execution traces or substitutes for direct tests.

## Live boundary found

The first real file-writing smoke request targeted the managed evidence directory
outside the Harness workspace. Existing `workspace-write` policy denied the write;
an attempted escalation had no approval channel. Harness returned normally, but
task acceptance was **FAIL** and no output file was created. The main agent saved
the failure receipt and explicitly abandoned that run. The failed write request
was not replayed and permissions were not expanded.

The workflow now states that owned paths do not grant extra sandbox access:
launchers write temporary inputs, the main agent can retain returned evidence,
and Harness file deliverables use an already-authorized workspace. Separate bounded
return-value tasks established the successful live acceptance and continuation
evidence above. This does not claim Harness can write arbitrary managed paths.

## Remaining bounds

Only Linux was directly exercised; macOS and Windows acceptance is NOT_RUN.
Fault and concurrency cases used controlled fixtures, not induced real-provider
failures. GC manages only its candidate runs and never adopts historical scattered
caches or native session storage. Deletion follows owned directory identities and
cooperating leases; arbitrary privileged writers and escaped process groups remain
outside the stated guarantee. Durable counts can miss the last batch after a crash
or failed filesystem commit and are not a transactional filesystem audit.
