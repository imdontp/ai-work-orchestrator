# Domain Model

## Core concepts

### Project

A logical unit connecting goals, source references, execution workspaces, policies, and workflows.

### Task

A bounded unit of work with objective, inputs, scope, acceptance criteria, permissions, and expected outputs.

### Workflow

A versioned graph describing task dependencies and execution rules.

### Workflow Node

One execution step inside a workflow. It references an agent profile and worker requirements but is not itself an agent or worker.

### Agent Profile

Reusable instructions, capabilities, permissions, tool policy, and output contract for a role such as analyst, implementer, or reviewer.

### Worker Adapter

A provider-specific bridge capable of starting, monitoring, cancelling, and collecting a result from a CLI worker.

### Worker

An available execution target such as Claude Code or Codex.

### Model

The underlying AI model selected by the CLI or provider. It must not be hard-coded into the agent profile domain concept.

### Session

Provider-specific conversational state. New task means new session by default.

### Run

One execution instance of a workflow or direct task.

### Worker Run

One attempt to execute one workflow node through a worker adapter.

### Artifact

A versioned output such as analysis JSON, patch, test result, report, decision record, or log.

### Approval

A human decision authorizing a plan, revision, or external side effect.

## Separation rule

```text
Agent Profile != Worker != Model != Workflow Node != Session != Run
```

## State model

Baseline task states:

- PENDING
- READY
- RUNNING
- VERIFYING
- WAITING_APPROVAL
- COMPLETED
- FAILED_RETRYABLE
- FAILED_PERMANENT
- BLOCKED
- CANCELLED
- INTERRUPTED

## Session policy

- New task: new session
- Repair of same task: resume allowed once when supported
- Independent review: always new session
- Requirement changed materially: new session
- Cross-project session reuse: prohibited
