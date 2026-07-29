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

## ADR-010: Worker sandboxes are not trusted for containment

**Decision:** The orchestrator provides workspace containment itself, in three layers,
and does not rely on a worker CLI's own sandbox flag:

1. The workspace root lives outside the primary checkout, in a dedicated location.
2. Each run's worktree sits alone inside a barrier directory that carries an
   OS-level deny-write rule, so a relative `..` escape fails at the filesystem.
3. Every write-capable run is bracketed by a mechanical scan of the protected paths.
   Any creation, modification or deletion outside the worktree fails the run.

**Reason:** The M1 capability spike observed Codex writing above its workspace root
while running under `-s workspace-write`, both by shelling out to PowerShell and by
its own `apply_patch` step. Claude Code did not write outside its directory, but its
refusal came with `permission_denials: []` — the model declined, nothing enforced it.
Neither worker demonstrated mechanical confinement, so `AGENTS.md` rules 6 and 7
cannot be satisfied by passing a sandbox flag and trusting it.

Prevention and detection are both required because they cover different attacks.
A paired control established that the deny-write barrier stops the relative escape
that was actually observed. It cannot stop a write to an unrelated absolute path,
and that case was never demonstrated to be blocked by anything, so the post-run scan
is what makes the guarantee mechanical rather than assumed.

**Rejected:** container isolation with the worktree as the only writable mount. It is
the stronger mechanism and remains the eventual target, but Docker Desktop on the
target machine has no working engine — it needs WSL2, and no distribution is
installed. Choosing it would have blocked M2 on an environment change.

**Consequence:** `WORKTREE_ROOT` may no longer point inside the repository. The
in-repo `worktrees/` directory is retained only as a signpost; see its README.

**Amendment — the barrier proves itself.** Running the POSIX branch on real Linux
showed the barrier silently doing nothing: as root, `chmod` succeeds and the directory
stays writable, because root ignores mode bits. That is how containers and most CI
run, so the prevention layer would have been absent exactly where it was most likely
to be trusted. `WriteBarrier.apply()` now writes a probe file after applying the rule
and raises if it lands, releasing the useless rule first. A barrier either holds or
refuses to claim it does; there is no state where it reports success and is porous.

The orchestrator must not run as root on POSIX. Detection still works there, but
prevention does not, and `arm()` will refuse rather than proceed on one layer.
