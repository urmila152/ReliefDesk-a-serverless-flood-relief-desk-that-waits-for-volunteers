const API = (window.RELIEF_API || "").replace(/\/$/, "");
const AREAS = [ // demo areas (fictional requests only)
  ["Hadapsar", 18.5089, 73.9259], ["Kothrud", 18.5074, 73.8077], ["Katraj", 18.4575, 73.8677],
  ["Aundh", 18.5580, 73.8075], ["Pimpri", 18.6298, 73.7997], ["Kharadi", 18.5515, 73.9348]];

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    method: opts.method || "GET",
    headers: { "content-type": "application/json", ...(opts.key ? { "x-coordinator-key": opts.key } : {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || "Request failed"), { status: res.status });
  return data;
}

// Safe DOM builder: all dynamic text goes through textContent (user text is never parsed as HTML).
function h(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
  return n;
}
const $ = (id) => document.getElementById(id);
const NEED = { rescue: "Rescue", medical: "Medical help", shelter: "Shelter", water: "Drinking water", food: "Food", other: "Other help" };
