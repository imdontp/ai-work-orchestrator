# Dashboard source

Plain ES modules. No build step, no bundler, no npm dependency — the files here are what
the browser loads, served by `apps/api/app/main.py` under `/dashboard`.

That is a security decision before it is a packaging one. The API has no authentication;
a dashboard on its own dev server would be a second origin, which means CORS, which
would make a control plane that runs CLI agents with filesystem write access reachable
from any page the operator happens to have open. Same-origin removes the question. See
ADR-011 and `docs/BACKLOG.md` item 5.

| File | What it owns |
|---|---|
| `index.html` | The shell: sidebar, top bar, view host. Nothing else. |
| `style.css` | The whole stylesheet. Dark first; a light palette follows it. |
| `dom.js` | Element helpers, badges, tables, switches, time formatting. |
| `icons.js` | Inline SVG. No icon font, no network request. |
| `api.js` | Every call to `/api/v1`. The only module that knows about `fetch`. |
| `runstate.js` | Run + events + workflow → per-node status, durations, progress. |
| `graph.js` | The DAG, drawn from the workflow definition. |
| `panels.js` | Overview, workers, approval, artifacts, logs, artifact renderers. |
| `views.js` | Pages: runs, run detail, approvals, workers, policies. |
| `app.js` | Routing, the refresh loop, and the three writes. |

## Three rules

**Never `innerHTML`.** Everything rendered comes from an LLM worker's artifact, a
repository path, or an operator's reason string. `dom.js` sets text with `textContent`
and exposes no way to assign markup. A worker emitting `<img onerror=...>` in a summary
is a plausible accident, and the page it would be scripting can approve runs.

**Never invent a number.** `runstate.js` returns `null` for a duration it cannot compute
and `null` for progress without a workflow, and the panels render "—". There is no
estimated completion and no "triggered by" because the control plane records neither.

**Never a text glyph where an icon belongs.** The first version used `▤ ⛉ ⚇ ⛨ ⌘`, which
rendered as boxes or as the wrong symbol depending on the machine's fonts. `icons.js`
builds real SVG with `createElementNS`, so the rule above still holds.

## Theme

Dark is the design; the OS preference does not decide it. The top bar has a toggle and
the choice is kept in `localStorage`. `:root[data-theme="light"]` is a palette swap so
the page is usable on a bright screen — not a second design.

## Testing

`tests/test_dashboard.py` covers what Python can break: the mount, the MIME types (a
Windows registry that maps `.js` to `text/plain` would serve a blank page with a 200),
the import graph, and that no CORS middleware has appeared.

What the browser paints has no automated coverage — there is no JS test runner here and
ADR-011 does not put one in scope. Two things stand in for it, and both found real bugs:

- **A DOM shim in Node.** Import the modules, stub `document.createElement`, call the
  render functions and serialize the tree. This found the `waiting`-node bug in
  `runstate.js`, where the plan gate highlighted a node four places past where the run
  had actually stopped.
- **A headless screenshot.** `chrome --headless=new --screenshot --window-size=1680,940`
  against a running server. Reading the picture found the rest: the brand line wrapping
  mid-phrase, a lone DAG node stretched across the panel, an invisible row connector,
  and a hundred-pixel hole under the graph where a grid wrapper stretched but the panel
  inside it did not. None of those are visible from the DOM tree, and none would fail a
  Python test.
