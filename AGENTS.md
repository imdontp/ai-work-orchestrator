# AGENTS.md

This repository implements a local-first Personal AI Work Orchestrator.

## Mission

Build a reliable bridge between CLI agents. The platform must coordinate Claude Code, Codex, and later OpenCode without requiring the user to copy and paste context between terminals.

## Non-negotiable architecture rules

1. Keep these concepts separate:
   - Agent Profile
   - Worker Adapter
   - Model
   - Workflow Node
   - Session
   - Run
2. Worker-to-worker handoff must go through the orchestrator.
3. Handoffs use structured, versioned artifacts.
4. Do not forward complete raw transcripts by default.
5. Do not trust worker-reported success without mechanical verification.
6. Do not write to the user's primary source checkout.
7. Write tasks must execute in isolated worktrees or equivalent sandboxes.
8. No automatic Git push, PR creation, deployment, production access, or destructive command.
9. High-risk effects require explicit human approval.
10. Retry loops must be bounded.

## Current milestone

Milestone 0 foundation. The next milestone is a CLI capability spike, not full feature development.

## Allowed work in Milestone 0

- Improve documentation
- Refine domain models and schemas
- Add tests for deterministic behavior
- Improve API health and metadata endpoints
- Add non-provider-specific execution abstractions

## Out of scope until capability spike is approved

- Guessing Claude Code or Codex flags
- Creating provider-specific command invocations without testing them locally
- Adding autonomous workflow generation
- Adding unrestricted shell access
- Adding multi-user features
- Adding cloud deployment or Kubernetes
- Adding self-modifying prompts or agents

## Engineering expectations

- Prefer small, reversible changes.
- Add or update tests with behavior changes.
- Preserve backward compatibility of published contracts unless versioned.
- Treat logs as potentially sensitive and redact credentials.
- Make failure states explicit.
- Use typed Python.
- Avoid hidden fallback from subscription workers to paid APIs.

## Human approval boundaries

Always stop before:

- Installing dependencies outside the approved project environment
- Modifying external systems
- Pushing Git branches or tags
- Opening or merging pull requests
- Accessing production or corporate systems
- Reading secrets not explicitly provided for the task

## Definition of done for a change

- Scope matches the assigned task.
- Tests pass.
- Contracts remain valid.
- Security boundaries remain intact.
- Assumptions and limitations are documented.
- No external side effect occurred without approval.
