# Workflow Specification

## Workflow shapes

### Direct

```text
Task -> One worker -> Validate result -> Complete
```

Use for small, low-risk, well-bounded tasks.

### Pipeline

```text
A -> artifact -> B -> artifact -> C
```

Use when one worker's structured output is required by the next step.

### Repair loop

```text
Implement -> Verify -> Review
     ^                  |
     +---- Revise ------+
```

Repair loops are bounded by policy.

### Parallel

Designed for later delivery. Parallel nodes must not share writable resources unless managed by locks or integration ownership.

## Initial vertical slice

1. Intake validates the user task.
2. Analyst produces requirement analysis and plan.
3. Human approves or rejects the plan.
4. Implementer receives approved plan and scoped repository context.
5. System verifier executes configured commands.
6. Independent reviewer receives requirement, diff, test evidence, and relevant files.
7. Repair is requested when findings require changes.
8. Human approves final result or requests changes.

## Handoff rules

Worker B receives:

- Original goal
- Current task contract
- Acceptance criteria
- Relevant source references
- Relevant artifact outputs from prior nodes
- Permissions and prohibited actions
- Expected result schema

Worker B does not receive the entire raw conversation unless explicitly approved for debugging.

## DAG validation rules

- Every node has an expected deliverable.
- Every dependency exists.
- No dependency cycle exists.
- Acceptance criteria are testable or reviewable.
- Parallel write scopes do not overlap.
- Shared resources have an integration owner or lock.
- Every high-risk side effect has an approval node.

## Completion rule

A worker reporting `completed` moves the task to verification, not directly to completed.
