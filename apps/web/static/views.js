/* Pages. Each one assembles panels for a route.
 *
 * ADR-011 scopes the dashboard to runs, approvals and cancel. Workers and Policies
 * exist here because the capabilities endpoint already reports them and an operator
 * checking whether git push is blocked should not have to read a .env file — they show
 * configuration, and neither offers a way to change it.
 */

import { ago, badge, el, empty, frag, panel, row, stateBadge, table } from "./dom.js";
import { dag } from "./graph.js";
import {
  approvalPanel,
  artifactsPanel,
  logsPanel,
  overviewPanel,
  workersPanel,
} from "./panels.js";
import { nodeStates } from "./runstate.js";

// ---------------------------------------------------------------------------
// Run list
// ---------------------------------------------------------------------------

export function runsPage(runs) {
  if (runs.length === 0) {
    return panel(
      "Runs",
      { icon: "▤" },
      empty("No runs yet. Submit a task with POST /api/v1/tasks — authoring stays an API call."),
    );
  }

  const rows = runs.map((run) =>
    el("tr", { class: "click", on: { click: () => (location.hash = `#/runs/${run.run_id}`) } }, [
      el("td", {}, [el("a", { class: "mono", href: `#/runs/${run.run_id}`, text: run.run_id })]),
      el("td", {}, [stateBadge(run.task_state, { live: run.advancing })]),
      el("td", { class: "mono", text: run.task_id }),
      el("td", { class: "mono faint", text: run.workflow_id }),
      el("td", { class: "faint" }, [ago(run.created_at)]),
      el("td", { class: "mono", text: String(run.completed_nodes.length) }),
      el("td", {
        class: run.repair_rounds ? "mono warn" : "mono faint",
        text: String(run.repair_rounds),
      }),
      el("td", { class: "wrap" }, [waitingOn(run)]),
    ]),
  );

  return panel(
    "Runs",
    { icon: "▤", badge: badge(`${runs.length} total`, "idle"), flush: true },
    table(["Run", "State", "Task", "Workflow", "Created", "Nodes", "Repairs", "Waiting on"], rows),
  );
}

function waitingOn(run) {
  if (run.awaiting_decision) return badge(run.awaiting_decision, "warn", { live: true });
  if (run.failure) return el("span", { class: "bad", text: run.failure });
  if (run.advancing) return el("span", { class: "faint", text: "working" });
  return el("span", { class: "faint", text: "—" });
}

// ---------------------------------------------------------------------------
// Run detail
// ---------------------------------------------------------------------------

export function runDetailPage(
  { run, events, artifacts, workflow, workflowError, capabilities },
  handlers = {},
) {
  const states = nodeStates(run, workflow, events);

  return el("div", { class: "detail" }, [
    el("div", { class: "area-dag" }, [
      panel(
        "Workflow DAG",
        {
          icon: "⧉",
          badge: workflow
            ? el("span", { class: "mono faint", text: `${workflow.workflow_id} v${workflow.version}` })
            : null,
        },
        workflow
          ? dag(workflow, states)
          : el("p", { class: "notice", text: workflowError ?? "The workflow graph is unavailable." }),
      ),
    ]),

    el("div", { class: "area-cards" }, [
      artifactsPanel(run, artifacts, workflow),
      approvalPanel(run, handlers),
    ]),

    el("div", { class: "area-side" }, [
      workersPanel(run, workflow, capabilities, states),
      overviewPanel(run, workflow),
    ]),

    el("div", { class: "area-logs" }, [logsPanel(events)]),
  ]);
}

// ---------------------------------------------------------------------------
// Approval inbox
// ---------------------------------------------------------------------------

