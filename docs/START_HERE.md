# Start Here

## Current status

Milestone 0 foundation and the Milestone 1 CLI capability spike are complete, and the
Milestone 2 execution slice is merged. Every stage of the MVP pipeline exists in code
and is covered by tests; what has not happened is a single run driven end to end against
the real CLIs.

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
one run, advance it, read its events, fetch the approval package, fetch a named
artifact, post a decision. `OrchestrationService` supervises the background advances.

Four defects surfaced while wiring this up and are fixed:

- `VERIFYING` had no edge to `READY`, so the shipped workflow — which puts independent
  review after verification — could not be executed at all by the shipped state machine.
- An approval could not follow an approval, which the final gate requires.
- The review gate was reviewing nothing: it now receives the diff and the evidence.
- The `Task` model was missing `constraints`, `scope`, `inputs` and `metadata`, all of
  which `contracts/task.schema.json` has. It now carries the full set.

## Recommended next action

Drive one run end to end against the real Claude Code and Codex CLIs: task intake,
analysis, plan approval, implementation in a worktree, mechanical verification,
independent review, final approval. Every part of that path has unit coverage and none
of it has been observed working together against live workers. That run is the last
item in the MVP definition of done, and it is where the assembled seams will fail if
they are going to.

Keep `workers/opencode.py` a placeholder.

Re-run `make test-live` after any CLI upgrade — it is what re-establishes that the
recorded argv templates still hold.

Do not begin the web dashboard yet (ADR-009).

## Not yet done

Named here rather than discovered later:

- No run has been driven end to end against the real CLIs.
- The API is unauthenticated. This is deliberate for a single-user local control plane
  and the reasoning is recorded in `docs/SECURITY_POLICY.md`; it is not a gap that has
  been overlooked, and it is a gap that must close before the API leaves localhost.
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
