/* The panels that make up a run detail page.
 *
 * Each one is a pure function from API payloads to DOM. Anything a panel cannot know
 * is shown as "—" rather than filled in: there is no estimated completion time and no
 * "triggered by", because the control plane records neither and a dashboard that
 * invents them is worse than one that admits the gap.
 */

import { artifactUrl } from "./api.js";
import {
  ago,
  badge,
  clock,
  el,
  empty,
  frag,
  moreLink,
  panel,
  row,
  stateBadge,
  table,
  time,
  workerTag,
} from "./dom.js";
import { icon } from "./icons.js";
import { RUNNING, progress } from "./runstate.js";

/** Past this, rendering a JSON tree costs more than it is worth. */
const MAX_RENDERED_CHARS = 200_000;

// ---------------------------------------------------------------------------
// Run overview
// ---------------------------------------------------------------------------

export function overviewPanel(run, workflow) {
  const bar = progress(run, workflow);
  return panel(
    "Run Overview",
    { glyph: "clock", badge: el("span", { class: "mono faint", text: run.run_id }) },
    el("div", { class: "rows" }, [
      row("Started", frag([time(run.created_at), el("span", { class: "faint", text: ` · ${ago(run.created_at)}` })])),
      row("Task", el("span", { class: "mono", text: run.task_id })),
      row("Workflow", el("span", { class: "mono", text: `${run.workflow_id}` })),
      row("State", stateBadge(run.task_state, { live: run.advancing })),
      row(
        "Progress",
        bar
          ? el("span", { style: "display:flex;align-items:center;gap:8px;min-width:150px" }, [
              el("span", { class: "bar" }, [el("i", { style: `width:${bar.percent}%` })]),
              el("span", { class: "mono faint", text: `${bar.done}/${bar.total}` }),
            ])
          : el("span", { class: "faint", text: "workflow graph unavailable" }),
      ),
      row(
        "Repair rounds",
        el("span", {
          class: run.repair_rounds > 0 ? "warn mono" : "mono",
          text: workflow ? `${run.repair_rounds} / ${workflow.max_repair_rounds}` : String(run.repair_rounds),
        }),
      ),
      run.worktree
        ? row("Worktree", el("span", { class: "mono", text: run.worktree.branch ?? "—" }))
        : null,
      run.awaiting_decision
        ? row("Awaiting", badge(run.awaiting_decision, "warn", { live: true }))
        : null,
      run.failure ? row("Failure", el("span", { class: "bad", text: run.failure })) : null,
    ]),
  );
}

// ---------------------------------------------------------------------------
// Workers
// ---------------------------------------------------------------------------

export function workersPanel(run, workflow, capabilities, states) {
  const configured = capabilities?.configured_workers ?? [];
  const sessions = run?.sessions ?? {};

  const cards = configured.map((name) => {
    const nodes = (workflow?.nodes ?? []).filter((node) => node.worker_requirement === name);
    const active = nodes.find((node) => states?.get(node.id)?.status === RUNNING);
    return el("div", { class: "worker" }, [
      el("div", { class: "head" }, [
        el("div", { class: `avatar ${kindOf(name)}`, text: initials(name) }),
        el("div", {}, [el("div", { class: "nm", text: name })]),
        el("span", { class: "grow" }),
        active ? badge("Running", "run", { live: true }) : badge("Idle", "idle"),
      ]),
      el("div", { class: "rows" }, [
        row("Role", nodes.length ? nodes.map((n) => n.agent_profile).join(", ") : "—"),
        row("Session", el("span", { class: "mono", text: sessions[name] ?? "—" })),
        row("Node", el("span", { class: "mono", text: active ? active.id : "—" })),
      ]),
    ]);
  });

  return panel(
    "Workers",
    {
      glyph: "workers",
      badge: badge(`${configured.length} configured`, configured.length ? "ok" : "idle"),
      foot: moreLink("#/workers", "View all workers"),
    },
    configured.length ? frag(cards) : empty("No worker adapters are configured."),
  );
}

function kindOf(name) {
  if (name.startsWith("claude")) return "claude";
  if (name.startsWith("codex")) return "codex";
  return "other";
}

