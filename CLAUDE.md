# CLAUDE.md

Follow `AGENTS.md` as the primary repository instruction.

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
