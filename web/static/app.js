const $ = (id) => document.getElementById(id);
const BASE = `${location.protocol}//${location.host}`;
$("h_endpoint").textContent = BASE;

// ── Tabs (Request + Snippet) ───────────────────────
document.querySelectorAll(".tabs").forEach((group) => {
  group.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    group.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    if (t.dataset.tab) {
      // Request tab
      document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
      document.querySelector(`.tab-panel[data-panel="${t.dataset.tab}"]`)?.classList.add("active");
      updateHint();
    }
    if (t.dataset.lang) renderSnippet();
  }));
});

// ── Presets ────────────────────────────────────────
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
  if (c.dataset.preset === "solve") {
    $("s_url").value = c.dataset.url;
    $("s_key").value = c.dataset.key;
  } else if (c.dataset.preset === "challenge") {
    $("c_url").value = c.dataset.url;
  }
  renderSnippet();
  updateHint();
}));

// ── Active tab helpers ─────────────────────────────
function activeRequestTab() {
  return document.querySelector(".request-card .tab.active")?.dataset.tab || "solve";
}
function activeLang() {
  return document.querySelector(".snippet-card .tab.active")?.dataset.lang || "curl";
}

function getPayload() {
  const kind = activeRequestTab();
  if (kind === "solve") {
    const body = {
      sitekey: $("s_key").value.trim(),
      siteurl: $("s_url").value.trim(),
      timeout: parseInt($("s_to").value) || 45,
    };
    const act = $("s_action").value.trim();
    if (act) body.action = act;
    return { endpoint: "/solve", body };
  }
  if (kind === "challenge") {
    return {
      endpoint: "/solve-challenge",
      body: {
        siteurl: $("c_url").value.trim(),
        timeout: parseInt($("c_to").value) || 60,
      },
    };
  }
  // raw
  let body;
  try { body = JSON.parse($("r_body").value); } catch { body = {}; }
  return { endpoint: $("r_ep").value, body };
}

function updateHint() {
  const { endpoint } = getPayload();
  $("run_hint").textContent = `POST ${endpoint}`;
}

// ── Status + response rendering ────────────────────
function setStatus(state, text) {
  const el = $("run_status");
  el.className = "status " + state;
  el.innerHTML = state === "run" ? `<span class="spin"></span> ${text}` : text;
}

function setMeta(status, ms, serverMs) {
  const kv = (b, e) => `<span class="kv"><b>${b}</b><em>${e}</em></span>`;
  $("meta").innerHTML =
    kv("status", status) +
    kv("time", ms != null ? `${(ms / 1000).toFixed(2)}s` : "—") +
    kv("server", serverMs != null ? `${serverMs}s` : "—") +
    `<button class="link-btn" id="copy" onclick="copyResp()">copy</button>`;
}

window.copyResp = () => navigator.clipboard.writeText($("out").textContent).then(() => {
  const btn = document.querySelector("#meta .link-btn");
  if (!btn) return;
  const prev = btn.textContent; btn.textContent = "copied";
  setTimeout(() => { btn.textContent = prev; }, 900);
});

