"""
Violetics Solver - Turnstile + CF JS-Challenge HTTP service (aiohttp).
"""

import collections
import json
import logging
import os
import sys
import time
import uuid
from urllib.parse import urlparse

from aiohttp import web

from .solver import (get_pool, solve_async, solve_challenge_async,
                     solve_recaptcha_v3_async, solve_aws_token_async,
                     _challenge_proxy)


PORT = int(os.environ.get("PORT", 9988))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 8))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 64 * 1024))  # 64 KB
# API key gate. Empty -> auth disabled (dev). Set API_KEY to require the
# X-API-Key header (or ?api_key=) on /solve and /solve-challenge.
API_KEY = os.environ.get("API_KEY", "").strip()

log = logging.getLogger("service")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(_ROOT, "web")
TEMPLATE_DIR = os.path.join(WEB_DIR, "templates")
STATIC_DIR = os.path.join(WEB_DIR, "static")

_PROCESS_STARTED = time.time()


_stats = {"in_flight": 0, "solved": 0, "errors": 0, "challenges": 0}

# Recent events for the playground's live stats panel. Capped ring buffer so
# an idle process does not accumulate memory. Each entry: {ts, endpoint,
# status, duration, url, summary}.
_events: "collections.deque[dict]" = collections.deque(maxlen=50)
_latency_ms: "collections.deque[float]" = collections.deque(maxlen=100)


def _rid() -> str:
    return uuid.uuid4().hex[:8]


def _classify_error(exc: BaseException) -> tuple[str, str, int]:
    """Map an exception to (error_code, public_message, http_status).

    Hides Playwright/Camoufox internal stack traces from clients while
    keeping enough signal that callers can branch on the result.
    """
    msg = str(exc).strip()
    low = msg.lower()
    if isinstance(exc, TimeoutError) or "timeout" in low:
        # Don't leak Playwright stack text (e.g. "Page.evaluate: ...") — the
        # full detail still goes to the server log via log.exception.
        return "timeout", "solve timeout", 504
    if any(h in low for h in ("connection closed", "browser has been closed",
                               "context has been closed", "net::err",
                               "ns_error", "navigation timeout")):
        return "browser_error", "browser unavailable, please retry", 503
    if "invalid url" in low or "invalid_url" in low:
        return "bad_request", "invalid siteurl", 400
    if isinstance(exc, ValueError):
        return "bad_request", msg or "bad request", 400
    return "solver_error", "internal solver error", 500


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


# ANSI colors, disabled when NO_COLOR is set or output isn't a TTY.
_USE_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def _emit_start(rid: str, method: str, path: str, url: str, key: str, peer: str):
    # Verbose per-request banner only in DEBUG; at INFO one line is logged on
    # completion (see _emit_end) so concurrent requests stay readable.
    if log.isEnabledFor(logging.DEBUG):
        log.debug("start %s %s %s url=%s key=%s from=%s",
                  rid, method, path, url or "-",
                  (key[:14] + "…") if len(key) > 14 else (key or "-"), peer)


def _emit_end(rid: str, elapsed: float, status: int, body: dict):
    ok = 200 <= status < 400
    icon = _c("32", "✓") if ok else _c("31", "✗")
    stat = _c("32" if ok else "31", str(status))
    dur = _c("33", f"{elapsed:6.2f}s")
    print(f"{icon} {rid} {stat} {dur}  {_summary(body)}", flush=True)


def _validate_siteurl(siteurl: str) -> None:
    """Cheap guard against empty / non-http(s) / no-host URLs.

    Defends the headless browser against attacker-controlled `file://` or
    `chrome://` URLs and gives callers a clean 400 instead of a downstream
    Playwright error.
    """
    if not siteurl:
        raise ValueError("siteurl required")
    try:
        u = urlparse(siteurl)
    except Exception:
        raise ValueError("invalid siteurl")
    if u.scheme not in ("http", "https"):
        raise ValueError("siteurl scheme must be http or https")
    if not u.hostname:
        raise ValueError("siteurl missing host")


