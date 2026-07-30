/* DOM helpers.
 *
 * Everything this page displays comes from an LLM worker's artifact, a repository
 * path, or an operator's own reason string. None of it is trusted markup, so text is
 * set with textContent and this module deliberately exposes no way to assign
 * innerHTML. A worker that emits "<img onerror=...>" in a summary is a plausible
 * accident, not an exotic attack, and the control plane it would be scripting runs
 * CLI agents with filesystem write access.
 */

import { icon } from "./icons.js";

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "on")
      for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    else if (key === "data") for (const [k, v] of Object.entries(value)) node.dataset[k] = v;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" || typeof child === "number" ? String(child) : child);
  }
  return node;
}

export function frag(children) {
  const f = document.createDocumentFragment();
  for (const child of [].concat(children)) if (child) f.append(child);
  return f;
}

export function replace(host, content) {
  host.replaceChildren();
  if (content) host.append(content);
}

/** A titled box. Every region of the page is one of these. */
export function panel(title, options = {}, body) {
  const { badge, actions, flush = false, area, glyph, foot } = options;
  return el("section", { class: `panel${area ? ` area-${area}` : ""}` }, [
    el("header", {}, [
      el("h2", {}, [glyph ? icon(glyph, { cls: "hicon" }) : null, title]),
      badge || null,
      el("span", { class: "grow" }),
      ...[].concat(actions || []).filter(Boolean),
    ]),
    el("div", { class: `body${flush ? " flush" : ""}` }, [body]),
    foot ? el("div", { class: "panel-foot" }, [foot]) : null,
  ]);
}

/** A "View all X →" line at the bottom of a panel. */
export function moreLink(href, label) {
  return el("a", { href, class: "more" }, [label, icon("chevronRight", { size: 13 })]);
}

export function badge(label, tone, { live = false } = {}) {
  return el("span", { class: `badge b-${tone}${live ? " live" : ""}` }, [
    el("span", { class: "pip" }),
    label,
  ]);
}

/** Run states, grouped by what an operator should do about them. */
const TONES = {
  COMPLETED: "ok",
  RUNNING: "run",
  VERIFYING: "run",
  READY: "run",
  PENDING: "idle",
  WAITING_APPROVAL: "warn",
  FAILED_RETRYABLE: "warn",
  BLOCKED: "warn",
  INTERRUPTED: "warn",
  FAILED_PERMANENT: "bad",
  CANCELLED: "bad",
};

export function stateBadge(state, { live = false } = {}) {
  return badge(state, TONES[state] ?? "idle", { live });
}

export function row(key, value) {
  return el("div", { class: "row" }, [
    el("span", { class: "k", text: key }),
    el("span", { class: "v" }, [typeof value === "string" ? value : value]),
  ]);
}

/** Local time, with the exact instant kept in the tooltip rather than discarded. */
export function time(iso) {
  if (!iso) return el("span", { class: "faint", text: "—" });
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return el("span", { class: "mono", text: iso });
  const today = new Date().toDateString() === at.toDateString();
  const label = today
    ? at.toLocaleTimeString()
    : `${at.toLocaleDateString()} ${at.toLocaleTimeString()}`;
  return el("span", { class: "mono", title: iso, text: label });
}

export function clock(iso) {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "--:--:--" : at.toLocaleTimeString("en-GB", { hour12: false });
}

export function ago(iso) {
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function table(headers, rows) {
  return el("div", { class: "scroll" }, [
    el("table", {}, [
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))),
      el("tbody", {}, rows),
    ]),
  ]);
}

/** A worker requirement, coloured per provider so a graph is scannable. */
export function workerTag(requirement) {
  const kind = requirement.startsWith("claude")
    ? "claude"
    : requirement.startsWith("codex")
      ? "codex"
      : requirement === "human"
        ? "human"
        : "tool";
  return el("span", { class: `tag ${kind}`, text: requirement });
}

export function empty(message) {
  return el("p", { class: "empty", text: message });
}

/** A real switch rather than a bare checkbox — the input stays for keyboard use. */
export function toggle(id, label, checked, onChange) {
  return el("label", { class: "switch" }, [
    el("span", { class: "track" }, [
      el("input", { type: "checkbox", id, checked: checked || null, on: { change: onChange } }),
      el("span", { class: "knob" }),
    ]),
    label,
  ]);
}

export { icon };