async function send() {
  const { endpoint, body } = getPayload();
  if (activeRequestTab() !== "raw") {
    if (!body.siteurl) { setStatus("err", "siteurl required"); return; }
    if (endpoint === "/solve" && !body.sitekey) { setStatus("err", "sitekey required"); return; }
  }
  setStatus("run", "sending…");
  $("run_btn").disabled = true;
  const t0 = performance.now();
  try {
    const r = await fetch(BASE + endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const ct = r.headers.get("content-type") || "";
    const data = ct.includes("json") ? await r.json() : await r.text();
    const ms = performance.now() - t0;
    setMeta(r.status, ms, data && data.elapsed);
    $("out").textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    setStatus(r.ok ? "ok" : "err", r.ok ? `ok ${r.status}` : `error ${r.status}`);
    refreshStats();
  } catch (e) {
    setMeta("net-err", performance.now() - t0, null);
    $("out").textContent = String(e);
    setStatus("err", "network error");
  } finally {
    $("run_btn").disabled = false;
  }
}
$("run_btn").onclick = send;

// ── Snippets ───────────────────────────────────────
function renderSnippet() {
  const lang = activeLang();
  const { endpoint, body } = getPayload();
  const url = BASE + endpoint;
  const json = JSON.stringify(body, null, 2);
  const compact = JSON.stringify(body);
  let code = "";
  if (lang === "curl") {
    code = `curl -X POST ${url} \\\n  -H 'Content-Type: application/json' \\\n  -d '${compact}'`;
  } else if (lang === "node") {
    code = `const res = await fetch("${url}", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(${json}),
});
const data = await res.json();
console.log(data);`;
  } else if (lang === "python") {
    const py = json
      .replace(/: true/g, ": True")
      .replace(/: false/g, ": False")
      .replace(/: null/g, ": None");
    code = `import requests

res = requests.post(
    "${url}",
    json=${py},
    timeout=${(body.timeout || 45) + 15},
)
res.raise_for_status()
print(res.json())`;
  } else if (lang === "php") {
    code = `<?php
$ch = curl_init("${url}");
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_HTTPHEADER => ["Content-Type: application/json"],
    CURLOPT_POSTFIELDS => json_encode(${phpArray(body)}),
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => ${(body.timeout || 45) + 15},
]);
echo curl_exec($ch);
curl_close($ch);`;
  } else if (lang === "go") {
    code = `package main

import (
\t"bytes"
\t"encoding/json"
\t"fmt"
\t"io"
\t"net/http"
)

func main() {
\tbody, _ := json.Marshal(map[string]any${goMap(body)})
\tresp, err := http.Post("${url}", "application/json", bytes.NewReader(body))
\tif err != nil { panic(err) }
\tdefer resp.Body.Close()
\tout, _ := io.ReadAll(resp.Body)
\tfmt.Println(string(out))
}`;
  }
  $("snip_out").textContent = code;
}
function phpArray(obj) {
  return "[" + Object.entries(obj).map(([k, v]) =>
    `"${k}" => ${typeof v === "string" ? `"${v}"` : v}`).join(", ") + "]";
}
function goMap(obj) {
  return "{\n" + Object.entries(obj).map(([k, v]) =>
    `\t\t"${k}": ${typeof v === "string" ? `"${v}"` : v}`).join(",\n") + ",\n\t}";
}

$("snip_copy").onclick = () => navigator.clipboard.writeText($("snip_out").textContent).then(() => {
  $("snip_copy").textContent = "copied";
  setTimeout(() => { $("snip_copy").textContent = "copy"; }, 900);
});

// Re-render snippet + hint when any input changes
["s_url", "s_key", "s_to", "s_action", "c_url", "c_to", "r_ep", "r_body"].forEach((id) => {
  $(id).addEventListener("input", () => { renderSnippet(); updateHint(); });
  $(id).addEventListener("change", () => { renderSnippet(); updateHint(); });
});

// ── Stats polling ──────────────────────────────────
function fmtUptime(s) {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
function fmtMs(ms) {
  if (!ms) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
}
function pulse(id) {
  const el = $(id); if (!el) return;
  el.classList.add("pulse");
  setTimeout(() => el.classList.remove("pulse"), 600);
}
let lastTotal = -1;
async function refreshStats() {
  try {
    const r = await fetch(BASE + "/stats", { cache: "no-store" });
    if (!r.ok) return;
    const s = await r.json();
    $("h_mode").textContent = `mode: ${s.mode}${s.proxy_url ? " → " + s.proxy_url : ""}`;
    $("st_uptime").textContent = fmtUptime(s.uptime);
    $("st_inflight").textContent = s.in_flight;
    $("st_solved").textContent = s.solved;
    $("st_challenges").textContent = s.challenges;
    $("st_errors").textContent = s.errors;
    $("st_success").textContent = s.total_requests ? `${s.success_rate}%` : "—";
    $("st_avg").textContent = fmtMs(s.latency_ms.avg);
    $("st_p95").textContent = fmtMs(s.latency_ms.p95);
    if (lastTotal >= 0 && s.total_requests > lastTotal) {
      ["st_solved", "st_challenges", "st_errors", "st_success", "st_avg", "st_p95"].forEach(pulse);
    }
    lastTotal = s.total_requests;
    renderEvents(s.events || []);
  } catch {}
}
function renderEvents(events) {
  $("ev_count").textContent = events.length ? `${events.length} event${events.length === 1 ? "" : "s"}` : "no events";
  const el = $("events");
  if (!events.length) { el.innerHTML = '<div class="empty">No requests yet. Send one to see it here.</div>'; return; }
  el.innerHTML = events.map((e) => {
    const ok = e.status >= 200 && e.status < 400;
    return `<div class="event">
      <span class="ts">${fmtTime(e.ts)}</span>
      <span class="ep">${esc(e.endpoint)}</span>
      <span class="st ${ok ? "ok" : "err"}">${e.status} · ${fmtMs(e.duration * 1000)}</span>
      <span class="sum" title="${esc(e.summary)}">${esc(e.summary)}</span>
    </div>`;
  }).join("");
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

updateHint();
renderSnippet();
refreshStats();
setInterval(refreshStats, 3000);
