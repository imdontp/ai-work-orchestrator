/* The API client.
 *
 * Same origin as the control plane: this page is served by the FastAPI app itself, so
 * there is no base URL to configure and no CORS to relax. ADR-011 records why that
 * matters — the API has no authentication, and a cross-origin dashboard would make an
 * unauthenticated control plane reachable from any page the operator has open.
 *
 * Every endpoint here already existed except GET /workflows/{id}, which ADR-012 adds
 * and bounds: it reads configuration, never run state.
 */

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function send(method, path, payload) {
  let response;
  try {
    response = await fetch(BASE + path, {
      method,
      headers: payload
        ? { accept: "application/json", "content-type": "application/json" }
        : { accept: "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
    });
  } catch {
    // A dead control plane is the common case here, not a network partition.
    throw new ApiError(`cannot reach the API at ${BASE} — is it running?`, 0);
  }
  const body = await response.text();
  if (!response.ok) {
    let message = body;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") message = parsed.detail;
    } catch {
      /* not JSON; the raw body is the best message available */
    }
    throw new ApiError(message || `${response.status} ${response.statusText}`, response.status);
  }
  return body ? JSON.parse(body) : null;
}

const get = (path) => send("GET", path);
const id = (value) => encodeURIComponent(value);

export const listRuns = (limit = 50) => get(`/runs?limit=${id(limit)}`);
export const getRun = (runId) => get(`/runs/${id(runId)}`);
export const getEvents = (runId) => get(`/runs/${id(runId)}/events`);
export const getArtifact = (runId, name) => get(`/runs/${id(runId)}/artifacts/${id(name)}`);
export const getWorkflow = (workflowId) => get(`/workflows/${id(workflowId)}`);
export const getCapabilities = () => get("/system/capabilities");

// -- the three writes, all of which the API already had ----------------------

export const decide = (runId, decision, reason) =>
  send("POST", `/runs/${id(runId)}/decision`, { decision, reason });

export const cancel = (runId, reason) => send("POST", `/runs/${id(runId)}/cancel`, { reason });

/** Where the raw JSON lives, for the cases the page declines to render. */
export const artifactUrl = (runId, name) => `${BASE}/runs/${id(runId)}/artifacts/${id(name)}`;
