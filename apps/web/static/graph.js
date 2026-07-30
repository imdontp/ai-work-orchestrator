/* The workflow DAG.
 *
 * Rendered from the workflow definition rather than from what has happened, so a node
 * that has not been reached is drawn as pending instead of being absent. That is the
 * whole reason ADR-012 exposes the graph: a picture assembled from the event trail can
 * only ever draw the past.
 */

import { duration, el, workerTag } from "./dom.js";
import { icon } from "./icons.js";
import { COMPLETED, FAILED, RUNNING, WAITING, layout } from "./runstate.js";

const MARK = {
  [COMPLETED]: { glyph: "check", label: "Completed" },
  [RUNNING]: { glyph: "refresh", label: "In Progress" },
  [FAILED]: { glyph: "cross", label: "Failed" },
  [WAITING]: { glyph: "pause", label: "Awaiting approval" },
};

const PER_ROW = 4;

export function dag(workflow, states) {
  const { rows, perRow } = layout(workflow, PER_ROW);
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

    // Pad short rows so every card is the same width. A row holding one node used to
    // stretch it across the whole panel.
    for (let pad = row.cells.length; pad < perRow; pad += 1) {
      cells.push(el("div", { class: "link" }));
      cells.push(el("div", { class: "node spacer", "aria-hidden": "true" }));
    }

    parts.push(el("div", { class: `dag-row${row.reversed ? " rtl" : ""}` }, cells));

    const next = rows[rowIndex + 1];
    if (next) {
      const from = row.cells[row.cells.length - 1];
      const live = isActiveEdge(states, from.node.id, next.cells[0].node.id);
      // The chain leaves a left-to-right row at its right edge and a reversed row at
      // its left, so the stem sits under the end it actually leaves from.
      parts.push(
        el("div", { class: `turn ${row.reversed ? "left" : "right"}${live ? " active" : ""}` }, [
          el("div", { class: "stem" }, [icon("arrowDown", { size: 18 })]),
        ]),
      );
    }
  });

  return el("div", { class: "dag" }, [
    el("div", { class: "dag-scale", id: "dagscale" }, parts),
    legend(),
  ]);
}

function connector(states, fromId, toId, reversed) {
  const live = isActiveEdge(states, fromId, toId);
  return el("div", { class: `link${live ? " active" : ""}` }, [
    icon(reversed ? "arrowLeft" : "arrowRight", { size: 18 }),
  ]);
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
      node.is_human ? icon("human", { size: 13 }) : null,
      node.needs_worktree ? icon("worktree", { size: 13 }) : null,
    ]),
    el("div", { class: "title", text: node.id }),
    el("div", { class: "foot" }, [
      el("span", { class: "st" }, [
        icon(mark ? mark.glyph : "pending", { size: 12 }),
        mark ? mark.label : "Pending",
      ]),
      state?.seconds != null ? el("span", { text: duration(state.seconds) }) : null,
    ]),
    el("div", { class: "foot" }, [
      workerTag(node.worker_requirement),
      state?.attempts > 1 ? el("span", { class: "warn", text: `${state.attempts}×` }) : null,
    ]),
    undrawn.length ? el("div", { class: "also", text: `also after ${undrawn.join(", ")}` }) : null,
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
    el("span", {}, [icon("human", { size: 12 }), "human gate"]),
    el("span", {}, [icon("worktree", { size: 12 }), "isolated worktree"]),
  ]);
}
