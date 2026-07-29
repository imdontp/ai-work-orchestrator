# Backlog

What is left, in the order it should be picked up. Each entry says what it is, why it
sits where it does, and how you will know it is done.

This is not a list of everything that could be built. Deferred scope — the web
dashboard (ADR-009), `workers/opencode.py`, parallel execution — is absent on purpose:
those are decisions recorded in `docs/MVP_SCOPE.md`, not debt. Listing them here would
make settled choices read as unfinished work.

Status as of the last update: Milestone 2 is complete, every item in the MVP definition
of done has live evidence behind it, and five runs have been driven against the real
Claude Code and Codex CLIs. `docs/START_HERE.md` records what each run established.

---

## 1. Run the pipeline against a real project

**Start here.** Every run so far used a throwaway repository built for the run — one
file of implementation, a test suite finishing in milliseconds. Nothing is known about
how the system behaves at real size.

Pick an existing checkout (not this one; ADR-010 forbids it and the config validator
refuses it), set `PROJECT_ROOT` and `VERIFICATION_COMMANDS` for it, and carry one
bounded, genuinely useful task through the full workflow.

Watch, because none of it has been observed:

- **Context size.** `ContextPackageBuilder` inlines prior artifacts up to
  `MAX_INLINE_ARTIFACT_CHARS` (20k) and references them by path beyond that. A real
  analysis may cross that line for the first time.
- **Verification duration** against `node_timeout_seconds` (1800). A suite taking
  minutes is fine; one taking longer is not.
- **Whether the analysis picks up existing conventions,** or proposes something the
  repository would reject.
- **The approval package at real diff size,** now that it names files rather than
  counting them.

Expect defects. Every run so far produced at least one, and none were found by reading
code.

**Done when:** a real task has been carried end to end, and whatever it exposed is
either fixed or written down.

## 2. Live evidence for a verification failure

The path where `VerificationRunner` contradicts a worker's success claim is the core of
rule 5, and it has unit coverage only.

Run 2 tried and missed: the implementer reported `status: "blocked"`, which sends the
run to repair before the verify node is reached, so the contradiction never happened.
Forcing it needs a worker that reports `completed` while the suite is actually red,
which honest workers do not readily do.

Better reached opportunistically during item 1, once the work is large enough to get
wrong, than staged. If it must be staged, aim for a change that passes the tests the
worker can see and fails one it cannot.

**Done when:** a run records `verification_finished` with `passed: false` alongside
`claimed_passed: true`, the two stay separately recorded, and the run does not reach
`COMPLETED`.

## 3. Live evidence for a review returning `request_changes`

The independent reviewer has only ever returned `pass` in a live run. The branch turning
a review verdict into a repair round is covered by tests and by the wording fix in
`56c5972`, but has never been driven by a real reviewer.

Needs an implementation that passes verification and still has something a reviewer
would object to — so, again, most likely to appear during item 1.

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
