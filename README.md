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

- Product and architecture documentation
- Domain model and workflow specification
- Security and approval policy
- JSON Schema contracts
- FastAPI application scaffold
- Deterministic state-machine baseline
- Worker adapter interface
- Safe subprocess execution baseline
- Example workflow and prompts
- Initial automated tests
- Docker Compose PostgreSQL service

## What is intentionally not included yet

- Production Claude Code or Codex command adapters
- Full database persistence
- Git worktree lifecycle implementation
- Web dashboard
- OpenCode integration
- Parallel workflows
- Autonomous swarm behavior

These are delivered in later milestones after the CLI capability spike validates real command behavior.

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

| Milestone | Outcome |
|---|---|
| M0 Foundation | Architecture, contracts, policies, repository scaffold |
| M1 CLI Capability Spike | Validate Claude Code and Codex headless execution |
| M2 CLI Agent Bridge | Real worker adapters and process supervision |
| M3 Vertical Slice | Analyze -> approve -> implement -> verify -> review -> approve |
| M4 Reliability | Retry, heartbeat, restart recovery, worktree cleanup |
| M5 Dashboard | Overview, task queue, run DAG, logs, artifacts, approvals |
| M6 Expansion | OpenCode, parallel DAG, quota-aware routing |

## Core principles

1. Application code controls execution.
2. LLM agents provide judgment and implementation.
3. Humans control meaningful risk.
4. Workers communicate through versioned artifacts, not direct chat.
5. Agent profile, worker, model, workflow node, and session are separate concepts.
6. Repository content and generated artifacts are untrusted inputs.
7. A worker claiming completion is not evidence of completion.

Read `AGENTS.md` before allowing a coding agent to modify this repository.
