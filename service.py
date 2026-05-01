"""
Violetics Solver - Turnstile + CF JS-Challenge HTTP service (aiohttp).
"""

import collections
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

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TEMPLATE_DIR = os.path.join(WEB_DIR, "templates")
STATIC_DIR = os.path.join(WEB_DIR, "static")

_PROCESS_STARTED = time.time()


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

# Recent events for the playground's live stats panel. Capped ring buffer so
# an idle process does not accumulate memory. Each entry: {ts, endpoint,
# status, duration, url, summary}.
_events: "collections.deque[dict]" = collections.deque(maxlen=50)
_latency_ms: "collections.deque[float]" = collections.deque(maxlen=100)


def _rid() -> str:
    return uuid.uuid4().hex[:8]


def _summary(body: dict) -> str:
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


def _record_event(endpoint: str, status: int, duration: float, url: str, body: dict):
    _events.appendleft({
        "ts": time.time(),
        "endpoint": endpoint,
        "status": status,
        "duration": round(duration, 3),
        "url": url,
        "summary": _summary(body)[:180],
    })
    _latency_ms.append(duration * 1000)


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
        _record_event(path, 400, time.time() - t0, "", body)
        return web.json_response(body, status=400)

    sitekey = (payload.get("sitekey") or "").strip()
    siteurl = (payload.get("siteurl") or "").strip()
    timeout = int(payload.get("timeout", 45))

    _emit_start(rid, method, path, siteurl, sitekey, peer)

    if not sitekey or not siteurl:
        body = {"error": "sitekey and siteurl required"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        token = await solve_async(sitekey, siteurl, req_id=rid, timeout=timeout)
        elapsed = time.time() - t0
        _stats["solved"] += 1
        body = {"token": token, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        _record_event(path, 200, elapsed, siteurl, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        body = {"error": str(exc), "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 500, body)
        _record_event(path, 500, elapsed, siteurl, body)
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
        _record_event(path, 400, time.time() - t0, "", body)
        return web.json_response(body, status=400)

    siteurl = (payload.get("siteurl") or "").strip()
    timeout = int(payload.get("timeout", 45))

    _emit_start(rid, method, path, siteurl, "", peer)

    if not siteurl:
        body = {"error": "siteurl required"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        result = await solve_challenge_async(siteurl, req_id=rid, timeout=timeout)
        elapsed = time.time() - t0
        _stats["challenges"] += 1
        body = {**result, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        _record_event(path, 200, elapsed, siteurl, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        body = {"error": str(exc), "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 500, body)
        _record_event(path, 500, elapsed, siteurl, body)
        return web.json_response(body, status=500)
    finally:
        _stats["in_flight"] -= 1


async def handle_playground(request: web.Request) -> web.Response:
    index_path = os.path.join(TEMPLATE_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return web.Response(text="playground template missing", status=500)
    return web.Response(text=html, content_type="text/html")


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


async def handle_stats(request: web.Request) -> web.Response:
    proxy_url, proxy_kind = _challenge_proxy()
    lat = list(_latency_ms)
    lat_sorted = sorted(lat)
    def pct(arr, p):
        if not arr:
            return 0
        k = min(len(arr) - 1, int(round((p / 100) * (len(arr) - 1))))
        return round(arr[k], 0)
    avg = round(sum(lat) / len(lat), 0) if lat else 0
    total = _stats["solved"] + _stats["challenges"] + _stats["errors"]
    success_rate = round(((_stats["solved"] + _stats["challenges"]) / total) * 100, 1) if total else 0.0
    return web.json_response({
        "uptime": round(time.time() - _PROCESS_STARTED, 1),
        "mode": proxy_kind or "nodriver",
        "proxy_url": proxy_url or None,
        **_stats,
        "total_requests": total,
        "success_rate": success_rate,
        "latency_ms": {"avg": avg, "p50": pct(lat_sorted, 50), "p95": pct(lat_sorted, 95), "samples": len(lat)},
        "events": list(_events),
    })


async def on_startup(app):
    proxy_url, proxy_kind = _challenge_proxy()
    if proxy_url:
        print(f"[solver] {proxy_kind} delegation enabled ({proxy_url}); browser lazy", flush=True)
        return
    pool = await get_pool(MAX_WORKERS)
    print(f"[solver] browser ready, MAX_WORKERS={pool.max_concurrent}", flush=True)


async def on_cleanup(app):
    import solver as _s
    if _s._pool is None:
        return
    await _s._pool.shutdown()


def main():
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    sys.stderr = sys.stdout

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
    app.router.add_get("/stats", handle_stats)
    if os.path.isdir(STATIC_DIR):
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
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