export function approvalsPage(runs) {
  const waiting = runs.filter((run) => run.awaiting_decision);
  if (waiting.length === 0) {
    return panel(
      "Approval Inbox",
      { icon: "⛉", badge: badge("0 waiting", "ok") },
      empty("Nothing is waiting on a human."),
    );
  }

  const cards = waiting.map((run) =>
    el("tr", { class: "click", on: { click: () => (location.hash = `#/runs/${run.run_id}`) } }, [
      el("td", {}, [badge(run.awaiting_decision, "warn", { live: true })]),
      el("td", {}, [el("a", { class: "mono", href: `#/runs/${run.run_id}`, text: run.run_id })]),
      el("td", { class: "mono", text: run.task_id }),
      el("td", { class: "mono faint", text: String(run.completed_nodes.length) }),
      el("td", { class: "faint" }, [ago(run.created_at)]),
      el("td", {}, [el("span", { class: "faint", text: "open to decide →" })]),
    ]),
  );

  return panel(
    "Approval Inbox",
    { icon: "⛉", badge: badge(`${waiting.length} waiting`, "warn"), flush: true },
    table(["Gate", "Run", "Task", "Nodes done", "Waiting since", ""], cards),
  );
}

// ---------------------------------------------------------------------------
// Workers and policies — configuration, read-only
// ---------------------------------------------------------------------------

export function workersPage(capabilities, workflow) {
  if (!capabilities) return panel("Workers", { icon: "⚇" }, empty("Capabilities unavailable."));

  const rows = capabilities.configured_workers.map((name) => {
    const nodes = (workflow?.nodes ?? []).filter((node) => node.worker_requirement === name);
    return el("tr", {}, [
      el("td", { class: "mono", text: name }),
      el("td", { class: "faint", text: nodes.map((n) => n.agent_profile).join(", ") || "—" }),
      el("td", { class: "mono faint", text: nodes.map((n) => n.id).join(", ") || "—" }),
    ]);
  });

  return frag([
    panel(
      "Configured workers",
      { icon: "⚇", badge: badge(`${capabilities.configured_workers.length}`, "idle"), flush: true },
      table(["Adapter", "Profiles it runs", "Nodes"], rows),
    ),
    el("div", { style: "height:16px" }),
    panel(
      "System",
      { icon: "⚙" },
      el("div", { class: "rows" }, [
        row("Milestone", el("span", { class: "mono", text: capabilities.milestone })),
        row("Execution modes", capabilities.execution_modes.join(", ")),
        row("Workspace root", el("span", { class: "mono", text: capabilities.workspace_root })),
        row("Workflow", el("span", { class: "mono", text: workflow?.workflow_id ?? "—" })),
      ]),
    ),
  ]);
}

export function policiesPage(capabilities) {
  const rules = [
    ["git push", capabilities?.git_push_allowed ? "allowed" : "blocked", !capabilities?.git_push_allowed],
    ["network access", capabilities?.network_access_allowed ? "allowed" : "restricted", !capabilities?.network_access_allowed],
    ["write scope", "task worktree only", true],
    ["workspace root", capabilities?.workspace_root ?? "—", true],
  ];

  return frag([
    panel(
      "Guardrails",
      { icon: "⛨", badge: badge("active", "ok") },
      el("div", { class: "rows" }, rules.map(([key, value, good]) =>
        row(key, el("span", { class: good ? "ok mono" : "bad mono", text: String(value) })),
      )),
    ),
    el("div", { style: "height:16px" }),
    panel(
      "What this page is not",
      { icon: "ⓘ" },
      el("p", { class: "faint", style: "margin:0" }, [
        "Read-only. Permissions live in .env and orchestrator/policies/, and ADR-011 keeps " +
          "config editing out of the browser: a mistake made through a UI on a system that " +
          "runs agents with filesystem write access is expensive and quiet.",
      ]),
    ),
  ]);
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export function errorPage(error, { notFound = false } = {}) {
  return panel(
    notFound ? "Not found" : "Cannot load this",
    { icon: "⚠" },
    frag([
      el("p", { class: "error", text: String(error?.message ?? error) }),
      el("p", { style: "margin-top:12px" }, [
        el("a", { href: "#/", text: "← Back to runs" }),
      ]),
    ]),
  );
}
