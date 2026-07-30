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

## ADR-012: The dashboard may read the workflow graph, and only that

**Decision:** add one endpoint, `GET /api/v1/workflows/{workflow_id}`, returning the
node graph of the configured workflow — ids, dependencies, worker requirement, agent
profile, workspace kind, session policy, approval gates, and the execution order. This
is an amendment to ADR-011's "no new backend surface", not an exception to be extended.

The three M3 slices ship together rather than in sequence: run list and detail, the
approval inbox, and cancel. They were ordered to bound the first delivery, and the
ordering stopped paying once the shell that holds all three existed.

**Reason:** a run record lists the nodes that have *completed*. It cannot say which
nodes exist, how they depend on one another, which is a human gate, which writes to a
worktree, or how many there are. A graph assembled from the event trail can only draw
the past — nodes appear as they start, so the picture grows during the run and never
shows what is still ahead. Progress has no denominator. Neither is a rendering problem
that better client code would solve; the information is not in any response.

The alternative was to drop the graph view. That was rejected because the graph is the
one thing the interface offers that reading `runs/` on disk does not: the shape of the
work, with the current position marked on it.

**The boundary.** This endpoint reads *configuration* — a YAML file the repository
ships, already validated at load time by `orchestrator/workflow/definition.py`. It
touches no run, accepts no parameters beyond the id, and returns nothing that changes
between two calls in the same process. `tests/test_dashboard.py` asserts that a run id,
task id, artifact or task state never appears in its payload. ADR-011's rule stands for
everything else: a page that needs run data the API does not have is a page to question.

**One field was added to an existing response** rather than as a new route:
`RunDetail.sessions`, the worker-to-session map the record already keeps. A session id
identifies a transcript, not a credential.

**What is still not built, deliberately:** live worker log streaming, which ADR-011
rules out and which no endpoint exposes. The dashboard shows the run's event trail
instead — `containment_armed`, `baseline_started`, `state_changed` and the rest. It is
polled every four seconds, and says on the panel that it is the audit trail rather than
worker stdout, so nobody mistakes one for the other.

**What is not shown because the system does not record it:** an estimated completion
time, and who triggered a run. Both are easy to fabricate and were left blank instead.
