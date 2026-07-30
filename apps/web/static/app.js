/* Router, refresh loop and the three writes.
 *
 * Polling, not streaming. ADR-011 rules out live log streaming, and a run takes
 * minutes rather than milliseconds, so a periodic GET is both sufficient and what the
 * API was built for — POST /advance returns immediately and tells the caller to poll.
 * Polling stops when the tab is hidden: nobody is reading it, and every poll re-reads
 * run records from disk.
 *
 * Scroll position and open <details> are preserved across a silent refresh. Without
 * that, a four-second poll would close an artifact the moment someone opened it.
 */

import {
  ApiError,
  cancel,
  decide,
  getArtifact,
  getCapabilities,
  getEvents,
  getRun,
  getWorkflow,
  listRuns,
} from "./api.js";
import { badge, el, moreLink, replace, stateBadge, toggle } from "./dom.js";
import { icon } from "./icons.js";
import {
  approvalsPage,
  errorPage,
  policiesPage,
  runDetailPage,
  runsPage,
  workersPage,
} from "./views.js";

const REFRESH_MS = 4000;

const view = document.getElementById("view");
const topbar = document.getElementById("topbar");
const nav = document.getElementById("nav");
const guardrails = document.getElementById("guardrails");

const NAV = [
  { hash: "#/", glyph: "runs", label: "Runs" },
  { hash: "#/approvals", glyph: "approvals", label: "Approvals" },
  { hash: "#/workers", glyph: "workers", label: "Workers" },
  { hash: "#/policies", glyph: "policies", label: "Policies" },
];

//: Dark is the design. The OS preference does not decide what a control room looks
//: like; this does, and it is remembered.
const THEME_KEY = "aiwo.theme";

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* private mode; the choice simply will not persist */
  }
}

let timer = null;
let generation = 0;
let auto = true;
let pendingAction = null;
//: Fetched once - it is configuration, and re-reading it every four seconds would be
//: four requests a minute for a payload that cannot change while the process runs.
let capabilities = null;
let capabilitiesTried = false;

// ---------------------------------------------------------------------------
// Routing
// ---------------------------------------------------------------------------

