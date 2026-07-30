# Personal AI Work Orchestrator

Local-first orchestration platform for coordinating subscription-based CLI agents such as Claude Code, Codex, and later OpenCode.

The product removes manual copy/paste handoffs between terminals by introducing task contracts, worker adapters, structured artifacts, deterministic workflows, verification, and human approval gates.

## MVP objective

Prove one reliable vertical slice:

```text
User task
  -> Claude Code analyzes and proposes a plan
  -> Human approves the plan
  -> Codex implements in an isolated Git worktree
  -> System verifier runs real commands
  -> Claude Code performs an independent review
  -> Human approves or requests changes
```

## What is included

That slice runs. Seven runs have been driven against the real Claude Code and Codex
CLIs, the last two against a real project; `docs/START_HERE.md` records what each one
established and what it did not.

- Product and architecture documentation, domain model, workflow spec, security policy
- JSON Schema contracts, checked by `make validate`
- FastAPI control plane — task intake, run control, events, artifacts, approvals, cancel
- Deterministic state machine with an explicit transition table
- Claude Code and Codex worker adapters, built on recorded argv templates
- Git worktree lifecycle with orchestrator-owned containment (ADR-010)
- Mechanical verification judged against the base revision, and bounded repair rounds
- Run persistence on the filesystem or PostgreSQL, behind one contract
- Docker Compose PostgreSQL service

## What is intentionally not included yet

- **Authentication** — deliberate for a single-user local control plane
  (`docs/SECURITY_POLICY.md`), and required before the API leaves `127.0.0.1`
- OpenCode integration
- Parallel workflows
- Autonomous swarm behavior

Authentication is scheduled. The last three are decisions recorded in
`docs/MVP_SCOPE.md`, not omissions.

The **operator dashboard is built** — `make run`, then
`http://127.0.0.1:8000/dashboard/`. It shows the workflow DAG with the run's position on
it, the approval gates with approve/reject, artifacts rendered from JSON, and the run's
event trail. It has not yet been used on a live run.

## Requirements

- Python 3.12+
- Docker with Docker Compose
- Git
- WSL2 recommended on Windows
- Claude Code and Codex CLIs authenticated locally for Milestone 1

## Quick start

```bash
cp .env.example .env
docker compose up -d postgres
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn apps.api.app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/docs
```

Run tests:

```bash
pytest
```

Validate JSON contracts:

```bash
python scripts/validate_contracts.py
```

## Milestones

| Milestone | Outcome | Status |
|---|---|---|
| M0 Foundation | Architecture, contracts, policies, repository scaffold | done |
| M1 CLI Capability Spike | Validate Claude Code and Codex headless execution | done |
| M2 CLI Agent Bridge and Vertical Slice | Real worker adapters, process supervision, and analyze -> approve -> implement -> verify -> review -> approve driven end to end | done |
| M3 Operator Dashboard | Run list and detail with the workflow DAG, approval inbox, cancel — nothing else (ADR-011, ADR-012) | built, not yet used on a live run |
| M4 Reliability | Retry, heartbeat, restart recovery, worktree cleanup | planned |
| M5 Expansion | OpenCode, parallel DAG, quota-aware routing | planned |

The vertical slice originally listed as its own milestone landed inside M2, which is why
the dashboard is M3 here and not M5. ADR-011 narrows it: an overview page of aggregate
counts is explicitly rejected, and the approval inbox is the reason the milestone exists.

## Core principles

1. Application code controls execution.
2. LLM agents provide judgment and implementation.
3. Humans control meaningful risk.
4. Workers communicate through versioned artifacts, not direct chat.
5. Agent profile, worker, model, workflow node, and session are separate concepts.
6. Repository content and generated artifacts are untrusted inputs.
7. A worker claiming completion is not evidence of completion.

Read `AGENTS.md` before allowing a coding agent to modify this repository.
