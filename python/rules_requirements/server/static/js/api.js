// SPDX-License-Identifier: AGPL-3.0-or-later
// JSON client for the rr serve API, plus the per-browser settings it sends.

const AUTHOR_KEY = "rr.author";
const TOKEN_KEY = "rr.token";

function read(storage, key) {
  try {
    return storage.getItem(key) || "";
  } catch {
    return "";
  }
}

function write(storage, key, value) {
  try {
    if (value) storage.setItem(key, value);
    else storage.removeItem(key);
  } catch {
    /* private mode or blocked storage: settings last for this page only */
  }
}

let memoryAuthor = "";
let memoryToken = "";

export const settings = {
  get author() {
    return read(localStorage, AUTHOR_KEY) || memoryAuthor;
  },
  set author(value) {
    memoryAuthor = (value || "").trim();
    write(localStorage, AUTHOR_KEY, memoryAuthor);
  },
  get token() {
    return read(sessionStorage, TOKEN_KEY) || memoryToken;
  },
  set token(value) {
    memoryToken = (value || "").trim();
    write(sessionStorage, TOKEN_KEY, memoryToken);
  },
};

/** Take a `#token=...` handed out by `rr serve --token` and drop it from the URL. */
export function captureToken() {
  const m = location.hash.match(/(?:^#|[&?])token=([^&]+)/);
  if (m) {
    settings.token = decodeURIComponent(m[1]);
    history.replaceState(null, "", location.pathname + location.search + "#/");
  }
}

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}


export async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") {
    headers["X-RR-Request"] = "1";
    headers["Content-Type"] = "application/json";
  }
  const author = settings.author;
  // Header values must be ISO-8859-1: percent-encode (the server decodes).
  if (author) headers["X-RR-Author"] = encodeURIComponent(author);
  const token = settings.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (err) {
    throw new ApiError(`Cannot reach the rr server (${err.message}). Is \`rr serve\` still running?`, 0, null);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 300) };
    }
  }
  if (!res.ok) {
    const message = (data && data.error) || `${res.status} ${res.statusText}`;
    throw new ApiError(
      res.status === 401 ? `${message} — open the link rr serve printed (it carries the token).` : message,
      res.status,
      data,
    );
  }
  return data;
}

export const get = (path) => api("GET", path);
export const post = (path, body = {}) => api("POST", path, body);
export const put = (path, body = {}) => api("PUT", path, body);
export const patch = (path, body = {}) => api("PATCH", path, body);
export const del = (path) => api("DELETE", path);

export const enc = encodeURIComponent;
