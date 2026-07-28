# MVP Scope

## MVP goal

Deliver one reliable local pipeline across Claude Code and Codex.

```text
Task intake
  -> Claude analysis and plan
  -> Human plan approval
  -> Codex implementation in isolated worktree
  -> System verification
  -> Claude independent review
  -> Human final approval
```

## In scope

- Single-user local execution
- Direct and sequential pipeline concepts
- Deterministic state transitions
- Task and artifact contracts
- Claude Code and Codex worker adapters after capability spike
- Background process execution
- Structured logs and artifacts
- Timeout and cancellation
- Git worktree isolation
- Mechanical verification commands
- Independent review
- Human approval packages
- PostgreSQL-ready persistence model

## Out of scope

- Multi-user SaaS
- Autonomous swarm discussion
- Agent-created permanent agents
- Unbounded repair loops
- Automatic Git push or deployment
- Production or corporate environment access
- Multi-machine distributed execution
- Kubernetes or Temporal
- Visual workflow builder
- Long-term autonomous memory
- Self-learning router
- OpenCode and parallel execution until the initial pipeline is stable

## Initial execution shapes

1. Direct: one worker performs one bounded task.
2. Pipeline: worker A produces an artifact consumed by worker B.
3. Repair loop: implementation, verification, review, bounded repair.

Parallel execution is designed for but deferred.

## MVP definition of done

- A user submits a structured task.
- Claude produces a validated analysis artifact.
- The system pauses for plan approval.
- Codex receives a generated context package without manual copy and paste.
- Codex works only in an isolated worktree.
- The system runs verification commands itself.
- Claude reviews actual diff and evidence in a fresh session.
- The user receives an approval package.
- Failure, timeout, cancellation, and bounded retry are visible.
- No worker modifies the primary checkout or performs external side effects.
