# Start Here

## Current status

Milestones 0, 1 and 2 are complete. Every item in the MVP definition of done has live
evidence behind it, and seven runs have been driven against the real Claude Code and
Codex CLIs — the last two against a real project, the seventh reaching `COMPLETED`.
The sections below record what each run established and what it did not.

Milestone 3 is **built and unevidenced**: ADR-011 scopes it to a read-mostly operator
dashboard, ADR-012 amends that with one endpoint, and all three slices are in
`apps/web/static/` — served at `/dashboard` by the API itself. No live run has been
watched through it yet, which is `docs/BACKLOG.md` item 12.

The spike found two blockers. Both are closed:

- **B1 — fixed.** `execution/process_manager.py` could not kill a timed-out process on
  Windows (`os.killpg` does not exist there) and no WSL distribution is installed.
  Termination is now platform-dispatched and covered by `tests/test_process_manager.py`.
  The POSIX branch is no longer assumed: `scripts/verify_posix.sh` exercises it, the
  workspace guard and the worktree manager in a Linux container.
- **B2 — contained.** Codex `-s workspace-write` did not confine writes. Containment is
  now owned by the orchestrator: see ADR-010 and `execution/workspace_guard.py`.
  `WORKTREE_ROOT` may no longer point inside the repository, and the deny-write barrier
  verifies that it actually denies rather than assuming the ACL took.

`execution/worktree_manager.py` is implemented against that layout: one run directory
per run, containing the worktree and nothing else, under a workspace root outside the
checkout. It creates, validates, locks, cleans up and reconciles after a restart.

`workers/claude_code.py` and `workers/codex.py` are implemented against section 7 of
the spike report, sharing process plumbing through `workers/cli_base.py`. They apply the
agent profile from `prompts/<profile>/system.md` — `--append-system-prompt` on Claude
Code, prepended to the payload on Codex, whose `-p/--profile` names a config profile and
not a role. The runner asks each adapter for a capability rather than naming a provider's
tools, and each worker gets its own model.

`orchestrator/workflow/` drives the node graph: it loads and validates the DAG, runs
each node through `TaskStateMachine`, creates a worktree for write nodes and brackets
them with `WorkspaceContainment`, re-runs verification commands mechanically, pauses at
approval gates, and bounds repair rounds. `RunStore` has two backends behind one
contract — `FilesystemRunStore` and `PostgresRunStore` — selected by
`RUN_STORE_BACKEND`, and `tests/test_run_store.py` runs one suite against both.

The HTTP surface is in `apps/api/app/routers/runs.py`: submit a task, list runs, fetch
one run, advance it, cancel it, read its events, fetch the approval package, fetch a
named artifact, post a decision. `OrchestrationService` supervises the background
advances and holds the runner for each, so a cancel can reach the worker it started.

Four defects surfaced while wiring this up and are fixed:

- `VERIFYING` had no edge to `READY`, so the shipped workflow — which puts independent
  review after verification — could not be executed at all by the shipped state machine.
- An approval could not follow an approval, which the final gate requires.
- The review gate was reviewing nothing: it now receives the diff and the evidence.
- The `Task` model was missing `constraints`, `scope`, `inputs` and `metadata`, all of
  which `contracts/task.schema.json` has. It now carries the full set.

## What has been run against the real CLIs

Seven runs. The first five used throwaway repositories built for the run; the last two
used a real project. Every run's artifacts and logs were deleted during cleanup, so
this section and the commits it names are what remains of them. Re-running regenerates
evidence; it does not restore those runs.

### Run 1 — the happy path

A task was submitted over the HTTP API and carried through the shipped workflow against
the real CLIs: analyze (Claude Code) → plan approval → implement (Codex, in a worktree)
→ verify → review (Claude Code, fresh session) → final approval → `COMPLETED`. The
target was a throwaway repository holding a failing test suite and no implementation,
so the run had real work to do and a red baseline to turn green.

What it established, from the run's own record rather than from the code:

- Every state change went through `TaskStateMachine`, including the `VERIFYING → READY`
  edge that a shipped workflow needs and that the shipped state machine once lacked.
