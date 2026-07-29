# Backlog

What is left, in the order it should be picked up. Each entry says what it is, why it
sits where it does, and how you will know it is done.

This is not a list of everything that could be built. Deferred scope — the web
dashboard (ADR-009), `workers/opencode.py`, parallel execution — is absent on purpose:
those are decisions recorded in `docs/MVP_SCOPE.md`, not debt. Listing them here would
make settled choices read as unfinished work.

Status as of the last update: Milestone 2 is complete, every item in the MVP definition
of done has live evidence behind it, and seven runs have been driven against the real
Claude Code and Codex CLIs. The seventh carried a task through a real project to
`COMPLETED`. `docs/START_HERE.md` records what each run established.

---

## 1. Run the pipeline against a real project — done

**Closed by run 7.** The same task, repository and workers as run 6; the only variable
was the orchestrator. Run 6 ended `FAILED_PERMANENT` because the target's suite was
already red. Run 7, with verification judged against the base revision (item 2), reached
`COMPLETED`.

Verification recorded `verified: true` with `baseline: [1]`, `regressions: []` and the
reason `commands failed on the base revision too; no regression detected` — the suite
was still red, the worker still said so in `claimed_passed: false`, and all four facts
sit in one artifact rather than one of them being smoothed away.

The review returned `pass` at confidence 0.9 with an info finding naming the frontend
build as why the suite is not green, and recorded that it had no shell tool so it did
not claim to have run anything. The produced `tests/test_splits.py` passes; the primary
checkout was untouched.

Both runs targeted `auto-trade-system` — 420 tracked files, 116 test files, a
283-second suite — with the task: add unit tests for `src/research/splits.py`, which had
none. Run 6's output was kept and is the file that now sits untracked in that
repository.

Four things a toy repository could never have shown, all found before run 6's pipeline
even started:

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

**What is still unknown at real size:** run 7 used no deselect list, so verification was
the plain suite and the baseline absorbed the pre-existing failures. What has not been
tried is a project whose suite runs longer than the hardcoded 600-second command
timeout, or a task large enough to push the analysis past the inline artifact limit.
Neither is a blocker; both are simply unmeasured.

## 2. Verification is judged against the base revision — settled

Item 2 used to read "get live evidence for a verification failure". Run 6 supplied it —
`verification_finished` recorded `passed: false` twice, the run did not reach
`COMPLETED`, and the repair budget bounded it. Rule 5 works.

**Settled in `1314db0`,** and proven in run 7: a failing command only fails the run when
it was not already failing on the base revision. The baseline is taken lazily, once per
run, and compared per command rather than per test. See the commit for why each of those
three was chosen and what each costs.

What run 6 showed is that **the runner cannot tell "the worker broke it" from "this
suite was already broken"**, and in run 6 it was the second. The worker did correct work
and the run failed. Two distinct causes, both outside the worker's control:

- **Pre-existing failures.** `test_strategy_gate_testnet_check_is_read_only` is red in
  the developer's checkout, before any agent touches it.
- **Order-dependent failures.** `test_dry_run_cycle_ignores_duplicate_signal` passes
  alone and fails in the full suite, returning `OPEN_ORDER_EXISTS` where it expects
  `DUPLICATE_SIGNAL` — state leaking between tests.

Deselecting known failures, as run 6 did, is a workaround that has to be maintained by
hand and is easy to get wrong: the deselect list for run 6 was built from a truncated
log and missed one, which then failed verification. Run 7 needed no deselect list at
all.

**What remains open, deliberately:** the comparison is per command, so a command that
was already red can hide a new failure inside itself. Test-level comparison would close
that, and would require the verifier to parse a specific framework's output — which is
the knowledge `execution/verifier.py` exists to avoid holding. The trade is recorded in
the contract and in the function's docstring rather than resolved. Revisit it if a run
is ever seen passing while carrying a real regression inside an already-failing command.

## 3. Live evidence for a review returning `request_changes`

The independent reviewer has only ever returned `pass` in a live run. The branch turning
a review verdict into a repair round is covered by tests and by the wording fix in
`56c5972`, but has never been driven by a real reviewer.

Needs an implementation that passes verification and still has something a reviewer would
object to. Run 6 never reached the review node at all, because verification failed first
and consumed the repair budget; item 2 removed that obstacle and run 7 got there — and
the reviewer passed work that deserved to pass, which is the correct outcome and not the
one this item needs.

So this stays open by its nature rather than by a blocker. It arrives when a real task
produces something defensible enough to verify and weak enough to object to.

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

## 8. Decide what a read-only node should see: the checkout or the base revision

Observed in run 7.

`_resolve_workspace` gives a read-only node with no write-node dependency the primary
checkout — which includes uncommitted and untracked files. A write node gets a worktree
created from `HEAD`, which does not.

In run 7 the analyst read a `tests/test_splits.py` that existed only as an untracked file
in the developer's checkout, and planned around it. The implementer, working from `HEAD`,
never saw that file. The outcome matched anyway because the implementer wrote it itself,
but the two nodes were reasoning about different repositories.

Why it matters: an analysis can plan against work in progress that the implementer will
never receive, and the resulting plan reads as though it were about the committed state.

The options are narrow. Point read-only nodes at a worktree from the same `base_ref` the
write node will use, so every node sees one state. Or keep the current behaviour and say
in the analyst's prompt that it is looking at a working tree which may differ from what
the implementer receives.

Not urgent — it produced no wrong result in run 7. Recorded because it is the kind of
thing that produces a baffling artifact months later.

**Done when:** the choice is made and either the code or the prompt reflects it.
