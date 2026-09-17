// Thin fetch wrapper — every call is same-origin (Section 1: reuse the
// existing API, never a frontend-only fake). The local MA1B token is read
// once from the page-scoped global app.web injected server-side (PV-01) —
// never persisted to localStorage/sessionStorage, never displayed.

const TOKEN = typeof window !== "undefined" ? window.__MAP_LOCAL_TOKEN__ : null;

export class ApiError extends Error {
  constructor(message, { status, code, detail } = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function request(path, { method = "GET", body, headers } = {}) {
  const reqHeaders = new Headers(headers || {});
  reqHeaders.set("Authorization", `Bearer ${TOKEN}`);
  let payload;
  if (body !== undefined) {
    reqHeaders.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, { method, headers: reqHeaders, body: payload });
  } catch (networkErr) {
    throw new ApiError("Could not reach the local API. Is the server running?", { status: 0 });
  }

  const raw = await response.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = raw;
    }
  }

  if (!response.ok) {
    const errBody = data && typeof data === "object" ? data.error : null;
    throw new ApiError((errBody && errBody.message) || response.statusText || "Request failed", {
      status: response.status,
      code: errBody && errBody.code,
      detail: errBody && errBody.detail,
    });
  }
  return data;
}

export const api = {
  get: (path) => request(path),
  post: (path, body, opts = {}) => request(path, { method: "POST", body: body ?? {}, ...opts }),
  raw: request,
  getText: async (path) => {
    const response = await fetch(path, { headers: { Authorization: `Bearer ${TOKEN}` } });
    if (!response.ok) {
      throw new ApiError(`Failed to load ${path} (${response.status}).`, { status: response.status });
    }
    return response.text();
  },
};
