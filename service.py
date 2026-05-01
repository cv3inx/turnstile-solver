"""
Turnstile + CF JS-Challenge HTTP service (aiohttp).
"""

import json
import logging
import os
import platform
import subprocess
import sys
import time
import uuid
from typing import Optional

from aiohttp import web

from solver import get_pool, solve_async, solve_challenge_async, _challenge_proxy


PORT = int(os.environ.get("PORT", 9988))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 8))


def _ensure_display() -> Optional[subprocess.Popen]:
    if platform.system() != "Linux" or os.environ.get("DISPLAY"):
        return None
    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)
    return proc


_stats = {"in_flight": 0, "solved": 0, "errors": 0, "challenges": 0}


def _rid() -> str:
    return uuid.uuid4().hex[:8]


def _summary(body: dict) -> str:
    """Short one-line status summary."""
    if "error" in body:
        return f"error: {body['error']}"
    if "token" in body:
        t = body["token"]
        return f"token {t[:12]}...{t[-6:]} ({len(t)} chars)"
    if "title" in body:
        parts = [f"title={body.get('title')!r}"]
        if "cookies" in body:
            parts.append(f"cookies={len(body['cookies'])}")
        if "html" in body:
            parts.append(f"html={len(body['html'])}b")
        return " ".join(parts)
    return "ok"


def _emit_start(rid: str, method: str, path: str, url: str, key: str, peer: str):
    block = (
        "\n「 NEW REQUEST 」"
        f"\n» ID     : {rid}"
        f"\n» FROM   : {peer}"
        f"\n» {method:<6} : {path}"
        f"\n» URL    : {url or '-'}"
    )
    if key:
        block += f"\n» KEY    : {key[:14] + '...' if len(key) > 14 else key}"
    print(block, flush=True)


def _emit_end(rid: str, elapsed: float, status: int, body: dict):
    print(
        f"» SPEED  : {elapsed:.2f}s"
        f"\n» STATUS : {status} - {_summary(body)}",
        flush=True,
    )


