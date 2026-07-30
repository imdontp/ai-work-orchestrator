# Web — the operator dashboard

Empty. Milestone 3 is defined and not started.

**ADR-011 is the scope.** Read it before writing anything here. Three slices, in order:

1. **Run list and run detail** — every run, its state, its nodes, its event trail, and
   its artifacts rendered rather than downloaded.
2. **Approval inbox** — the pending gates across all runs, each showing its approval
   package, with approve, request changes and reject.
3. **Cancel** — stop a run from the interface.

Nothing else. In particular: no task authoring, no config editing, no strategy or
workflow editing, no live log streaming. Submitting a task stays an API call.

**No new backend surface.** `apps/api/app/routers/runs.py` already exposes list, detail,
events, approval package, artifacts, decision and cancel. If a page needs something the
API does not have, that is a signal to question the page, not to add an endpoint.

**Do not expose it beyond `127.0.0.1`** before authentication lands — `docs/BACKLOG.md`
item 5. ADR-011 records why a browser client changes the argument that
`docs/SECURITY_POLICY.md` makes for a single-user local control plane.

## What this file used to say

It listed four pages — Overview, Run Control Center, Approval Inbox, Project Sources and
Workspaces — and named Next.js. ADR-011 supersedes that list, and explicitly rejects the
Overview page: counts of runs by state are the easiest thing to build and the least
useful thing to have. The stack is unsettled rather than decided; Next.js was written
here during Milestone 0, before the pages were narrowed to three.
