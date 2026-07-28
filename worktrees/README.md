# This directory is not the workspace root

It is kept only so the name does not get reused by mistake.

Worker worktrees must **not** live inside this repository. The Milestone 1 capability
spike observed a worker writing above its own workspace root while running under that
worker's sandbox flag, so a worktree at `worktrees/<run>/` would put the primary
checkout one `..` away.

`WORKTREE_ROOT` defaults to `~/.ai-work-orchestrator/workspaces` and the application
refuses to start if it is configured to point inside the checkout.

See `docs/DECISIONS.md` ADR-010 and `docs/spikes/M1_CLI_CAPABILITY_REPORT.md` B2.
