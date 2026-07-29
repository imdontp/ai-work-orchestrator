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

## API exposure

The HTTP API has **no authentication**. Anyone who can reach it can submit a task, and
therefore cause CLI workers to run against `PROJECT_ROOT` and spend the machine's
subscription quota. It can also approve its own runs, which defeats every approval gate
in the system.

This is deliberate for the single-user local MVP — `docs/MVP_SCOPE.md` puts multi-user
features out of scope, and authentication without a user model is theatre. It is safe
only because of where the API listens:

- `APP_HOST` defaults to `127.0.0.1`. Changing it to `0.0.0.0` publishes an unauthenticated
  control plane for CLI agents onto the network.
- Do not put the API behind a tunnel, reverse proxy or port forward without adding
  authentication first.
- Do not run it on a shared or multi-user machine.

Authentication is a precondition for any deployment that is not a single user on
localhost, not an enhancement to add afterwards.

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
