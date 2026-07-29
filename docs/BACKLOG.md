# Backlog

What is left, in the order it should be picked up. Each entry says what it is, why it
sits where it does, and how you will know it is done.

This is not a list of everything that could be built. Deferred scope — the web
dashboard (ADR-009), `workers/opencode.py`, parallel execution — is absent on purpose:
those are decisions recorded in `docs/MVP_SCOPE.md`, not debt. Listing them here would
make settled choices read as unfinished work.

Status as of the last update: Milestone 2 is complete, every item in the MVP definition
of done has live evidence behind it, and six runs have been driven against the real
Claude Code and Codex CLIs — the sixth against a real project. `docs/START_HERE.md`
records what each run established.

---

## 1. Run the pipeline against a real project — first attempt done

Run 6 carried a task through `auto-trade-system` (420 tracked files, 116 test files,
a 283-second suite): add unit tests for `src/research/splits.py`, which had none.

**The work itself was correct.** Codex wrote `tests/test_splits.py` — nine tests, all
passing, no source file touched — and reported `claimed_passed: false` because the full
suite was red for reasons that had nothing to do with it. The file has been kept.

**The run still ended `FAILED_PERMANENT`,** because verification ran the full suite and
the full suite does not pass. That is the finding, and it is bigger than the run: see
item 2.

Four things a toy repository could never have shown, all found before the pipeline even
started:

- **The suite takes 283 seconds.** `runner_config()` hardcodes
  `VerificationCommand(timeout_seconds=600)`, so this project has a factor of two in
  hand and a slower one would hit the ceiling with no way to configure around it.
- **`.venv` is gitignored, so a worktree has no dependencies.** The interpreter has to
  be named by absolute path in `VERIFICATION_COMMANDS` while the code under test comes
  from the worktree.
- **The suite asserts on `frontend/dist`, which `frontend/.gitignore` excludes.** Five
  tests pass in the developer's checkout and fail in any worktree, because the built
  assets are not in git. **A worktree is not equivalent to a checkout** for any project
  with a build step — a real constraint on ADR-005 that nothing in the architecture
  states.
- **Analysis took 109 seconds** and produced a 7.3 KB artifact, comfortably under
  `MAX_INLINE_ARTIFACT_CHARS`. Context size was not the problem it was expected to be.

The analysis quality held up at real size: it cited source line numbers, adopted the
repository's own import convention over the one the task text specified, and flagged two
testing pitfalls (unstable sort on duplicate timestamps, `reset_index` breaking a naive
`DataFrame.equals`) that the task had not mentioned.

**Still to do:** run a task whose verification can actually pass, so the pipeline is
observed reaching `COMPLETED` on a real project rather than failing on the repository's
own state. That needs item 2 settled first.

## 2. Verification is only meaningful against a deterministic suite

Item 2 used to read "get live evidence for a verification failure". Run 6 supplied it —
`verification_finished` recorded `passed: false` twice, the run did not reach
`COMPLETED`, and the repair budget bounded it. Rule 5 works.

What it also showed is that **the runner cannot tell "the worker broke it" from "this
suite was already broken"**, and in run 6 it was the second. The worker did correct work
and the run failed. Two distinct causes, both outside the worker's control:

- **Pre-existing failures.** `test_strategy_gate_testnet_check_is_read_only` is red in
  the developer's checkout, before any agent touches it.
- **Order-dependent failures.** `test_dry_run_cycle_ignores_duplicate_signal` passes
  alone and fails in the full suite, returning `OPEN_ORDER_EXISTS` where it expects
  `DUPLICATE_SIGNAL` — state leaking between tests.

Deselecting known failures, as run 6 did, is a workaround that has to be maintained by
hand and is easy to get wrong: the deselect list for run 6 was built from a truncated
log and missed one, which then failed verification.

The decision to make: what the orchestrator should require of a project before it will
believe a verification result, and what it should do when the answer is "this suite is
not trustworthy". Options include recording a baseline of known failures at run start
and comparing against it, rather than requiring green.

**Done when:** the position is decided and written down, and a real project run reaches
`COMPLETED` through verification that means something.

## 3. Live evidence for a review returning `request_changes`

The independent reviewer has only ever returned `pass` in a live run. The branch turning
a review verdict into a repair round is covered by tests and by the wording fix in
`56c5972`, but has never been driven by a real reviewer.

Needs an implementation that passes verification and still has something a reviewer
would object to. Run 6 never reached the review node at all — verification failed first
and consumed the repair budget — so this is blocked behind item 2 in practice.

**Done when:** the repair reason names the review node, the implementation replays in
the same worktree, and the repair budget bounds it.

## 4. Decide how to evidence a containment violation

`WorkspaceContainment` failing a run for writes outside the workspace is the enforcement
behind rules 6 and 7 and ADR-010 — the answer to blocker B2, where Codex was observed
writing above its workspace root.

It has unit coverage and no live evidence, and provoking a real worker into escaping is
awkward: the interesting case is a worker that escapes *without being told to*, which is
exactly what cannot be scheduled.

This is a decision, not an experiment. Either:

- accept unit coverage as sufficient here and say so in `START_HERE.md` — defensible,
  since the barrier itself is separately proven by the write-barrier tests and every run
  logs `containment_armed`; or
- build a deliberate escape harness, a worker profile instructed to write to an absolute
  path outside the workspace, and record what the run does.

Either is fine. Leaving it silently unevidenced is not.

**Done when:** the choice is made and written into `START_HERE.md`.

## 5. Authentication, before the API leaves localhost

The control plane has no authentication. This is deliberate, documented in
`docs/SECURITY_POLICY.md` for a single-user local deployment, and `APP_HOST` defaults to
`127.0.0.1` to keep it there.

It stops being acceptable the moment the API binds to anything else: an unauthenticated
caller can start runs that execute CLI agents with filesystem write access against a
configured checkout.

**Do not start this speculatively.** It is listed so it cannot be forgotten at the point
it becomes required, not because it is due now.

**Blocking for:** any remote access, any second user, any deployment beyond this
machine.

## 6. Decide whether `append_event` needs the same lock tolerance as `save`

`FilesystemRunStore.save` had `os.replace` fail once with `PermissionError [WinError 5]`
— a scanner or indexer briefly holding the file. Fixed in `5554b9f` with a bounded
retry; a transient failure there would have marked a healthy run failed.

`append_event` opens the events file in append mode and carries the same exposure on
Windows. It has never been observed failing, so it was deliberately left alone rather
than hardened on a guess — this repository's standard is that documentation is not
evidence, only a recorded run is.

Revisit if it is ever seen failing, or when the run count is high enough that "never
observed" stops meaning much. The fix shape differs from `save`'s: there is no rename to
retry, so it would be a retry around the open.

**Done when:** it has failed and been fixed, or enough runs have accumulated to call it
a non-issue in writing.

## 7. Re-run the capability spike after any CLI upgrade

Standing task, not a one-off. The verified argv templates in
`docs/spikes/M1_CLI_CAPABILITY_REPORT.md` section 7 go stale: the first Codex suite
failed 12 of 15 probes because `-a/--ask-for-approval` exists on `codex` but not on
`codex exec`.

After upgrading either CLI:

- `make spike SANDBOX=<dir>` to re-record behaviour
- `make test-live` for the adapter suite — the 8 tests skipped in every ordinary run
- `health_check()` asserts `required_flags` are still advertised; a failure there is the
  early warning

Evidence on file was recorded against **Claude Code 2.1.220** and **codex-cli 0.145.0**.

**Done when:** the spike has been re-recorded and the live suite passes against the new
versions.
