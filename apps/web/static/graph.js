/* The workflow DAG.
 *
 * Rendered from the workflow definition rather than from what has happened, so a node
 * that has not been reached is drawn as pending instead of being absent. That is the
 * whole reason ADR-012 exposes the graph: a picture assembled from the event trail can
 * only ever draw the past.
 */

import { duration, el, workerTag } from "./dom.js";
import { COMPLETED, FAILED, RUNNING, WAITING, layout } from "./runstate.js";

const MARK = {
  [COMPLETED]: { glyph: "✓", label: "Completed" },
  [RUNNING]: { glyph: "◐", label: "In Progress" },
  [FAILED]: { glyph: "✕", label: "Failed" },
  [WAITING]: { glyph: "⏸", label: "Awaiting approval" },
};

export function dag(workflow, states) {
  const { rows } = layout(workflow);
  const parts = [];

  rows.forEach((row, rowIndex) => {
    const cells = [];
    row.cells.forEach((cell, cellIndex) => {
      if (cellIndex > 0) {
        const previous = row.cells[cellIndex - 1].node;
        cells.push(connector(states, previous.id, cell.node.id, row.reversed));
      }
      cells.push(nodeCard(cell.node, cell.number, states.get(cell.node.id), workflow));
    });
    parts.push(el("div", { class: `dag-row${row.reversed ? " rtl" : ""}` }, cells));

    const next = rows[rowIndex + 1];
    if (next) {
      const from = row.cells[row.cells.length - 1];
      const to = next.cells[0];
      const live = isActiveEdge(states, from.node.id, to.node.id);
      parts.push(
        el("div", { class: `turn${row.reversed ? " left" : ""}${live ? " active" : ""}` }, ["↓"]),
      );
    }
  });

  return el("div", { class: "dag" }, [...parts, legend()]);
}

function connector(states, fromId, toId, reversed) {
  const live = isActiveEdge(states, fromId, toId);
  return el("div", { class: `link${live ? " active" : ""}`, text: reversed ? "←" : "→" });
}

/** An edge is live when its source is done and its target is the one being worked on. */
function isActiveEdge(states, fromId, toId) {
  const from = states.get(fromId);
  const to = states.get(toId);
  if (!from || !to) return false;
  return from.status === COMPLETED && [RUNNING, WAITING].includes(to.status);
}

function nodeCard(node, number, state, workflow) {
  const status = state?.status ?? "pending";
  const mark = MARK[status];
  // A dependency the serpentine row does not draw, so a branch is not silently lost.
  const undrawn = node.depends_on.filter(
    (dep) => workflow.execution_order.indexOf(dep) !== workflow.execution_order.indexOf(node.id) - 1,
  );

  return el("div", { class: `node ${status}`, title: describe(node, state) }, [
    el("div", { class: "top" }, [
      el("span", { class: "num", text: String(number) }),
      el("span", { class: "grow" }),
      node.is_human ? el("span", { text: "☖" }) : null,
      node.needs_worktree ? el("span", { title: "isolated worktree", text: "⌘" }) : null,
    ]),
    el("div", { class: "title", text: node.id }),
    el("div", { class: "foot" }, [
      el("span", { class: "st" }, [
        el("span", { text: mark ? mark.glyph : "○" }),
        mark ? mark.label : "Pending",
      ]),
      el("span", { text: state?.seconds != null ? duration(state.seconds) : "" }),
    ]),
    el("div", { class: "foot" }, [
      workerTag(node.worker_requirement),
      state?.attempts > 1 ? el("span", { class: "warn", text: `${state.attempts} attempts` }) : null,
    ]),
    undrawn.length
      ? el("div", { class: "foot faint", text: `also after ${undrawn.join(", ")}` })
      : null,
  ]);
}

function describe(node, state) {
  const lines = [
    `${node.id} — ${node.agent_profile}`,
    `worker: ${node.worker_requirement}`,
    `workspace: ${node.workspace}`,
    `session: ${node.session_policy}`,
    `artifact: ${node.expected_artifact}`,
  ];
  if (node.approval_after) lines.push(`approval after: ${node.approval_after}`);
  if (state?.reason) lines.push(`reason: ${state.reason}`);
  return lines.join("\n");
}

function legend() {
  const swatch = (colour, label) =>
    el("span", {}, [el("span", { class: "swatch", style: `background:${colour}` }), label]);
  return el("div", { class: "legend" }, [
    swatch("var(--ok)", "Completed"),
    swatch("var(--accent)", "In Progress"),
    swatch("var(--warn)", "Awaiting approval"),
    swatch("var(--line)", "Pending"),
    el("span", { class: "faint" }, ["☖ human gate · ⌘ isolated worktree"]),
  ]);
}