- The worker claimed `verification.claimed_passed: true`; `VerificationRunner` re-ran
  the command itself and agreed. The two are separately recorded, so a disagreement
  would have been visible.
- Both write-capable nodes logged `containment_armed` with the deny barrier, and the
  primary checkout was untouched afterwards — the produced file existed only in the
  worktree.
- The run could not reach `COMPLETED` without a human decision at both gates.
- The analysis node, which is read-only, said so in its own risk list rather than
  writing the file it had planned.

Three defects surfaced that no unit test had caught, all now fixed:

- Identity fields in worker artifacts were the worker's guess. Codex reported
  `"worker": "/root"`. The runner now stamps `worker`, `task_id` and `run_id`.
- The approval package counted dirty files instead of naming them, so a human at the
  high-risk gate read "3 uncommitted change(s)" for one source file and two caches.
- `FilesystemRunStore.save` could fail on a transient Windows file lock, which would
  mark a healthy run failed. The rename now retries.

### Run 2 — the repair loop and its bound

The target held a test that reaches the network, which the task was not granted, at a
domain reserved by RFC 2606 that never resolves. So the suite could not be made to pass
from inside the run, whatever the implementation did.

Codex was invoked three times. Each time it wrote a correct `slugify.py` and reported
`status: "blocked"` with `claimed_passed: false` rather than claiming a success it did
not have — and rather than taking the loophole the analysis had spotted, that
`conftest.py` and `pytest.ini` sit at the root and are not literally "under `tests/`".
The run ended `FAILED_PERMANENT` with `repair_rounds: 2`.

What it established:

- The repair loop runs and is bounded. Rounds went `RUNNING → FAILED_RETRYABLE → READY`
  through the state machine each time, and the third failure ended the run rather than
  starting a fourth.
- `containment_armed` was logged on every repair round, not only the first.
- The identity stamping from run 1 works in the field: Codex reported `run_id` as
  `TASK-E2E-002-codex-1`, `-2` and `-3` — its own worker run id — and the runner
  corrected all three.

One defect surfaced, now fixed: every repair event and the final failure read "review
requested changes" when no review node had run. Both paths into that branch shared one
hardcoded string. The reason is now derived from the artifact and names the node
(`56c5972`).

The run also showed why a verification failure is hard to force: `status: "blocked"`
sends the run to repair before the verify node is reached, so the path where
`VerificationRunner` contradicts a worker's success claim is still unevidenced.

### Run 3 — a node timeout

`DEFAULT_TASK_TIMEOUT_SECONDS` was set to 5, well under the 40–90 seconds the analyze
node actually takes, so the worker was certain to be killed mid-flight. It was, three
times — the initial attempt and both repair rounds — and the run ended
`FAILED_PERMANENT` with `claude_code failed: exit=1 kind=timeout`.

This is the first live evidence for blocker B1. `ProcessManager` recorded
`termination: "taskkill_tree"` on all three attempts, which is the Windows branch
written to replace `os.killpg`; until now only unit tests with stand-in processes had
exercised it. All three worker PIDs were gone afterwards, with no orphaned process left
behind.

It also showed the repair loop driving a *failed* node rather than one asking for
changes — a different branch — and the recorded reason was the real one rather than a
shared string.

One thing to know rather than fix: a killed worker may or may not have emitted its
session id first. Two of the three attempts recorded none. Resuming a session after a
timeout is therefore not dependable; the shipped workflow does not try to, so nothing
is broken today.

No new defect surfaced. Unlike runs 1 and 2, this path behaved as designed throughout.

### Runs 4 and 5 — cancellation, and the feature it turned out to need

Driving this one started by finding there was nothing to drive. `MVP_SCOPE.md` lists
cancellation in scope and its definition of done says cancellation is visible, but the
only way to stop a run was to reject an approval — which requires the run to already be
paused. `WorkerAdapter.cancel` and `ProcessManager.cancel` existed and had no callers
anywhere outside `workers/`, `execution/` and the tests. A worker mid-flight could not
be stopped; the only recourse was to wait out `node_timeout_seconds`, thirty minutes by
default.