async def handle_solve(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    method = request.method
    peer = request.remote or "-"

    try:
        payload = await request.json()
    except Exception:
        body = {"error": "invalid JSON"}
        _emit_start(rid, method, path, "", "", peer)
        _emit_end(rid, time.time() - t0, 400, body)
        return web.json_response(body, status=400)

    sitekey = (payload.get("sitekey") or "").strip()
    siteurl = (payload.get("siteurl") or "").strip()
    timeout = int(payload.get("timeout", 45))

    _emit_start(rid, method, path, siteurl, sitekey, peer)

    if not sitekey or not siteurl:
        body = {"error": "sitekey and siteurl required"}
        _emit_end(rid, time.time() - t0, 400, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        token = await solve_async(sitekey, siteurl, req_id=rid, timeout=timeout)
        elapsed = time.time() - t0
        _stats["solved"] += 1
        body = {"token": token, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        body = {"error": str(exc), "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 500, body)
        return web.json_response(body, status=500)
    finally:
        _stats["in_flight"] -= 1


async def handle_challenge(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    method = request.method
    peer = request.remote or "-"

    try:
        payload = await request.json()
    except Exception:
        body = {"error": "invalid JSON"}
        _emit_start(rid, method, path, "", "", peer)
        _emit_end(rid, time.time() - t0, 400, body)
        return web.json_response(body, status=400)

    siteurl = (payload.get("siteurl") or "").strip()
    timeout = int(payload.get("timeout", 45))

    _emit_start(rid, method, path, siteurl, "", peer)

    if not siteurl:
        body = {"error": "siteurl required"}
        _emit_end(rid, time.time() - t0, 400, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        result = await solve_challenge_async(siteurl, req_id=rid, timeout=timeout)
        elapsed = time.time() - t0
        _stats["challenges"] += 1
        body = {**result, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        body = {"error": str(exc), "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 500, body)
        return web.json_response(body, status=500)
    finally:
        _stats["in_flight"] -= 1


_PLAYGROUND_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>EzSolver Playground</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0f17;--panel:#121826;--panel2:#0f1522;--line:#1f2a3c;--text:#e6edf5;--mute:#8aa0bd;--accent:#4ea1ff;--accent2:#6ad1b8;--err:#ff6b6b;--ok:#4ade80;--warn:#facc15;--chip:#1b2436}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#0d1322,#0b0f17)}
header h1{margin:0;font-size:15px;font-weight:600;letter-spacing:.3px}
header .dot{width:8px;height:8px;border-radius:50%;background:var(--accent2);box-shadow:0 0 0 4px rgba(106,209,184,.15)}
header .tag{font-size:11px;color:var(--mute);margin-left:auto}
main{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1.2fr);gap:16px;padding:16px;max-width:1400px;margin:0 auto}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.card h2{margin:0;padding:12px 14px;font-size:13px;font-weight:600;color:#bcd0ee;border-bottom:1px solid var(--line);background:var(--panel2);display:flex;align-items:center;gap:8px}
.card .body{padding:14px}
.tabs{display:flex;gap:4px;padding:0 8px;border-bottom:1px solid var(--line);background:var(--panel2)}
.tab{padding:10px 14px;font-size:12px;color:var(--mute);cursor:pointer;border-bottom:2px solid transparent;user-select:none}
.tab.active{color:var(--text);border-color:var(--accent)}
.tab-panel{display:none}
.tab-panel.active{display:block}
label{display:block;font-size:11px;color:var(--mute);margin:10px 0 4px;text-transform:uppercase;letter-spacing:.4px}
input,textarea,select{width:100%;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
textarea{min-height:90px;resize:vertical}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
.row{display:flex;gap:8px}
.row>*{flex:1}
.chiprow{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{background:var(--chip);border:1px solid var(--line);color:var(--text);border-radius:999px;padding:4px 10px;font-size:11px;cursor:pointer}
.chip:hover{border-color:var(--accent)}
.actions{display:flex;gap:8px;align-items:center;margin-top:14px}
button{background:var(--accent);border:0;color:#06101e;font-weight:600;padding:9px 14px;border-radius:6px;cursor:pointer;font-size:13px}
button:hover{filter:brightness(1.1)}
button.secondary{background:var(--chip);color:var(--text);border:1px solid var(--line)}
button:disabled{opacity:.55;cursor:not-allowed}
.status{font-size:12px;color:var(--mute);display:flex;align-items:center;gap:6px}
.status.ok{color:var(--ok)}
.status.err{color:var(--err)}
.status.run{color:var(--warn)}
.spin{width:12px;height:12px;border-radius:50%;border:2px solid currentColor;border-top-color:transparent;animation:s 0.7s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
pre{margin:0;padding:12px 14px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all;max-height:70vh;overflow:auto;color:#cfe3ff;background:#070b14}
.meta{display:flex;flex-wrap:wrap;gap:8px;padding:10px 14px;font-size:11px;color:var(--mute);border-bottom:1px solid var(--line);background:#0a0f1a}
.meta b{color:var(--text);font-weight:600}
.kv{display:flex;gap:6px;align-items:center;background:var(--chip);padding:3px 8px;border-radius:999px}
.copy{margin-left:auto;font-size:11px;color:var(--mute);cursor:pointer}
.copy:hover{color:var(--text)}
.hint{font-size:11px;color:var(--mute);margin-top:4px}
footer{color:var(--mute);text-align:center;font-size:11px;padding:12px}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>EzSolver Playground</h1>
  <span class="tag">POST /solve &bull; POST /solve-challenge</span>
</header>
<main>
  <section class="card">
    <div class="tabs">
      <div class="tab active" data-tab="solve">Turnstile (/solve)</div>
      <div class="tab" data-tab="challenge">CF Challenge (/solve-challenge)</div>
      <div class="tab" data-tab="raw">Raw JSON</div>
    </div>

    <div class="tab-panel active" data-panel="solve">
      <div class="body">
        <label>Site URL</label>
        <input id="s_url" placeholder="https://www.instveed.com/en" value="https://www.instveed.com/en">
        <label>Site Key</label>
        <input id="s_key" placeholder="0x4AAAAAAC3x1H..." value="0x4AAAAAAC3x1HgHsaQcGwbt">
        <div class="row">
          <div><label>Timeout (s)</label><input id="s_to" type="number" value="45"></div>
        </div>
        <div class="chiprow">
          <span class="chip" data-preset="solve" data-url="https://www.instveed.com/en" data-key="0x4AAAAAAC3x1HgHsaQcGwbt">instveed</span>
        </div>
        <div class="actions">
          <button id="run_solve">Send</button>
          <span class="status" id="s_status">ready</span>
        </div>
        <div class="hint">Returns a Turnstile token. Clears the widget by auto-clicking the iframe.</div>
      </div>
    </div>

    <div class="tab-panel" data-panel="challenge">
      <div class="body">
        <label>Site URL</label>
        <input id="c_url" placeholder="https://api.sawit.biz.id/docs" value="https://api.sawit.biz.id/docs">
        <div class="row">
          <div><label>Timeout (s)</label><input id="c_to" type="number" value="60"></div>
        </div>
        <div class="chiprow">
          <span class="chip" data-preset="challenge" data-url="https://api.sawit.biz.id/docs">sawit /docs</span>
          <span class="chip" data-preset="challenge" data-url="https://cobalt.meowing.de/">cobalt</span>
          <span class="chip" data-preset="challenge" data-url="https://nopecha.com/demo/cloudflare">nopecha demo</span>
        </div>
        <div class="actions">
          <button id="run_challenge">Send</button>
          <span class="status" id="c_status">ready</span>
        </div>
        <div class="hint">Clears &quot;Just a moment&hellip;&quot; challenges and returns cookies + final HTML.</div>
      </div>
    </div>

    <div class="tab-panel" data-panel="raw">
      <div class="body">
        <label>Endpoint</label>
        <select id="r_ep"><option>/solve</option><option>/solve-challenge</option></select>
        <label>JSON body</label>
        <textarea id="r_body">{
  "sitekey": "0x4AAAAAAC3x1HgHsaQcGwbt",
  "siteurl": "https://www.instveed.com/en",
  "timeout": 45
}</textarea>
        <div class="actions">
          <button id="run_raw">Send</button>
          <span class="status" id="r_status">ready</span>
        </div>
      </div>
    </div>
  </section>

  <section class="card">
    <h2>Response <span class="copy" id="copy">copy</span></h2>
    <div class="meta" id="meta"><span class="kv"><b>status</b> &mdash;</span><span class="kv"><b>time</b> &mdash;</span></div>
    <pre id="out">// response appears here</pre>
  </section>
</main>
<footer>EzSolver &middot; <a href="/health" style="color:var(--mute)">health</a></footer>
<script>
const $ = (id) => document.getElementById(id);
const tabs = document.querySelectorAll(".tab");
tabs.forEach(t => t.addEventListener("click", () => {
  tabs.forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  document.querySelector(`.tab-panel[data-panel="${t.dataset.tab}"]`).classList.add("active");
}));
document.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => {
  if (c.dataset.preset === "solve") {
    $("s_url").value = c.dataset.url;
    $("s_key").value = c.dataset.key;
  } else if (c.dataset.preset === "challenge") {
    $("c_url").value = c.dataset.url;
  }
}));
function setStatus(el, state, text) {
  el.className = "status " + state;
  el.innerHTML = state === "run" ? `<span class="spin"></span> ${text}` : text;
}
function show(meta, body, t0, ok) {
  const ms = Math.round(performance.now() - t0);
  const statusChip = `<span class="kv"><b>status</b> ${ok.status}</span>`;
  const timeChip = `<span class="kv"><b>time</b> ${(ms/1000).toFixed(2)}s</span>`;
  const extra = body && body.elapsed != null ? `<span class="kv"><b>server</b> ${body.elapsed}s</span>` : "";
  meta.innerHTML = statusChip + timeChip + extra;
  $("out").textContent = typeof body === "string" ? body : JSON.stringify(body, null, 2);
}
async function send(ep, payload, statusEl) {
  setStatus(statusEl, "run", "sending...");
  const t0 = performance.now();
  try {
    const r = await fetch(ep, { method: "POST", headers: { "Content-Type":"application/json" }, body: JSON.stringify(payload) });
    const ct = r.headers.get("content-type") || "";
    const body = ct.includes("json") ? await r.json() : await r.text();
    show($("meta"), body, t0, r);
    setStatus(statusEl, r.ok ? "ok" : "err", r.ok ? `ok ${r.status}` : `error ${r.status}`);
  } catch (e) {
    show($("meta"), { error: String(e) }, t0, { status: "net-err" });
    setStatus(statusEl, "err", "network error");
  }
}
$("run_solve").onclick = () => send("/solve", {
  sitekey: $("s_key").value.trim(),
  siteurl: $("s_url").value.trim(),
  timeout: parseInt($("s_to").value) || 45,
}, $("s_status"));
$("run_challenge").onclick = () => send("/solve-challenge", {
  siteurl: $("c_url").value.trim(),
  timeout: parseInt($("c_to").value) || 60,
}, $("c_status"));
$("run_raw").onclick = () => {
  let payload;
  try { payload = JSON.parse($("r_body").value); }
  catch (e) { setStatus($("r_status"), "err", "invalid JSON"); return; }
  send($("r_ep").value, payload, $("r_status"));
};
$("copy").onclick = () => navigator.clipboard.writeText($("out").textContent).then(() => {
  const el = $("copy"); el.textContent = "copied"; setTimeout(() => el.textContent = "copy", 1000);
});
</script>
</body>
</html>"""


async def handle_playground(request: web.Request) -> web.Response:
    return web.Response(text=_PLAYGROUND_HTML, content_type="text/html")


async def handle_health(request: web.Request) -> web.Response:
    # Don't force-launch the browser from the healthcheck when a challenge
    # proxy is configured - that caused restart loops previously.
    proxy_url, proxy_kind = _challenge_proxy()
    if proxy_url:
        return web.json_response({
            "status": "ok",
            "mode": proxy_kind,
            "proxy_url": proxy_url,
            **_stats,
        })
    pool = await get_pool()
    return web.json_response({
        "status": "ok",
        "max_concurrent": pool.max_concurrent,
        "solved_total": pool.solve_count,
        **_stats,
    })


async def on_startup(app):
    proxy_url, proxy_kind = _challenge_proxy()
    if proxy_url:
        print(f"[solver] {proxy_kind} delegation enabled ({proxy_url}); browser lazy", flush=True)
        return
    pool = await get_pool(MAX_WORKERS)
    print(f"[solver] browser ready, MAX_WORKERS={pool.max_concurrent}", flush=True)


async def on_cleanup(app):
    # Avoid spinning up the browser just to stop it when FS is handling traffic.
    import solver as _s
    if _s._pool is None:
        return
    await _s._pool.shutdown()


def main():
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    sys.stderr = sys.stdout

    # Silence all existing loggers — we only want the pretty block from _emit().
    logging.basicConfig(level=logging.ERROR, stream=sys.stdout)
    for name in ("solver", "service", "aiohttp.access", "aiohttp.server",
                 "aiohttp.web", "nodriver", "nodriver.core.browser"):
        logging.getLogger(name).setLevel(logging.ERROR)
        logging.getLogger(name).propagate = False

    xvfb = _ensure_display()
    app = web.Application()
    app.router.add_get("/", handle_playground)
    app.router.add_post("/solve", handle_solve)
    app.router.add_post("/solve-challenge", handle_challenge)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"[solver] listening on http://0.0.0.0:{PORT}", flush=True)
    try:
        web.run_app(app, host="0.0.0.0", port=PORT, print=None, access_log=None)
    finally:
        if xvfb:
            xvfb.terminate()


if __name__ == "__main__":
    main()
