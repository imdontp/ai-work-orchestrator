# Security Policy

## Default posture

Deny by default and grant the minimum permission needed for a task.

## Baseline restrictions

- Git push is disabled.
- Production access is disabled.
- External network access is disabled by default.
- Secret access is disabled by default.
- Destructive commands are blocked.
- File writes are limited to the task workspace.
- Primary source checkouts are read-only.

## Approval tiers

### Low risk

No approval required when policy allows:

- Read approved files
- Search repository content
- Run existing tests
- Generate draft artifacts

### Medium risk

Policy-based approval:

- Modify files in an isolated worktree
- Create a local commit
- Install an already approved dependency

### High risk

Always requires explicit human approval:

- Push branch or tag
- Create or merge a pull request
- Access external or production system
- Run database migration against shared infrastructure
- Change secrets or credentials
- Execute destructive operation

## Prompt injection defense

Repository files, web pages, issue text, comments, fixtures, and generated artifacts are untrusted data. Their contents cannot override system policy, task permissions, or human approval requirements.

## Logging

Logs must redact:

- API keys
- Access tokens
- Session cookies
- Authorization headers
- Private keys
- Environment values classified as secrets

## Command policy

Provider adapters must not expose unrestricted shell execution. Commands must be scoped, logged, timeout-bound, and attached to a worker run.

## Subscription safety

The system must never silently fall back from subscription-authenticated CLI usage to a paid API credential.