# Paths reachable without a key even when API_KEY is set. /health stays open
# for container/uptime probes.
_PUBLIC_PATHS = frozenset({"/health"})


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require the API key on every path except _PUBLIC_PATHS. No-op when
    API_KEY is unset (dev). Key accepted via X-API-Key header or ?api_key=
    so the browser playground can pass it in the URL."""
    if API_KEY and request.path not in _PUBLIC_PATHS:
        given = request.headers.get("X-API-Key") or request.query.get("api_key") or ""
        if given != API_KEY:
            return web.json_response(
                {"error": "unauthorized", "error_code": "unauthorized"}, status=401)
    return await handler(request)


async def _read_payload(request: web.Request) -> dict:
    """Bounded-size JSON body parse. Raises ValueError on bad input."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("invalid JSON")


async def handle_solve(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    method = request.method
    peer = request.remote or "-"

    try:
        payload = await _read_payload(request)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_start(rid, method, path, "", "", peer)
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, "", body)
        return web.json_response(body, status=400)

    sitekey = (payload.get("sitekey") or "").strip()
    siteurl = (payload.get("siteurl") or "").strip()
    try:
        timeout = max(5, min(180, int(payload.get("timeout", 45))))
    except (TypeError, ValueError):
        timeout = 45
    action = payload.get("action") or None
    cdata = payload.get("cdata") or None

    _emit_start(rid, method, path, siteurl, sitekey, peer)

    if not sitekey:
        body = {"error": "sitekey required", "error_code": "bad_request"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)
    try:
        _validate_siteurl(siteurl)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        token = await solve_async(sitekey, siteurl, req_id=rid, timeout=timeout,
                                   action=action, cdata=cdata)
        elapsed = time.time() - t0
        _stats["solved"] += 1
        body = {"token": token, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        _record_event(path, 200, elapsed, siteurl, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        code, public_msg, status = _classify_error(exc)
        # Full detail to the server log; sanitised body to the client.
        log.exception("solve failed rid=%s code=%s", rid, code)
        body = {"error": public_msg, "error_code": code, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, status, body)
        _record_event(path, status, elapsed, siteurl, body)
        return web.json_response(body, status=status)
    finally:
        _stats["in_flight"] -= 1


async def handle_challenge(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    method = request.method
    peer = request.remote or "-"

    try:
        payload = await _read_payload(request)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_start(rid, method, path, "", "", peer)
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, "", body)
        return web.json_response(body, status=400)

    siteurl = (payload.get("siteurl") or "").strip()
    try:
        timeout = max(5, min(180, int(payload.get("timeout", 45))))
    except (TypeError, ValueError):
        timeout = 45

    _emit_start(rid, method, path, siteurl, "", peer)

    try:
        _validate_siteurl(siteurl)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
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
        code, public_msg, status = _classify_error(exc)
        log.exception("challenge failed rid=%s code=%s", rid, code)
        body = {"error": public_msg, "error_code": code, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, status, body)
        _record_event(path, status, elapsed, siteurl, body)
        return web.json_response(body, status=status)
    finally:
        _stats["in_flight"] -= 1


async def handle_recaptcha(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    peer = request.remote or "-"

    try:
        payload = await _read_payload(request)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_start(rid, request.method, path, "", "", peer)
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, "", body)
        return web.json_response(body, status=400)

    sitekey = (payload.get("sitekey") or "").strip()
    siteurl = (payload.get("siteurl") or "").strip()
    action = (payload.get("action") or "verify").strip()
    try:
        timeout = max(5, min(180, int(payload.get("timeout", 45))))
    except (TypeError, ValueError):
        timeout = 45

    _emit_start(rid, request.method, path, siteurl, sitekey, peer)

    if not sitekey:
        body = {"error": "sitekey required", "error_code": "bad_request"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)
    try:
        _validate_siteurl(siteurl)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        token = await solve_recaptcha_v3_async(sitekey, siteurl, req_id=rid,
                                               timeout=timeout, action=action)
        elapsed = time.time() - t0
        _stats["solved"] += 1
        body = {"token": token, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        _record_event(path, 200, elapsed, siteurl, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        code, public_msg, status = _classify_error(exc)
        log.exception("recaptcha failed rid=%s code=%s", rid, code)
        body = {"error": public_msg, "error_code": code, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, status, body)
        _record_event(path, status, elapsed, siteurl, body)
        return web.json_response(body, status=status)
    finally:
        _stats["in_flight"] -= 1


async def handle_aws_token(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    peer = request.remote or "-"

    try:
        payload = await _read_payload(request)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_start(rid, request.method, path, "", "", peer)
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, "", body)
        return web.json_response(body, status=400)

    siteurl = (payload.get("siteurl") or "").strip()
    try:
        timeout = max(5, min(180, int(payload.get("timeout", 45))))
    except (TypeError, ValueError):
        timeout = 45

    _emit_start(rid, request.method, path, siteurl, "", peer)

    try:
        _validate_siteurl(siteurl)
    except ValueError as ve:
        body = {"error": str(ve), "error_code": "bad_request"}
        _emit_end(rid, time.time() - t0, 400, body)
        _record_event(path, 400, time.time() - t0, siteurl, body)
        return web.json_response(body, status=400)

    _stats["in_flight"] += 1
    try:
        result = await solve_aws_token_async(siteurl, req_id=rid, timeout=timeout)
        elapsed = time.time() - t0
        _stats["challenges"] += 1
        body = {**result, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        _record_event(path, 200, elapsed, siteurl, body)
        return web.json_response(body)
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        code, public_msg, status = _classify_error(exc)
        log.exception("aws-token failed rid=%s code=%s", rid, code)
        body = {"error": public_msg, "error_code": code, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, status, body)
        _record_event(path, status, elapsed, siteurl, body)
        return web.json_response(body, status=status)
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


_warp_cache = {"ts": 0.0, "state": "unknown"}


async def _warp_state() -> str:
    """warp=on/off from Cloudflare's trace endpoint, fetched through the same
    egress proxy the browser uses so it reflects real solve traffic. Cached
    30s so health polling doesn't hammer it. 'unknown' if the check fails."""
    import aiohttp
    from .solver import _solver_proxy
    if time.time() - _warp_cache["ts"] < 30:
        return _warp_cache["state"]
    state = "unknown"
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get("https://www.cloudflare.com/cdn-cgi/trace",
                             proxy=_solver_proxy()) as r:
                text = await r.text()
        for line in text.splitlines():
            if line.startswith("warp="):
                state = line.split("=", 1)[1] or "off"
                break
    except Exception:
        state = "unknown"
    _warp_cache.update(ts=time.time(), state=state)
    return state


async def handle_health(request: web.Request) -> web.Response:
    # Don't force-launch the browser from the healthcheck when a challenge
    # proxy is configured - that caused restart loops previously.
    warp = await _warp_state()
    proxy_url, proxy_kind = _challenge_proxy()
    if proxy_url:
        return web.json_response({
            "status": "ok",
            "mode": proxy_kind,
            "proxy_url": proxy_url,
            "warp": warp,
            **_stats,
        })
    pool = await get_pool()
    return web.json_response({
        "status": "ok",
        "max_concurrent": pool.max_concurrent,
        "solved_total": pool.solve_count,
        "warp": warp,
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
    # Always warm the browser at startup — /solve still routes through
    # Camoufox even when a challenge proxy is configured. Lazy-loading the
    # browser meant the first /solve paid a ~30s cold-start tax.
    pool = await get_pool(MAX_WORKERS)
    if proxy_url:
        print(f"[solver] {proxy_kind} delegation enabled ({proxy_url}); browser warm, "
              f"MAX_WORKERS={pool.max_concurrent}", flush=True)
    else:
        print(f"[solver] browser warm, MAX_WORKERS={pool.max_concurrent}", flush=True)


async def on_cleanup(app):
    from . import solver as _s
    if _s._pool is None:
        return
    await _s._pool.shutdown()


def main():
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    sys.stderr = sys.stdout

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Keep the noisy libs quiet but let our own loggers through so
    # `log.exception` is actually visible for debugging.
    for name in ("aiohttp.access", "aiohttp.server", "aiohttp.web",
                 "camoufox", "playwright", "nodriver", "nodriver.core.browser"):
        logging.getLogger(name).setLevel(logging.WARNING)

    app = web.Application(client_max_size=MAX_BODY_BYTES,
                          middlewares=[auth_middleware])
    if API_KEY:
        print("[solver] API key auth ENABLED", flush=True)
    app.router.add_get("/", handle_playground)
    app.router.add_post("/solve", handle_solve)
    app.router.add_post("/solve-challenge", handle_challenge)
    app.router.add_post("/recaptcha-v3", handle_recaptcha)
    app.router.add_post("/aws-token", handle_aws_token)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stats", handle_stats)
    if os.path.isdir(STATIC_DIR):
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"[solver] listening on http://0.0.0.0:{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
