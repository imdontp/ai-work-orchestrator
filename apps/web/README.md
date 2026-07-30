# Web — the operator dashboard

Built. Served by the FastAPI app at **`/dashboard`**, so `make run` is all it takes:

```
http://127.0.0.1:8000/dashboard/
```

Source and its rules are in `static/README.md`. Scope is **ADR-011**, amended by
**ADR-012**.

## What it does

- **Runs** — every run, its state, repair rounds, and what it is waiting on.
- **Run detail** — the workflow DAG with the current position marked, the approval
  package, artifacts rendered from JSON rather than downloaded, the workers and their
  sessions, and the run's event trail.
- **Approvals** — the pending gates across all runs; approve, request changes or reject.
- **Cancel** — stop any non-terminal run.
- **Workers** and **Policies** — configuration, read-only.

## What it deliberately does not do

No task authoring, no config or workflow editing, no live worker log streaming. The
event trail stands in for logs and says so on the panel. Submitting a task stays an API
call. ADR-011 records why: a mistake made through a UI on a system that runs agents with
filesystem write access is expensive and quiet.

Nothing is fabricated. There is no estimated completion time and no "triggered by",
because the control plane records neither.

## Do not expose it beyond `127.0.0.1`

The API has no authentication. `docs/BACKLOG.md` item 5 is the prerequisite, and the
dashboard must not be the reason the API gets exposed before it lands.