Rejecting at the plan gate was confirmed live first — `WAITING_APPROVAL → CANCELLED`,
the operator's reason kept as the failure, and a terminal run refusing both `/advance`
and `/decision` with 409. Then cancellation of a *running* run was implemented across
the runner, the service and the API (`8ad7ce5`), and driven against the real CLI: a run
cancelled while Claude Code was working reached `CANCELLED` in about 1.6 seconds, the
worker process was gone, and `repair_rounds` stayed at 0 — a killed worker exits
non-zero, and without a guard the run would have spent a repair round retrying the node
the operator had just stopped.

That live run also exposed a race. `wait` is woken by the death of the process, which
happens inside the kill, before `cancel` has recorded how it killed — so the outcome
recorded `termination: null` for a process `taskkill` had in fact killed. The kill was
never in doubt; the audit trail was. `ProcessHandle` now carries an event that `cancel`
creates before its first await, and `wait` waits for it. The pre-existing cancel test
could not have caught this: it cancels and then waits, where a real run has `wait`
running as its own task. Re-run against the real CLI afterwards, the outcome records
`termination: "taskkill_tree"`.

### Runs 6 and 7 — a real project, twice

The first five runs used repositories built for the run. Runs 6 and 7 used
`auto-trade-system`: 420 tracked files, 116 test files, a 283-second suite. Same task
both times — add unit tests for a module that had none — the same workers, and the same
repository. The only variable was the orchestrator.

**Run 6 failed, and was right to by its own rules.** Codex produced nine passing tests,
touched no source file, and reported `claimed_passed: false`. Verification ran the full
suite, the suite was red, the run ended `FAILED_PERMANENT`. The work was correct and the
system said otherwise, because "does this repository's suite pass" was never the question
worth asking.

Four things a toy repository could not have shown came out of it, recorded in
`docs/BACKLOG.md` item 1. The one that matters architecturally: the suite asserts on
`frontend/dist`, which `frontend/.gitignore` excludes, so five tests pass in the
developer's checkout and fail in any worktree. **A worktree is not equivalent to a
checkout** for a project with a build step, and nothing in ADR-005 says so.

**Run 7 reached `COMPLETED`.** With verification judged against the base revision
(`1314db0`), the same red suite produced `verified: true`, `baseline: [1]`,
`regressions: []` and a reason naming why. The suite was still red, the worker still said
so in `claimed_passed: false`, and all of those facts sit in one artifact rather than one
being smoothed away. The review returned `pass` and recorded that it had no shell tool,
so it did not claim to have run anything.

That is the MVP demonstrated on work someone actually wanted done, rather than on a
repository built to be demonstrated on.

One thing to know: `analyze` reads the primary checkout, including uncommitted and
untracked files, while `implement` gets a worktree from `HEAD`. In run 7 the analyst
planned around a file that existed only as an untracked change. Nothing went wrong —
the implementer wrote that file itself — but the two nodes were reasoning about
different repositories. `docs/BACKLOG.md` item 8.

### Still unevidenced

**A review returning `request_changes`** has unit coverage and no live run behind it.
It needs a worker to produce something defensible enough to verify and weak enough to
object to, and seven runs suggest that is hard to arrange with workers that report
honestly — run 2's implementer declined a loophole it had been shown and reported
`blocked` instead, and run 7's reviewer passed work that deserved to pass. It stays open
by its nature, not for want of trying.

**A containment violation will not be evidenced, and that is a decision.** The
interesting case is a worker that writes outside its workspace *without being told to*,
which cannot be scheduled by definition. A harness that instructs a worker to escape
would demonstrate only that an instructed escape is caught, which
`tests/test_workflow_runner.py` already shows with `FakeAdapter.escape_write`.

What supports the rule instead is narrower but real: the deny barrier is exercised
directly by the write-barrier tests, `WorkspaceContainment` is proven by unit tests on
both platforms — `scripts/verify_posix.sh` covers the POSIX side in a container — and
every live run has logged `containment_armed` on every write-capable node, with the
primary checkout confirmed untouched afterwards each time. The enforcement path is
tested; only a worker spontaneously misbehaving is not, and it cannot be.