function route() {
  const hash = location.hash || "#/";
  const detail = hash.match(/^#\/runs\/(.+)$/);
  if (detail) return { name: "detail", runId: decodeURIComponent(detail[1]) };
  if (hash.startsWith("#/approvals")) return { name: "approvals" };
  if (hash.startsWith("#/workers")) return { name: "workers" };
  if (hash.startsWith("#/policies")) return { name: "policies" };
  return { name: "runs" };
}

function renderNav(current) {
  const active =
    current.name === "detail" ? "#/" : NAV.find((item) => item.hash.includes(current.name))?.hash ?? "#/";
  replace(
    nav,
    el(
      "div",
      {},
      NAV.map((item) =>
        el("a", { href: item.hash, class: item.hash === active ? "on" : "" }, [
          el("span", { class: "ico" }, [icon(item.glyph)]),
          item.label,
        ]),
      ),
    ),
  );
}

function renderGuardrails() {
  const rule = (label, good) =>
    el("li", {}, [
      el("span", { class: good ? "ok" : "bad" }, [icon(good ? "check" : "cross", { size: 12 })]),
      label,
    ]);
  replace(
    guardrails,
    el("div", {}, [
      el("h4", {}, [
        el("span", { class: "ok" }, [icon("policies", { size: 14 })]),
        "Guardrails",
        badge("active", "ok"),
      ]),
      el("ul", {}, [
        rule("git push: human approval", !capabilities?.git_push_allowed),
        rule("network access: restricted", !capabilities?.network_access_allowed),
        rule("write scope: task worktree", true),
        rule("containment: orchestrator-owned", true),
        rule("approval required: high risk", true),
      ]),
      moreLink("#/policies", "View all policies"),
    ]),
  );
}

// ---------------------------------------------------------------------------
// Top bar
// ---------------------------------------------------------------------------

function renderTopbar(current, context = {}) {
  const { run } = context;
  const here = NAV.find((n) => n.hash === (current.name === "runs" ? "#/" : `#/${current.name}`));
  const title =
    current.name === "detail"
      ? el("h1", {}, [
          icon("runs", { cls: "hicon", size: 17 }),
          run?.task_id ?? current.runId,
          run ? stateBadge(run.task_state, { live: run.advancing }) : null,
        ])
      : el("h1", {}, [icon(here?.glyph ?? "runs", { cls: "hicon", size: 17 }), here?.label ?? "Runs"]);

  const cancellable = run && !["COMPLETED", "FAILED_PERMANENT", "CANCELLED"].includes(run.task_state);

  replace(
    topbar,
    el("div", { style: "display:flex;align-items:center;gap:12px;width:100%;flex-wrap:wrap" }, [
      title,
      run ? el("span", { class: "sub", text: run.run_id }) : null,
      el("span", { class: "grow" }),
      cancellable
        ? el(
            "button",
            {
              class: "danger",
              disabled: pendingAction ? true : null,
              on: { click: () => onCancel(run) },
            },
            [
              pendingAction === "cancel"
                ? el("span", { class: "spin" })
                : icon("stop", { size: 13 }),
              "Cancel run",
            ],
          )
        : null,
      toggle("auto", "auto-refresh", auto, (event) => {
        auto = event.target.checked;
        schedule();
      }),
      el("button", { class: "ghost", on: { click: () => render({ silent: true }) } }, [
        icon("refresh", { size: 13 }),
        "Refresh",
      ]),
      el(
        "button",
        {
          class: "ghost icon-only",
          title: "Switch theme",
          on: {
            click: () => {
              applyTheme(currentTheme() === "dark" ? "light" : "dark");
              render({ silent: true });
            },
          },
        },
        [icon(currentTheme() === "dark" ? "sun" : "moon", { size: 14 })],
      ),
      el("span", { class: "stamp", id: "stamp" }),
    ]),
  );
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

async function render({ silent = false } = {}) {
  const current = route();
  const mine = ++generation;
  renderNav(current);

  if (!silent) {
    replace(view, el("p", { class: "empty", text: "Loading…" }));
    renderTopbar(current);
  }

  if (!capabilitiesTried) {
    capabilitiesTried = true;
    try {
      capabilities = await getCapabilities();
    } catch {
      capabilities = null;
    }
    renderGuardrails();
  }

  try {
    const memory = remember();
    if (current.name === "detail") {
      const context = await loadDetail(current.runId);
      if (mine !== generation) return;
      renderTopbar(current, context);
      replace(view, runDetailPage(context, { onDecide: (d, r) => onDecide(context.run, d, r), pending: pendingAction }));
    } else if (current.name === "approvals") {
      const runs = await listRuns();
      if (mine !== generation) return;
      renderTopbar(current);
      replace(view, approvalsPage(runs));
    } else if (current.name === "workers") {
      const workflow = await safeWorkflow();
      if (mine !== generation) return;
      renderTopbar(current);
      replace(view, workersPage(capabilities, workflow));
    } else if (current.name === "policies") {
      renderTopbar(current);
      replace(view, policiesPage(capabilities));
    } else {
      const runs = await listRuns();
      if (mine !== generation) return;
      renderTopbar(current);
      replace(view, runsPage(runs));
    }
    restore(memory);
    stampNow();
  } catch (error) {
    if (mine !== generation) return;
    renderTopbar(current);
    replace(view, errorPage(error, { notFound: error instanceof ApiError && error.status === 404 }));
  }
}

async function loadDetail(runId) {
  const [run, events] = await Promise.all([getRun(runId), getEvents(runId)]);
  let workflow = null;
  let workflowError = null;
  try {
    workflow = await getWorkflow(run.workflow_id);
  } catch (error) {
    // A run recorded against a workflow that is no longer configured still has to be
    // readable; the graph panel says why it is missing instead of the page failing.
    workflowError = error instanceof ApiError ? error.message : String(error);
  }
  const artifacts = await loadArtifacts(runId, Object.values(run.artifacts ?? {}));
  return { run, events, artifacts, workflow, workflowError, capabilities };
}

/** Fetch every artifact for a run at once. They are local files and small. */
async function loadArtifacts(runId, names) {
  const loaded = new Map();
  await Promise.all(
    names.map(async (name) => {
      try {
        loaded.set(name, { ok: true, value: await getArtifact(runId, name) });
      } catch (error) {
        loaded.set(name, { ok: false, error: describe(error) });
      }
    }),
  );
  return loaded;
}

async function safeWorkflow() {
  try {
    const runs = await listRuns(1);
    const id = runs[0]?.workflow_id;
    return id ? await getWorkflow(id) : null;
  } catch {
    return null;
  }
}

function describe(error) {
  return error instanceof ApiError ? error.message : String(error);
}

function stampNow() {
  const stamp = document.getElementById("stamp");
  if (stamp) stamp.textContent = new Date().toLocaleTimeString();
}

// ---------------------------------------------------------------------------
// Preserving what the operator was looking at
// ---------------------------------------------------------------------------

function remember() {
  return {
    scroll: view.scrollTop,
    open: [...view.querySelectorAll("details[open] > summary .mono")].map((n) => n.textContent),
    logs: document.getElementById("logbox")?.scrollTop ?? null,
  };
}

function restore(memory) {
  for (const summary of view.querySelectorAll("details > summary .mono")) {
    if (memory.open.includes(summary.textContent)) summary.parentElement.parentElement.open = true;
  }
  view.scrollTop = memory.scroll;
  const box = document.getElementById("logbox");
  if (box) box.scrollTop = memory.logs === null ? box.scrollHeight : memory.logs;
}

// ---------------------------------------------------------------------------
// The three writes
// ---------------------------------------------------------------------------

async function onDecide(run, decision, reason) {
  if (decision !== "approve" && !reason) {
    window.alert(`A reason is required to ${decision.replace("_", " ")}.`);
    return;
  }
  if (decision === "reject" && !window.confirm(`Reject and cancel ${run.run_id}?`)) return;

  pendingAction = decision;
  await render({ silent: true });
  try {
    await decide(run.run_id, decision, reason);
  } catch (error) {
    window.alert(describe(error));
  } finally {
    pendingAction = null;
    await render({ silent: true });
  }
}

async function onCancel(run) {
  const reason = window.prompt(`Cancel ${run.run_id}? The worker is killed, not waited out.\n\nReason:`, "");
  if (reason === null) return;

  pendingAction = "cancel";
  await render({ silent: true });
  try {
    await cancel(run.run_id, reason);
  } catch (error) {
    window.alert(describe(error));
  } finally {
    pendingAction = null;
    // A cancelled run settles on its own task, so the record may still read RUNNING
    // for a moment. The poll picks up the final state.
    await render({ silent: true });
  }
}

// ---------------------------------------------------------------------------
// Loop
// ---------------------------------------------------------------------------

function schedule() {
  if (timer !== null) clearInterval(timer);
  timer = setInterval(() => {
    if (auto && !document.hidden && !pendingAction) render({ silent: true });
  }, REFRESH_MS);
}

window.addEventListener("hashchange", () => render());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && auto) render({ silent: true });
});

let stored = null;
try {
  stored = localStorage.getItem(THEME_KEY);
} catch {
  /* private mode */
}
applyTheme(stored === "light" ? "light" : "dark");

replace(document.getElementById("brandmark"), icon("brand", { size: 17 }));
renderGuardrails();
render();
schedule();
