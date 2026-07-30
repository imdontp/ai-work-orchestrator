# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Follow `AGENTS.md` as the primary repository instruction. It defines the non-negotiable architecture rules, the current milestone scope, and the human approval boundaries. This file adds Claude-specific role guidance plus the commands and architecture map.

## Commands

```bash
make dev            # pip install -e '.[dev]'  (Python 3.12+, in a venv)
make test           # pytest  (config in pyproject.toml: testpaths=tests, asyncio_mode=auto)
make test-live      # AIWO_LIVE_TESTS=1 pytest tests/test_worker_adapters_live.py
make spike SANDBOX=<dir>   # re-record CLI behaviour after an upgrade
make lint           # ruff check .            (line-length 100, rules E,F,I,B,UP)
make typecheck      # mypy apps orchestrator workers execution  (strict mode)
make validate       # python scripts/validate_contracts.py
make run            # uvicorn apps.api.app.main:app --reload
make postgres-up    # docker compose up -d postgres
```

`tests/test_run_store.py` runs one contract suite against both storage backends. The
Postgres parametrization skips when no database is reachable, so a green suite without
`make postgres-up` has only proven the filesystem half:

```bash
make postgres-up && pytest tests/test_run_store.py -v   # both backends
```

Single test: `pytest tests/test_state_machine.py::test_name`. Use `-q` off with `pytest -o addopts=""` when you need full output.

Endpoints once running: `http://127.0.0.1:8000/health`, `/docs`, `/api/v1/system/capabilities`.

`make validate` is a separate gate from `make test` — it checks that all four files in `contracts/` exist and use the 2020-12 JSON Schema dialect. Run it whenever contracts change.

### Platform note

The target machine is native Windows with **no WSL distribution installed**. `execution/process_manager.py` dispatches on platform: `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` on Windows, `SIGTERM`/`SIGKILL` to the process group on POSIX; `WriteBarrier` uses `icacls` deny aces on Windows and mode bits on POSIX.

The POSIX branches are exercised in a Linux container rather than left as "should work":

```bash
bash scripts/verify_posix.sh        # process manager, workspace guard, worktree manager
```

Run it from Git Bash, not PowerShell — PowerShell resolves `bash` to WSL, which has no distribution. Behind a TLS-inspecting proxy the container cannot reach PyPI; export the host trust store to `.ca-bundle.pem` (gitignored) and the script mounts it rather than disabling certificate verification:

```powershell
$sb = New-Object System.Text.StringBuilder
Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root | ForEach-Object {
  [void]$sb.AppendLine("-----BEGIN CERTIFICATE-----")
  [void]$sb.AppendLine([Convert]::ToBase64String($_.RawData, 'InsertLineBreaks'))
  [void]$sb.AppendLine("-----END CERTIFICATE-----")
}
[System.IO.File]::WriteAllText(".ca-bundle.pem", $sb.ToString())
```

## Architecture

The system is a **deterministic control plane** driving **LLM CLI workers**. The split is the core design constraint: application code owns state, permissions, timeouts, retries, and approval gates; agents only supply judgment and implementation.

```
apps/api          FastAPI control plane — health, capabilities, task intake, run control
  services/       OrchestrationService: builds runners, supervises background advances
apps/web/static   Operator dashboard — plain ES modules, no build step, mounted at
                  /dashboard by the API itself so it is same-origin (ADR-011, ADR-012)
orchestrator/     Deterministic decision layer — no provider knowledge
  domain/         Task, TaskState, TaskPermissions, ExecutionMode, ApprovalRisk (pydantic)
  state_machine/  TaskStateMachine — explicit allowed-transition table, raises InvalidTransition
  policies/       PermissionPolicy — action + TaskPermissions -> PolicyDecision (allow/approval/risk)
  routing/        StaticRoutingPolicy — picks ExecutionMode from task shape
  context_builder/ ContextPackage + ContextPackageBuilder — the curated handoff payload
  workflow/       definition (DAG load + validation), store (RunStore contract +
                  FilesystemRunStore), postgres_store (PostgresRunStore), runner
workers/          WorkerAdapter ABC (health_check/start/stream_events/cancel/collect),
                  CliWorkerAdapter (shared process plumbing), ClaudeCodeAdapter, CodexAdapter
execution/        ProcessManager (argv-only subprocess, no shell), VerificationRunner,
                  WorktreeManager (worktree lifecycle), workspace_guard (containment)
contracts/        JSON Schemas — the versioned wire format between workers
workflows/        YAML node graphs (analyze -> implement -> verify -> review -> final_approval)
prompts/          Per-role system prompts (task_analyst, implementer, reviewer)
docs/             Vision, MVP scope, domain model, workflow spec, security policy, ADRs
artifacts/ runs/  Runtime output roots, configured via .env
                  (worktrees/ is NOT a workspace root — see its README and ADR-010)
```