## Recommended next action

**Drive a live run and watch it through the dashboard.** `docs/BACKLOG.md` item 12.

M3 is built. `make run`, then `http://127.0.0.1:8000/dashboard/`. It shows the workflow
DAG with the run's position on it, the approval package with approve, request changes
and reject, artifacts rendered from JSON, the workers and their sessions, and the event
trail. ADR-011 scopes it; ADR-012 records the one endpoint it needed and the boundary on
that endpoint.

Every substantive fix in this repository came from running the thing rather than reading
it — seven runs, five defects, none of them findable in code review. The dashboard is
the largest piece here with no live run behind it, and it was built against payloads
that were *shaped like* real ones rather than taken from a real one.

Read ADR-011 and ADR-012 before extending it. The scope they rule out is most of what a
dashboard could plausibly do, and the reasons are recorded there.

`docs/BACKLOG.md` holds everything else. Nothing in it blocks a milestone; the items are
evidence still to gather, decisions with no deadline, and one standing task. Use the
orchestrator on real work and the remaining unevidenced paths will arrive on their own —
they have resisted every attempt to stage them.

Keep `workers/opencode.py` a placeholder.

Re-run `make test-live` after any CLI upgrade — it is what re-establishes that the
recorded argv templates still hold.

Do not expose the dashboard beyond `127.0.0.1` before backlog item 5 lands. ADR-011
records why a browser client changes that calculation.

## Not yet done

Named here rather than discovered later. `docs/BACKLOG.md` carries the same items in
the order they should be picked up, with what each one needs to be considered done.

- No live evidence for a review returning `request_changes`. A verification failure has
  it — run 6 — and a containment violation will not get it by decision. See the section
  above for both.
- The API is unauthenticated. This is deliberate for a single-user local control plane
  and the reasoning is recorded in `docs/SECURITY_POLICY.md`; it is not a gap that has
  been overlooked, and it is a gap that must close before the API leaves localhost —
  which ADR-011 makes a prerequisite of shipping the dashboard anywhere but this machine.
- The dashboard has never been used on a live run. Its Python side has tests and its
  render path was exercised outside a browser against payloads shaped like the real
  contracts, but "an operator approved a real gate from the browser" is a claim nothing
  here supports yet. `docs/BACKLOG.md` item 12.
- `RunStore.append_event` opens the events file in append mode and carries the same
  transient-lock exposure on Windows that `save` was hardened against. It has never
  been observed failing, so it has been left alone rather than hardened on a guess.
- `workers/opencode.py` is a docstring-only placeholder, and parallel execution is
  designed for but deferred. Both are MVP scope decisions, not omissions.

## Review order

1. `README.md`
2. `docs/MVP_SCOPE.md`
3. `docs/SYSTEM_ARCHITECTURE.md`
4. `docs/DOMAIN_MODEL.md`
5. `docs/WORKFLOW_SPEC.md`
6. `docs/SECURITY_POLICY.md`
7. `contracts/`
8. `AGENTS.md`

## Milestone 1 deliverables — done

`docs/spikes/M1_CLI_CAPABILITY_REPORT.md` is a verified compatibility report for
Claude Code and Codex covering:

- Installed CLI version
- Authentication mode
- Headless invocation
- Structured output
- Streaming logs
- Exit code behavior
- Timeout and cancellation
- Session continuation
- Scoped file writes
- Behavior when quota or authentication fails

Observed commands and outputs are recorded. Raw evidence is regenerated by
`scripts/spike_m1.py` into `artifacts/m1-spike/<timestamp>/`.

Re-run the spike after any CLI upgrade. Verified argv templates go stale: the first
Codex suite failed 12/15 probes because `-a/--ask-for-approval` exists on `codex` but
not on `codex exec`.

## Published contracts

All six schemas in `contracts/` use the 2020-12 dialect and are checked by
`make validate`:

`task`, `worker-result`, `analysis-result`, `verification-result`, `review-result`,
`approval-package`.
