/* Inline SVG icons.
 *
 * Built as real SVG elements rather than text glyphs. The glyphs this replaces —
 * ▤ ⛉ ⚇ ⛨ ⌘ — rendered as boxes or as the wrong symbol depending on which fonts the
 * machine had, which is exactly the failure a self-contained page should not have.
 *
 * No icon font and no network request: a strict-CSP page cannot fetch one, and this
 * dashboard is served from the control plane with nothing external allowed.
 *
 * Shapes are literals in this file, never interpolated from data, and are built with
 * createElementNS rather than innerHTML so `dom.js`'s rule holds everywhere.
 */

const NS = "http://www.w3.org/2000/svg";

//: [tag, attributes]. Stroke-based, 24x24 viewBox, currentColor.
const ICONS = {
  runs: [
    ["path", { d: "M8 6h13M8 12h13M8 18h13" }],
    ["path", { d: "M3 6h.01M3 12h.01M3 18h.01" }],
  ],
  approvals: [
    ["path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" }],
    ["polyline", { points: "9 12 11 14 15 10" }],
  ],
  workers: [
    ["rect", { x: "4", y: "4", width: "16", height: "16", rx: "2" }],
    ["rect", { x: "9", y: "9", width: "6", height: "6" }],
    ["path", { d: "M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" }],
  ],
  policies: [["path", { d: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" }]],
  dag: [
    ["circle", { cx: "6", cy: "6", r: "3" }],
    ["circle", { cx: "6", cy: "18", r: "3" }],
    ["path", { d: "M6 9v6" }],
    ["circle", { cx: "18", cy: "12", r: "3" }],
    ["path", { d: "M9 6h4a2 2 0 0 1 2 2v2M9 18h4a2 2 0 0 0 2-2v-2" }],
  ],
  artifact: [
    ["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" }],
    ["polyline", { points: "14 2 14 8 20 8" }],
    ["path", { d: "M8 13h8M8 17h5" }],
  ],
  clock: [
    ["circle", { cx: "12", cy: "12", r: "9" }],
    ["polyline", { points: "12 7 12 12 15 14" }],
  ],
  terminal: [
    ["polyline", { points: "4 17 10 11 4 5" }],
    ["path", { d: "M12 19h8" }],
  ],
  refresh: [
    ["path", { d: "M21 4v6h-6" }],
    ["path", { d: "M3 20v-6h6" }],
    ["path", { d: "M4.6 9a8 8 0 0 1 13.3-3L21 10M3 14l3.1 4A8 8 0 0 0 19.4 15" }],
  ],
  stop: [["rect", { x: "5", y: "5", width: "14", height: "14", rx: "2" }]],
  check: [["polyline", { points: "20 6 9 17 4 12" }]],
  pending: [["circle", { cx: "12", cy: "12", r: "8" }]],
  pause: [["path", { d: "M10 4v16M14 4v16" }]],
  cross: [["path", { d: "M18 6 6 18M6 6l12 12" }]],
  human: [
    ["path", { d: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" }],
    ["circle", { cx: "12", cy: "7", r: "4" }],
  ],
  worktree: [
    ["rect", { x: "3", y: "11", width: "18", height: "10", rx: "2" }],
    ["path", { d: "M7 11V7a5 5 0 0 1 10 0v4" }],
  ],
  arrowRight: [
    ["path", { d: "M5 12h13" }],
    ["polyline", { points: "13 6 19 12 13 18" }],
  ],
  arrowLeft: [
    ["path", { d: "M19 12H6" }],
    ["polyline", { points: "11 6 5 12 11 18" }],
  ],
  arrowDown: [
    ["path", { d: "M12 5v13" }],
    ["polyline", { points: "6 13 12 19 18 13" }],
  ],
  chevronRight: [["polyline", { points: "9 18 15 12 9 6" }]],
  info: [
    ["circle", { cx: "12", cy: "12", r: "9" }],
    ["path", { d: "M12 16v-4M12 8h.01" }],
  ],
  warning: [
    ["path", { d: "M10.3 4 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0z" }],
    ["path", { d: "M12 9v4M12 17h.01" }],
  ],
  fit: [
    ["circle", { cx: "12", cy: "12", r: "8" }],
    ["path", { d: "M12 1v3M12 20v3M23 12h-3M4 12H1" }],
  ],
  expand: [
    ["path", { d: "M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3" }],
    ["path", { d: "M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3" }],
  ],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  minus: [["path", { d: "M5 12h14" }]],
  external: [
    ["path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }],
    ["polyline", { points: "15 3 21 3 21 9" }],
    ["path", { d: "M10 14 21 3" }],
  ],
  moon: [["path", { d: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" }]],
  sun: [
    ["circle", { cx: "12", cy: "12", r: "4" }],
    ["path", { d: "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }],
  ],
  brand: [
    ["path", { d: "M12 3 3 8v8l9 5 9-5V8z" }],
    ["path", { d: "M12 12 3 8M12 12l9-4M12 12v9" }],
  ],
};

export function icon(name, { size = 16, cls = "" } = {}) {
  const shapes = ICONS[name];
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  if (!shapes) return svg;
  for (const [tag, attrs] of shapes) {
    const shape = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) shape.setAttribute(key, value);
    svg.append(shape);
  }
  return svg;
}

export const hasIcon = (name) => name in ICONS;