function initials(name) {
  return name
    .split(/[_\-\s]/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

// ---------------------------------------------------------------------------
// Approval package
// ---------------------------------------------------------------------------

const RISK_TONE = { high: "bad", medium: "warn", low: "ok" };

export function approvalPanel(run, { onDecide, pending } = {}) {
  const approval = run.pending_approval;
  if (!approval) {
    return panel(
      "Approval Package",
      { glyph: "approvals" },
      empty(
        run.awaiting_decision
          ? "This run is waiting, but the package could not be read."
          : "Nothing is waiting on a human right now.",
      ),
    );
  }

  const reason = el("textarea", {
    placeholder: "Reason — required to request changes or reject, kept in the run record.",
  });
  const busy = Boolean(pending);

  const act = (decision) => () => onDecide?.(decision, reason.value.trim());

  return panel(
    "Approval Package",
    {
      glyph: "approvals",
      badge: badge(
        `${approval.risk_level} risk`,
        RISK_TONE[String(approval.risk_level).toLowerCase()] ?? "warn",
      ),
    },
    frag([
      el("div", { class: "rows labelled" }, [
        row("Gate", el("span", { class: "mono", text: approval.approval_type })),
        row("What changed", el("span", { text: approval.summary || "—" })),
        row("Files affected", `${approval.changes?.length ?? 0} file(s)`),
      ]),
      // Collapsed by default. The four rows above are the decision; these are the
      // backing detail, and expanded they pushed the buttons - and the run's event
      // trail below them - off a laptop screen.
      foldout("Changes", approval.changes),
      foldout("Evidence", approval.evidence),
      foldout("Risks", approval.risks, { open: true }),
      reason,
      el("div", { class: "actions" }, [
        el("button", { class: "primary", disabled: busy || null, on: { click: act("approve") } }, [
          busy === "approve" ? el("span", { class: "spin" }) : icon("check", { size: 14 }),
          "Approve",
        ]),
        el("button", { disabled: busy || null, on: { click: act("request_changes") } }, [
          "Request Changes",
        ]),
        el("button", { class: "danger", disabled: busy || null, on: { click: act("reject") } }, [
          "Reject",
        ]),
      ]),
      el("p", { class: "faint", style: "font-size:11.5px;margin:10px 0 0" }, [
        "Rejecting cancels the run. The reason is kept as the run's failure.",
      ]),
    ]),
  );
}

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

export function artifactsPanel(run, artifacts, workflow) {
  const entries = Object.entries(run.artifacts ?? {});
  if (entries.length === 0) {
    return panel(
      "Artifacts",
      { glyph: "artifact", badge: badge("0 items", "idle") },
      empty("No artifact has been produced yet."),
    );
  }

  const rows = entries.map(([nodeId, name]) => {
    const loaded = artifacts.get(name);
    const node = workflow?.nodes?.find((n) => n.id === nodeId);
    const size = loaded?.ok ? JSON.stringify(loaded.value).length : null;
    return el("details", { class: "art" }, [
      el("summary", {}, [
        el("span", { class: "mono", style: "flex:1 1 auto", text: name }),
        el("span", { style: "flex:0 0 52px" }, [el("span", { class: "tag", text: extension(name) })]),
        el("span", {
          class: "faint mono",
          style: "flex:0 0 68px",
          text: size ? bytes(size) : "—",
        }),
        el("span", { style: "flex:0 0 120px" }, [
          node ? workerTag(node.worker_requirement) : el("span", { class: "tag", text: nodeId }),
        ]),
        loaded && !loaded.ok ? el("span", { class: "bad", text: "unreadable" }) : null,
      ]),
      el("div", { class: "inner" }, [artifactBody(run, name, loaded)]),
    ]);
  });

  const head = el("div", { class: "art-head" }, [
    el("span", { style: "flex:1 1 auto", text: "Name" }),
    el("span", { style: "flex:0 0 52px", text: "Type" }),
    el("span", { style: "flex:0 0 68px", text: "Size" }),
    el("span", { style: "flex:0 0 120px", text: "Source" }),
  ]);

  return panel(
    "Artifacts",
    { glyph: "artifact", badge: badge(`${entries.length} items`, "idle"), flush: true },
    frag([head, ...rows]),
  );
}

function extension(name) {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "FILE" : name.slice(dot + 1).toUpperCase();
}

function bytes(count) {
  return count < 1024 ? `${count} B` : `${(count / 1024).toFixed(1)} KB`;
}

function artifactBody(run, name, loaded) {
  if (!loaded) return el("p", { class: "faint", text: "Loading…" });
  if (!loaded.ok) {
    return frag([
      el("p", { class: "error", text: loaded.error }),
      el("p", {}, [el("a", { href: artifactUrl(run.run_id, name), text: "Open the raw JSON" })]),
    ]);
  }
  const payload = loaded.value;
  const size = JSON.stringify(payload)?.length ?? 0;
  if (size > MAX_RENDERED_CHARS) {
    return frag([
      el("p", { class: "faint", text: `${bytes(size)} — too large to render here.` }),
      el("p", {}, [el("a", { href: artifactUrl(run.run_id, name), text: "Open the raw JSON" })]),
    ]);
  }
  return frag([
    artifactView(payload),
    el("p", { style: "margin:12px 0 0" }, [
      el("a", { class: "faint", href: artifactUrl(run.run_id, name), text: "raw JSON ↗" }),
    ]),
  ]);
}

/** Pick a renderer by shape. An unrecognised artifact still renders generically. */
export function artifactView(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return renderValue(payload);
  if ("verified" in payload && "commands" in payload) return verificationView(payload);
  if ("verdict" in payload && "findings" in payload) return reviewView(payload);
  if ("plan" in payload && "observed_facts" in payload) return analysisView(payload);
  if ("status" in payload && "verification" in payload) return workerResultView(payload);
  return renderValue(payload);
}

/* Verification -------------------------------------------------------------
 *
 * Four separate facts that a summary would flatten into one, and the whole reason
 * contracts/verification-result.schema.json names the worker's field
 * "claimed_passed": what the worker said, what the orchestrator concluded, what the
 * base revision already did, and what actually regressed. Run 7 recorded
 * claimed_passed false and verified true at the same time, correctly.
 */
function verificationView(payload) {
  const commands = Array.isArray(payload.commands) ? payload.commands : [];
  const baseline = payload.baseline;
  const regressions = Array.isArray(payload.regressions) ? payload.regressions : [];
  // The runner records a regression as a copy of the command dict rather than an
  // index, so match on the argv it ran.
  const regressed = new Set(regressions.map((command) => JSON.stringify(command.args)));

  const rows = commands.map((command, index) => {
    const argv = Array.isArray(command.args) ? command.args.join(" ") : "—";
    const base = Array.isArray(baseline) ? baseline[index] : undefined;
    const failed = command.exit_code !== 0 || command.timed_out;
    return el("tr", {}, [
      el("td", { class: "mono wrap", text: argv }),
      el("td", {
        class: failed ? "mono bad" : "mono ok",
        text: command.timed_out ? "timed out" : String(command.exit_code ?? "—"),
      }),
      el("td", { class: "mono faint", text: base === undefined ? "—" : String(base) }),
      el("td", {}, [
        regressed.has(JSON.stringify(command.args))
          ? el("span", { class: "bad", text: "regression" })
          : el("span", { class: "faint", text: "—" }),
      ]),
    ]);
  });

  return frag([
    el("div", { class: "facts" }, [
      factCard("Worker claimed", payload.claimed_passed ? "passed" : "did not pass", payload.claimed_passed ? "ok" : "bad", "what the worker said"),
      factCard("Orchestrator verified", payload.verified ? "verified" : "not verified", payload.verified ? "ok" : "bad", "what re-running the commands concluded"),
      factCard(
        "Base revision",
        Array.isArray(baseline) ? `exit ${baseline.join(", ")}` : "not captured",
        Array.isArray(baseline) ? "warn" : "faint",
        Array.isArray(baseline) ? "the suite before this run touched it" : "captured only when verification fails",
      ),
      factCard(
        "Regressions",
        regressions.length === 0 ? "none" : String(regressions.length),
        regressions.length === 0 ? "ok" : "bad",
        "commands this run broke",
      ),
    ]),
    payload.reason ? el("p", { class: "notice", text: payload.reason }) : null,
    commands.length
      ? frag([el("h3", { text: "Commands" }), table(["Command", "Exit", "Base", "Regression"], rows)])
      : null,
    listBlock("Evidence", payload.evidence_paths),
  ]);
}

function reviewView(payload) {
  const findings = Array.isArray(payload.findings) ? payload.findings : [];
  return frag([
    el("div", { class: "facts" }, [
      factCard("Verdict", String(payload.verdict), payload.verdict === "pass" ? "ok" : "bad"),
      factCard("Findings", String(findings.length), findings.length ? "warn" : "ok"),
      factCard("Confidence", String(payload.confidence ?? "—"), "faint"),
    ]),
    findings.length
      ? frag([
          el("h3", { text: "Findings" }),
          table(
            ["Severity", "Finding", "Required action"],
            findings.map((finding) =>
              el("tr", {}, [
                el("td", {}, [
                  el("span", { class: `tag ${severityTone(finding.severity)}`, text: String(finding.severity ?? "—") }),
                ]),
                el("td", { class: "wrap", text: String(finding.finding ?? "") }),
                el("td", { class: "wrap faint", text: String(finding.required_action ?? "—") }),
              ]),
            ),
          ),
        ])
      : el("p", { class: "faint", text: "No findings." }),
    listBlock("Residual risks", payload.residual_risks),
  ]);
}

function severityTone(severity) {
  return ["critical", "high"].includes(severity) ? "human" : severity === "info" ? "tool" : "";
}

function analysisView(payload) {
  return frag([
    payload.summary ? el("p", { text: String(payload.summary) }) : null,
    listBlock("Plan", payload.plan),
    listBlock("Observed facts", payload.observed_facts),
    listBlock("Inferences", payload.inferences),
    listBlock("Assumptions", payload.assumptions),
    listBlock("Open questions", payload.open_questions),
    listBlock("Risks", payload.risks),
  ]);
}

function workerResultView(payload) {
  const verification = payload.verification ?? {};
  return frag([
    el("div", { class: "facts" }, [
      factCard("Status", String(payload.status), payload.status === "completed" ? "ok" : "warn"),
      factCard("Worker", String(payload.worker ?? "—"), "faint"),
      factCard(
        "Claimed passed",
        verification.claimed_passed === undefined ? "not stated" : String(verification.claimed_passed),
        verification.claimed_passed ? "ok" : "bad",
        "a claim, re-checked by the verify node",
      ),
    ]),
    payload.summary ? el("p", { text: String(payload.summary) }) : null,
    listBlock("Files changed", payload.files_changed),
    listBlock("Decisions", payload.decisions),
    listBlock("Assumptions", payload.assumptions),
    listBlock("Risks", payload.risks),
  ]);
}

function factCard(key, value, tone, note) {
  return el("div", { class: "fact" }, [
    el("div", { class: "k", text: key }),
    el("div", { class: `v ${tone ?? ""}`, text: value }),
    note ? el("div", { class: "n", text: note }) : null,
  ]);
}

/** A collapsed list with its count in the summary, so nothing is hidden silently. */
function foldout(title, items, { open = false } = {}) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return el("details", { class: "fold", open: open || null }, [
    el("summary", {}, [title, el("span", { class: "tag", text: String(items.length) })]),
    el(
      "ul",
      { class: "plain tight" },
      items.map((item) =>
        el("li", {}, [typeof item === "object" && item !== null ? renderValue(item) : String(item)]),
      ),
    ),
  ]);
}

function listBlock(title, items) {
  if (!Array.isArray(items) || items.length === 0) return null;
  return frag([
    el("h3", { text: title }),
    el(
      "ul",
      { class: "plain" },
      items.map((item) =>
        el("li", {}, [typeof item === "object" && item !== null ? renderValue(item) : String(item)]),
      ),
    ),
  ]);
}

// ---------------------------------------------------------------------------
// Event trail, shown as logs
// ---------------------------------------------------------------------------

/** Which colour an event kind reads as. Not a severity — a source. */
function eventTone(kind) {
  if (kind.startsWith("run_")) return "k-run";
  if (kind.startsWith("node_") || kind.startsWith("worker_")) return "k-node";
  if (kind.startsWith("state_")) return "k-state";
  if (kind.startsWith("verification") || kind.startsWith("baseline")) return "k-verify";
  if (kind.startsWith("containment") || kind.startsWith("worktree")) return "k-guard";
  if (kind.includes("fail") || kind.includes("violation")) return "k-fail";
  return "";
}

export function logsPanel(events, { onClear } = {}) {
  const lines = events.map((event) =>
    el("div", { class: "logline" }, [
      el("span", { class: "t", text: clock(event.at) }),
      el("span", { class: `src ${eventTone(event.kind)}`, text: `[${event.kind.toUpperCase()}]` }),
      el("span", { class: "msg", text: message(event) }),
    ]),
  );

  const box = el("div", { class: "logs", id: "logbox" }, lines.length ? lines : [
    el("div", { class: "faint", text: "No events recorded." }),
  ]);

  return panel(
    "Run Logs",
    {
      glyph: "terminal",
      badge: badge(`${events.length} events`, "idle"),
      actions: [
        el("span", {
          class: "faint",
          style: "font-size:11.5px",
          text: "the run's audit trail, not worker stdout",
        }),
      ],
      flush: true,
    },
    box,
  );
}

function message(event) {
  const parts = [];
  if (event.node_id) parts.push(event.node_id);
  const detail = Object.entries(event.detail ?? {})
    .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : value}`)
    .join(" ");
  if (detail) parts.push(detail);
  return parts.join("  ") || "—";
}

// ---------------------------------------------------------------------------
// Generic JSON
// ---------------------------------------------------------------------------

export function renderValue(value) {
  if (value === null || value === undefined) return el("span", { class: "faint", text: "—" });
  if (typeof value === "boolean")
    return el("span", { class: value ? "ok" : "bad", text: String(value) });
  if (typeof value === "number") return el("span", { class: "mono", text: String(value) });
  if (typeof value === "string") {
    return value.includes("\n") || value.length > 160
      ? el("pre", { text: value })
      : el("span", { text: value });
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return el("span", { class: "faint", text: "[]" });
    return el("ul", { class: "plain" }, value.map((item) => el("li", {}, [renderValue(item)])));
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return el("span", { class: "faint", text: "{}" });
  return el(
    "dl",
    { class: "kv" },
    entries.map(([key, item]) => frag([el("dt", { text: key }), el("dd", {}, [renderValue(item)])])),
  );
}
