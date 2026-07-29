# AGENTS.md

This repository implements a local-first Personal AI Work Orchestrator.

## Mission

Build a reliable bridge between CLI agents. The platform must coordinate Claude Code, Codex, and later OpenCode without requiring the user to copy and paste context between terminals.

## Non-negotiable architecture rules

1. Keep these concepts separate:
   - Agent Profile
   - Worker Adapter
   - Model
   - Workflow Node
   - Session
   - Run
2. Worker-to-worker handoff must go through the orchestrator.
3. Handoffs use structured, versioned artifacts.
4. Do not forward complete raw transcripts by default.
5. Do not trust worker-reported success without mechanical verification.
6. Do not write to the user's primary source checkout.
7. Write tasks must execute in isolated worktrees or equivalent sandboxes.
8. No automatic Git push, PR creation, deployment, production access, or destructive command.
9. High-risk effects require explicit human approval.
10. Retry loops must be bounded.

## Current milestone

Milestone 1 CLI capability spike is complete: `docs/spikes/M1_CLI_CAPABILITY_REPORT.md`.
The Milestone 2 execution slice is merged.

- **B1 — fixed.** The `ProcessManager` timeout path called `os.killpg`, which does not
  exist on Windows, and no WSL distribution is installed on the target machine.
  Termination is now platform-dispatched, and the POSIX branch is exercised against a
  real process group by `scripts/verify_posix.sh` in a Linux container rather than
  assumed to work.
- **B2 — contained.** Codex `-s workspace-write` was observed writing above the
  workspace root. Rules 6 and 7 are now enforced by the orchestrator rather than the
  worker: ADR-010 and `execution/workspace_guard.py`. The deny-write barrier verifies
  that it actually denies instead of assuming the ACL took.

Rule 7 in practice: a write task runs in a worktree that is the only entry in its run
directory, that run directory carries a deny-write barrier, and the run is bracketed by
`WorkspaceContainment` so writes outside it fail the run. `WORKTREE_ROOT` must be
outside the repository; the application refuses to start otherwise.

`execution/worktree_manager.py` implements that layout, the Claude Code and Codex
adapters are implemented against the recorded argv templates, and
`orchestrator/workflow/` drives the node graph end to end in process.

Rules 5, 7, 9 and 10 are enforced in `orchestrator/workflow/runner.py` rather than
trusted to a worker: verification commands are re-run mechanically, write nodes execute
in a contained worktree, a run cannot reach `COMPLETED` without passing through
`WAITING_APPROVAL`, and repair rounds are bounded by the workflow. Rules 5, 7 and 9 now
have live evidence as well as tests — see the end-to-end run below.

Task intake and persistence are done: `RunStore` has a filesystem and a PostgreSQL
backend behind one contract, and `apps/api/app/routers/runs.py` submits a task, lists
and fetches runs, advances them, streams events, serves the approval package and
artifacts, and records a decision. The API is unauthenticated by design for a
single-user local control plane; the reasoning is in `docs/SECURITY_POLICY.md`.

One run has been driven end to end against the real Claude Code and Codex CLIs —
intake, analysis, plan approval, implementation in a worktree, mechanical verification,
independent review, final approval — which closes the last item in the MVP definition of
done. It found three defects that the tests did not: worker-guessed identity fields in
artifacts, an approval package that counted changes instead of naming them, and a
record rename that a Windows file lock could fail. All three are fixed.

A second run drove the repair loop to its bound: the target's suite could not be made to
pass from inside the run, Codex reported `blocked` three times rather than claiming a
success it did not have, and the run ended `FAILED_PERMANENT` at `repair_rounds: 2`. It
found one defect — every repair event named a review node that had never run — and
confirmed the identity stamping working in the field.

A third run forced a node timeout with a five-second budget. It is the first live
evidence for blocker B1: `ProcessManager` recorded `termination: "taskkill_tree"` on all
three attempts and left no orphaned process, where until then only unit tests with
stand-in processes had exercised the Windows branch.

A failing verification, a review asking for changes, a containment violation and a
cancellation still have unit coverage and no live evidence, and all three runs used
throwaway repositories rather than a real project. `docs/START_HERE.md` records what
each run established and what it did not.

## Allowed work

- Improve documentation
- Refine domain models and schemas
- Add tests for deterministic behavior
- Improve API and orchestration endpoints
- Add non-provider-specific execution abstractions
- Extend the worker adapters against recorded, re-verified argv templates

## Out of scope

- Guessing Claude Code or Codex flags
- Creating provider-specific command invocations without testing them locally
- Adding autonomous workflow generation
- Adding unrestricted shell access
- Adding multi-user features
- Adding cloud deployment or Kubernetes
- Adding self-modifying prompts or agents

## Engineering expectations

- Prefer small, reversible changes.
- Add or update tests with behavior changes.
- Preserve backward compatibility of published contracts unless versioned.
- Treat logs as potentially sensitive and redact credentials.
- Make failure states explicit.
- Use typed Python.
- Avoid hidden fallback from subscription workers to paid APIs.

## Human approval boundaries

Always stop before:

- Installing dependencies outside the approved project environment
- Modifying external systems
- Pushing Git branches or tags
- Opening or merging pull requests
- Accessing production or corporate systems
- Reading secrets not explicitly provided for the task

## Definition of done for a change

- Scope matches the assigned task.
- Tests pass.
- Contracts remain valid.
- Security boundaries remain intact.
- Assumptions and limitations are documented.
- No external side effect occurred without approval.