### Rules the code encodes

- **Handoff is artifact-mediated.** Worker A never talks to worker B. Output is normalized into an artifact, the orchestrator routes it, and `ContextPackage` builds worker B's input. Raw transcripts are not forwarded.
- **Worker success is a claim, not evidence.** `worker-result.schema.json` deliberately names the field `verification.claimed_passed`. `VerificationRunner` re-runs commands mechanically; a worker reporting `completed` moves the task to `VERIFYING`, never straight to `COMPLETED`.
- **Provider specifics are quarantined.** `orchestrator/` and `execution/` must stay provider-agnostic; `ProcessManager` accepts an argv list and refuses shell strings. Adapters build the argv.
- **Independent review uses a fresh session.** Encoded in `workflows/analyze-implement-review.yaml` as `session_policy: new` on the review node.
- **Deny by default.** `PermissionPolicy` hard-denies `git_push` regardless of task permissions; network, writes, and secrets require explicit grants. `.env` defaults `ALLOW_NETWORK_ACCESS` and `ALLOW_GIT_PUSH` to false.

### Keep these five concepts distinct

Agent Profile != Worker Adapter != Model != Workflow Node != Session. Conflating them is the failure mode this architecture exists to prevent — see `docs/DOMAIN_MODEL.md` and ADR-004.

## Milestone gate

The M1 capability spike is complete and both of its blockers are closed. `execution/worktree_manager.py`, `execution/workspace_guard.py`, `workers/claude_code.py` and `workers/codex.py` are implemented. `workers/opencode.py` remains an intentional docstring-only placeholder — MVP scope defers it until the initial pipeline is stable.

Verified argv templates for Claude Code and Codex are in `docs/spikes/M1_CLI_CAPABILITY_REPORT.md` section 7. They were executed on this machine and their output recorded. Treat them as **unverified again after any CLI upgrade** and re-run `scripts/spike_m1.py` — the first Codex suite failed 12/15 probes because `-a/--ask-for-approval` exists on `codex` but not on `codex exec`. Documentation is not evidence; only a recorded local run is.

Write-capable runs must go through `WorktreeManager.create()` and be bracketed by `WorkspaceContainment` with `worktree.git_allowances`. See ADR-010 — no worker sandbox flag is trusted for containment. `WorkflowRunner` already does this; anything driving a worker outside the runner must do it too.

The task states describe the run's *phase*, not each node. Consecutive nodes of the same kind need no transition; where the table has no direct edge, `READY` is the neutral state a run passes back through. Never add a `_transition` call that bypasses `TaskStateMachine`.

Adapter rules:

- Provider specifics live only in `workers/claude_code.py` and `workers/codex.py`. `CliWorkerAdapter` owns spawning, streaming, cancellation and outcome normalization and must not learn a flag name.
- Adapter logs go to `WorkerRequest.log_dir`, never into the workspace — a log written into the worktree lands in the worker's own diff.
- Claude Code's JSON envelope reports `subtype: "success"` even on failure; read `is_error` and `api_error_status` instead.
- Codex's flag surface differs per subcommand. `health_check()` asserts `required_flags` are still advertised; do not add a flag to an adapter without adding it there.

## Claude Code role during early milestones

Claude Code is expected to act mainly as:

- Requirement analyst
- Architecture reviewer
- Plan author
- Independent code reviewer

It is not automatically trusted as an executor or verifier.

## Working rules

- Start with a new session for a new task.
- Resume only for repair work on the same task.
- Independent review must use a fresh session.
- Do not infer that a CLI command works from documentation alone; record it as unverified until capability testing succeeds.
- Separate observed facts, inferences, assumptions, and recommendations.
- When reviewing code, inspect actual diffs and test evidence before implementation summaries.
- Never push or modify external systems.

## Expected structured review result

Must validate against `contracts/review-result.schema.json`.

```json
{
  "schema_version": "1.0",
  "task_id": "TASK-001",
  "run_id": "RUN-001",
  "verdict": "pass",
  "findings": [],
  "residual_risks": [],
  "confidence": 0.9
}
```
