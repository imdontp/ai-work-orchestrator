/* Deriving what the graph should show from what the API reports.
 *
 * The run record says which nodes have completed. The event trail says when each one
 * started and stopped. The workflow says which nodes exist at all. None of the three
 * alone can answer "what is happening right now", so this module is where they are
 * combined — kept apart from rendering because it is the part that can be wrong in a
 * way that looks plausible.
 *
 * Nothing here invents a number. A node with no start event has no duration, and a
 * run with no workflow has no progress fraction; both come back null rather than 0.
 */

export const PENDING = "pending";
export const RUNNING = "running";
export const COMPLETED = "completed";
export const FAILED = "failed";
export const WAITING = "waiting";

/**
 * Per-node status, start, finish and duration.
 *
 * Repair rounds re-run a node, so the events are read in order and the last one wins:
 * a node that failed, was retried and then completed reads as completed, with the
 * attempt count kept so the retry is not hidden.
 */
export function nodeStates(run, workflow, events) {
  const states = new Map();
  for (const node of workflow?.nodes ?? []) {
    states.set(node.id, {
      status: PENDING,
      startedAt: null,
      finishedAt: null,
      seconds: null,
      attempts: 0,
      reason: null,
    });
  }

  for (const event of events ?? []) {
    const state = event.node_id ? states.get(event.node_id) : null;
    if (!state) continue;
    if (event.kind === "node_started") {
      state.status = RUNNING;
      state.startedAt = event.at;
      state.finishedAt = null;
      state.seconds = null;
      state.attempts += 1;
    } else if (event.kind === "node_completed") {
      state.status = COMPLETED;
      state.finishedAt = event.at;
      state.seconds = elapsed(state.startedAt, event.at);
    } else if (event.kind === "node_failed") {
      state.status = FAILED;
      state.finishedAt = event.at;
      state.seconds = elapsed(state.startedAt, event.at);
      state.reason = event.detail?.reason ?? null;
    }
  }

  // The record is the authority on what completed; events can be missing for a node
  // the runner satisfies itself, and a run restored in another process still has its
  // completed list.
  for (const nodeId of run?.completed_nodes ?? []) {
    const state = states.get(nodeId);
    if (state && state.status !== COMPLETED) {
      state.status = COMPLETED;
      state.seconds = elapsed(state.startedAt, state.finishedAt);
    }
  }

  const waiting = waitingNode(run, workflow);
  if (waiting && states.has(waiting)) states.get(waiting).status = WAITING;

  // A run that has stopped cannot still have a node running: either it failed there
  // or it was cancelled out from under it.
  if (run && isTerminal(run.task_state) && !run.advancing) {
    for (const state of states.values()) {
      if (state.status === RUNNING) state.status = run.failure ? FAILED : PENDING;
    }
  }

  return states;
}

/** Which node the pending approval belongs to, or null. */
function waitingNode(run, workflow) {
  const approval = run?.pending_approval;
  if (!approval || !workflow) return null;
  // resume_after_node is stripped from the published package, so match on the gate the
  // workflow declares instead. A human node is its own gate.
  //
  // The gate node has necessarily *completed* — approval_after fires once the node is
  // done — so completion is not a reason to skip it. Getting this wrong marked the
  // wrong node as waiting: with the plan gate open after analyze, the graph highlighted
  // final_approval, four nodes further on than where the run had actually stopped.
  const byGate = workflow.nodes.find((node) => node.approval_after === approval.approval_type);
  if (byGate) return byGate.id;
  const human = workflow.nodes.find(
    (node) => node.is_human && !run.completed_nodes.includes(node.id),
  );
  return human ? human.id : null;
}

export function isTerminal(state) {
  return ["COMPLETED", "FAILED_PERMANENT", "CANCELLED"].includes(state);
}

function elapsed(from, to) {
  if (!from || !to) return null;
  const seconds = (new Date(to).getTime() - new Date(from).getTime()) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

/**
 * Completed nodes over total nodes.
 *
 * Null without a workflow rather than a guess: before ADR-012 there was no way to
 * know the denominator, and a percentage with an invented denominator is worse than
 * no percentage.
 */
export function progress(run, workflow) {
  const total = workflow?.nodes?.length ?? 0;
  if (!total) return null;
  const done = (run.completed_nodes ?? []).length;
  return { done, total, percent: Math.round((done / total) * 100) };
}

/**
 * Lay the graph out as rows that read left to right, then right to left.
 *
 * Execution order, not dependency depth: the runner executes in that order, ties
 * broken by declaration, so this is the sequence an operator is actually watching.
 * Branches still appear in the right place because a node cannot be ordered before
 * anything it depends on; what the rows do not draw is a second incoming edge, which
 * the node card names instead.
 */
export function layout(workflow, maxPerRow = 4) {
  const order = workflow.execution_order.filter((id) => workflow.nodes.some((n) => n.id === id));
  const byId = new Map(workflow.nodes.map((node) => [node.id, node]));

  // Balanced rather than greedy: five nodes at four per row gave a full row and a
  // lone card with three empty slots beside it. Fill the same number of rows evenly.
  const rowCount = Math.max(1, Math.ceil(order.length / maxPerRow));
  const perRow = Math.ceil(order.length / rowCount);

  const rows = [];
  for (let index = 0; index < order.length; index += perRow) {
    const slice = order.slice(index, index + perRow).map((id, offset) => ({
      node: byId.get(id),
      number: index + offset + 1,
    }));
    rows.push({ cells: slice, reversed: (index / perRow) % 2 === 1 });
  }
  return { rows, order, perRow };
}
