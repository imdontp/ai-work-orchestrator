# System Architecture

## High-level architecture

```text
Web dashboard or CLI
        |
        v
FastAPI application
        |
        v
Deterministic orchestrator
  - state machine
  - DAG and dependency rules
  - routing policy
  - approval policy
  - retry and timeout
        |
        +--> Context builder
        +--> Artifact manager
        +--> Process supervisor
        +--> Verification runner
        |
        v
Worker adapter interface
  - ClaudeCodeAdapter
  - CodexAdapter
  - OpenCodeAdapter later
        |
        v
Isolated execution workspace
```

## Control plane and decision layer

### Deterministic control plane

Application code owns:

- Task state
- Dependency resolution
- Worker availability
- Permission enforcement
- Timeout and cancellation
- Retry limits
- Approval gates
- Artifact lifecycle
- Audit trail

### LLM decision layer

Agents may perform:

- Requirement interpretation
- Planning
- Research
- Architecture reasoning
- Implementation
- Semantic review
- Synthesis

LLMs do not grant permissions or approve their own external side effects.

## Trust boundaries

| Input | Trust level |
|---|---|
| System policy | Trusted |
| Versioned agent profile | Trusted |
| Human-approved task contract | Trusted |
| Repository content | Untrusted data |
| Web content | Untrusted data |
| Generated artifacts | Partially trusted |
| Mechanical verifier output | Evidence |

## Worker communication

Workers do not message each other directly.

```text
Worker A
  -> normalized artifact
  -> orchestrator
  -> context builder
  -> worker B
```

## Storage

### PostgreSQL

Planned for projects, tasks, workflow runs, worker runs, approvals, artifact metadata, events, and policy decisions.

### Artifact store

Initial local filesystem, later S3-compatible storage if needed.

### Execution workspaces

Git worktree per write task. Primary source checkout remains unchanged.

## Process reliability

The process supervisor must eventually support:

- stdout and stderr draining
- process group ownership
- heartbeat
- timeout
- graceful cancellation
- force termination
- orphan detection
- restart reconciliation

## Versioning

Each run records:

- workflow version
- agent profile version
- prompt template version
- contract version
- adapter version
- CLI version
- repository commit
