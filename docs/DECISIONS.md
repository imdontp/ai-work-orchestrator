# Architecture Decision Log

## ADR-001: Local-first, single-user MVP

**Decision:** Build for one local user before multi-user or cloud deployment.

**Reason:** The primary problem is local CLI-to-CLI handoff. This minimizes security and infrastructure complexity.

## ADR-002: Deterministic orchestrator first

**Decision:** Use application code and an explicit state machine rather than LangGraph or Temporal in V1.

**Reason:** The initial workflow is small, and deterministic execution is easier to debug and validate.

## ADR-003: Structured artifact handoff

**Decision:** Workers communicate through normalized artifacts routed by the orchestrator.

**Reason:** Reduces context noise, improves auditability, and allows worker substitution.

## ADR-004: Separate agent profile from worker and model

**Decision:** Agent profile, worker adapter, model, workflow node, and session are separate domain concepts.

**Reason:** Prevents provider lock-in and preserves routing flexibility.

## ADR-005: Git worktree per write task

**Decision:** Every write task operates in an isolated Git worktree.

**Reason:** Protects the primary checkout and enables bounded parallelism later.

## ADR-006: PostgreSQL target storage

**Decision:** Target PostgreSQL rather than SQLite.

**Reason:** The product requires concurrent task updates, locking, worker heartbeat, event history, and future queue behavior.

## ADR-007: Human approval is risk-tiered

**Decision:** Require approval for meaningful risk rather than every action.

**Reason:** Prevent approval fatigue while preserving control.

## ADR-008: Claude Code and Codex first

**Decision:** Validate Claude Code and Codex before adding OpenCode.

**Reason:** Fewer variables make failures easier to isolate.

## ADR-009: Dashboard after core reliability

**Decision:** Build the orchestration vertical slice before the full web dashboard.

**Reason:** The major technical risk is reliable CLI automation, not interface design.
