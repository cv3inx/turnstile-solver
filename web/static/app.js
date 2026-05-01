const $ = (id) => document.getElementById(id);

// Auto-detect the solver's public URL from the address bar - snippets and
// fetch calls use this so sharing the playground link just works.
const BASE = `${location.protocol}//${location.host}`;
$("h_endpoint").textContent = BASE;

// ── Tabs ──────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  document.querySelector(`.tab-panel[data-panel="${t.dataset.tab}"]`).classList.add("active");
  if (t.dataset.tab === "snippet") renderSnippet();
}));
document.querySelectorAll(".sub-tab").forEach((t) => t.addEventListener("click", () => {
  document.querySelectorAll(".sub-tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  renderSnippet();
}));

// ── Presets ───────────────────────────────────────────
document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => {
  if (c.dataset.preset === "solve") {
    $("s_url").value = c.dataset.url;
    $("s_key").value = c.dataset.key;
  } else if (c.dataset.preset === "challenge") {
    $("c_url").value = c.dataset.url;
  }
  renderSnippet();
}));

// ── Send ──────────────────────────────────────────────
function setStatus(el, state, text) {
  el.className = "status " + state;
  el.innerHTML = state === "run" ? `<span class="spin"></span> ${text}` : text;
}

function show(body, t0, res) {
  const ms = Math.round(performance.now() - t0);
  const statusChip = `<span class="kv"><b>status</b> ${res.status}</span>`;
  const timeChip = `<span class="kv"><b>time</b> ${(ms / 1000).toFixed(2)}s</span>`;
  const extra = body && body.elapsed != null ? `<span class="kv"><b>server</b> ${body.elapsed}s</span>` : "";
  $("meta").innerHTML = statusChip + timeChip + extra;
  $("out").textContent = typeof body === "string" ? body : JSON.stringify(body, null, 2);
}

async function send(ep, payload, statusEl) {
  setStatus(statusEl, "run", "sending...");
  const t0 = performance.now();
  try {
    const r = await fetch(BASE + ep, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    show(body, t0, r);
    setStatus(statusEl, r.ok ? "ok" : "err", r.ok ? `ok ${r.status}` : `error ${r.status}`);
    refreshStats();
  } catch (e) {
    show({ error: String(e) }, t0, { status: "net-err" });
    setStatus(statusEl, "err", "network error");
  }
}

function getPayload(kind) {
  if (kind === "solve") {
    return {
      endpoint: "/solve",
      body: {
        sitekey: $("s_key").value.trim(),
        siteurl: $("s_url").value.trim(),
        timeout: parseInt($("s_to").value) || 45,
      },
    };
  }
  return {
    endpoint: "/solve-challenge",
    body: {
      siteurl: $("c_url").value.trim(),
      timeout: parseInt($("c_to").value) || 60,
    },
  };
}

$("run_solve").onclick = () => {
  const p = getPayload("solve");
  send(p.endpoint, p.body, $("s_status"));
};
$("run_challenge").onclick = () => {
  const p = getPayload("challenge");
  send(p.endpoint, p.body, $("c_status"));
};
$("run_raw").onclick = () => {
  let payload;
  try { payload = JSON.parse($("r_body").value); }
  catch { setStatus($("r_status"), "err", "invalid JSON"); return; }
  send($("r_ep").value, payload, $("r_status"));
};

// ── Copy buttons ──────────────────────────────────────
function setupCopy(btnId, targetId) {
  $(btnId).onclick = () => navigator.clipboard.writeText($(targetId).textContent).then(() => {
    const el = $(btnId);
    const prev = el.textContent;
    el.textContent = "copied";
    setTimeout(() => { el.textContent = prev; }, 1000);
  });
}
setupCopy("copy", "out");
setupCopy("snip_copy", "snip_out");

// ── Snippets ──────────────────────────────────────────
function renderSnippet() {
  const lang = document.querySelector(".sub-tab.active")?.dataset.lang || "curl";
  const kind = $("snip_src").value;
  const { endpoint, body } = getPayload(kind);
  const url = BASE + endpoint;
  const json = JSON.stringify(body, null, 2);
  const ind = JSON.stringify(body);
  let code = "";

  if (lang === "curl") {
    code = `curl -X POST ${url} \\\n  -H 'Content-Type: application/json' \\\n  -d '${ind}'`;
  } else if (lang === "node") {
    code = `const res = await fetch("${url}", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(${json})
});
const data = await res.json();
console.log(data);`;
  } else if (lang === "python") {
    code = `import requests

res = requests.post(
    "${url}",
    json=${json.replace(/"/g, '"').replace(/: true/g, ": True").replace(/: false/g, ": False").replace(/: null/g, ": None")},
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
$res = curl_exec($ch);
curl_close($ch);
echo $res;`;
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
  const parts = Object.entries(obj).map(([k, v]) => {
    const val = typeof v === "string" ? `"${v}"` : v;
    return `"${k}" => ${val}`;
  });
  return "[" + parts.join(", ") + "]";
}

function goMap(obj) {
  const parts = Object.entries(obj).map(([k, v]) => {
    const val = typeof v === "string" ? `"${v}"` : v;
    return `\t\t"${k}": ${val}`;
  });
  return "{\n" + parts.join(",\n") + ",\n\t}";
}

// Re-render snippet on any input change so users can copy live values
["s_url", "s_key", "s_to", "c_url", "c_to", "snip_src"].forEach((id) => {
  $(id).addEventListener("input", renderSnippet);
  $(id).addEventListener("change", renderSnippet);
});

// ── Stats polling ─────────────────────────────────────
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
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function pulse(el) {
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
      ["st_solved", "st_challenges", "st_errors", "st_success", "st_avg", "st_p95"].forEach(id => pulse($(id)));
    }
    lastTotal = s.total_requests;

    renderEvents(s.events || []);
  } catch {}
}

function renderEvents(events) {
  $("ev_count").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
  const el = $("events");
  if (!events.length) {
    el.innerHTML = '<div class="empty">No requests yet. Send one to see it here.</div>';
    return;
  }
  el.innerHTML = events.map(e => {
    const ok = e.status >= 200 && e.status < 400;
    const stClass = ok ? "ok" : "err";
    const dur = fmtMs(e.duration * 1000);
    return `<div class="event">
      <span class="ts">${fmtTime(e.ts)}</span>
      <span class="ep">${esc(e.endpoint)}</span>
      <span class="st ${stClass}">${e.status} · ${dur}</span>
      <span class="sum" title="${esc(e.summary)}">${esc(e.summary)}</span>
    </div>`;
  }).join("");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

refreshStats();
renderSnippet();
setInterval(refreshStats, 3000);
