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

## ADR-011: Milestone 3 is a read-mostly operator dashboard

**Decision:** M3 delivers a web dashboard over the existing HTTP surface, in that order:

1. **Run list and run detail** — every run, its state, its nodes, its event trail, and
   its artifacts rendered rather than downloaded.
2. **Approval inbox** — the pending gates across all runs, each showing its approval
   package, with approve, request changes and reject.
3. **Cancel** — stop a run from the interface.

Nothing else. In particular: no task authoring, no config editing, no strategy or
workflow editing, no live log streaming. Submitting a task stays an API call.

**Reason:** the orchestrator's approval gates are the part a human is required for, and
they are currently reachable only by hand-written HTTP calls. That is the friction worth
removing. Everything else the interface could do is either already easy from the API or
is a way to change the system's behaviour from a browser, which is the class of feature
`docs/MVP_SCOPE.md` keeps out for the same reason it keeps the dashboard read-only in
spirit: a mistake made through a UI on a system that runs agents with filesystem write
access is expensive and quiet.

**No new backend surface.** The API already exposes list, detail, events, approval,
artifacts, decision and cancel. If a page needs something the API does not have, that is
a signal to question the page, not to add an endpoint.

**Consequence — the API stops being safe to leave unauthenticated by default.** A
browser client means CORS, and CORS means an origin that is not the terminal the operator
typed in. `docs/SECURITY_POLICY.md` justifies no-authentication for a single-user local
control plane driven from the same machine; a dashboard widens who can reach it. So
`docs/BACKLOG.md` item 5 becomes a prerequisite of shipping the dashboard beyond
localhost, not a someday item — and the dashboard must not be the reason the API gets
exposed before that lands.

**Rejected:** starting with an Overview page of aggregate numbers. Counts of runs by
state are the easiest thing to build and the least useful thing to have; nobody is
blocked on not knowing them. The approval inbox is what a human is actually waiting on.
