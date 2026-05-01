"""
Cloudflare Turnstile + JS-Challenge ("Just a moment...") solver.

Design notes:
  - Single warm Camoufox (stealth Firefox) browser with a persistent
    profile. Camoufox replaces nodriver because CF fingerprints
    patchright/nodriver on current Cloudflare deploys so the Turnstile
    iframe never mounts. Camoufox + its built-in human-like mouse model
    reliably clears the widget.
  - New page per request via `browser.new_page()`, closed after solve.
  - Solves serialised through a lock: concurrent tabs hitting the same
    sitekey make CF escalate difficulty. HTTP callers can still fire in
    parallel - requests queue here.
  - No hardcoded sleeps. Event-driven waits via page.wait_for_function.
  - Two public entry points:
        solve_async(sitekey, siteurl) -> str
        solve_challenge_async(siteurl) -> dict   # cleared cookies + html

  - solve_challenge_async optionally delegates to a challenge proxy
    (Byparr or FlareSolverr) when CHALLENGE_PROXY_URL / FLARESOLVERR_URL
    is set. The in-process browser is the fallback.
"""

import asyncio
import json
import logging
import os
import random
import time
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from camoufox.async_api import AsyncCamoufox


log = logging.getLogger("solver")


def _step(req_id: str, msg: str):
    """One-line stdout progress log, visible between the NEW REQUEST block."""
    print(f"  [{req_id}] {msg}", flush=True)


# ---------- Profile + headless mode ----------

def _get_profile_dir() -> str:
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    return "/tmp/ts_profile"


def _headless_mode():
    # Camoufox accepts False, True, or 'virtual'. 'virtual' spawns its own
    # Xvfb internally so we don't need one in the image.
    mode = os.environ.get("CAMOUFOX_HEADLESS", "virtual").lower()
    if mode in ("true", "1"):
        return True
    if mode in ("false", "0"):
        return False
    return "virtual"


# ---------- Singleton browser ----------

class BrowserSingleton:
    def __init__(self, max_concurrent: int):
        self._camoufox: Optional[AsyncCamoufox] = None
        self.browser = None  # playwright BrowserContext when launched with user_data_dir
        self.sem = asyncio.Semaphore(max_concurrent)
        self.solve_lock = asyncio.Lock()
        self.max_concurrent = max_concurrent
        self._start_lock = asyncio.Lock()
        self.solve_count = 0
        self.stopped = False

    async def ensure(self):
        async with self._start_lock:
            if self.browser is not None and not self.stopped:
                return
            profile = _get_profile_dir()
            os.makedirs(profile, exist_ok=True)
            log.info("launching camoufox profile=%s", profile)
            self._camoufox = AsyncCamoufox(
                headless=_headless_mode(),
                humanize=True,
                persistent_context=True,
                user_data_dir=profile,
                os=["windows", "macos", "linux"],
                locale="en-US",
            )
            # AsyncCamoufox is an async context manager. Enter it
            # manually so the BrowserContext survives beyond a `with`
            # block and can be reused across many HTTP requests.
            self.browser = await self._camoufox.__aenter__()
            self.stopped = False
            log.info("camoufox ready")

    async def new_page(self, url: str):
        await self.ensure()
        page = await self.browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.warning("initial goto failed: %s", e)
        return page

    async def shutdown(self):
        if self.stopped:
            return
        self.stopped = True
        if self._camoufox is not None:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:
                pass
        self._camoufox = None
        self.browser = None


_pool: Optional[BrowserSingleton] = None
_pool_lock: Optional[asyncio.Lock] = None


async def get_pool(size: Optional[int] = None) -> BrowserSingleton:
    global _pool, _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is None:
            n = size if size is not None else int(os.environ.get("MAX_WORKERS", 8))
            _pool = BrowserSingleton(n)
            await _pool.ensure()
        return _pool


# ---------- Turnstile injection ----------

_INJECT_JS_TEMPLATE = """
(() => {
    if (document.getElementById('_ts_box')) return;
    window._tsToken = null;
    const wrap = document.createElement('div');
    wrap.id = '_ts_box';
    wrap.style = 'position:fixed;top:20px;left:20px;z-index:2147483647;';
    document.body.appendChild(wrap);
    window._tsLoad = function () {
        turnstile.render('#_ts_box', {
            sitekey: '__SITEKEY__',
            callback: function(t) { window._tsToken = t; }
        });
    };
    if (typeof turnstile !== 'undefined') {
        window._tsLoad();
    } else {
        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_tsLoad&render=explicit';
        s.async = true;
        document.head.appendChild(s);
    }
})();
"""

_GET_TOKEN_JS = """
(() => {
    if (window._tsToken) return window._tsToken;
    const inp = document.querySelector('#_ts_box [name="cf-turnstile-response"]');
    return (inp && inp.value) ? inp.value : null;
})()
"""

_GET_IFRAME_RECT_JS = """
(() => {
    for (const f of document.querySelectorAll('iframe')) {
        const src = f.src || f.getAttribute('src') || '';
        if (!src.includes('challenges.cloudflare.com')) continue;
        const r = f.getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
    }
    return null;
})()
"""

_IS_CHALLENGE_JS = """
(() => {
    if (document.title.toLowerCase().includes('just a moment')) return true;
    if (document.querySelector('div.challenge-form, #challenge-form, .ray-id')) return true;
    if (document.querySelector('iframe[src*="challenges.cloudflare.com/cdn-cgi"]')) return true;
    return false;
})()
"""


async def _turnstile_on_page(page, sitekey: str, req_id: str, timeout: int) -> str:
    loop = asyncio.get_event_loop()
    t0 = loop.time()

    _step(req_id, "waiting for page load...")
    try:
        await page.wait_for_load_state("load", timeout=15_000)
    except Exception:
        pass
    _step(req_id, f"page loaded ({loop.time() - t0:.1f}s)")

    _step(req_id, "injecting turnstile widget")
    await page.evaluate(_INJECT_JS_TEMPLATE.replace("__SITEKEY__", sitekey))

    try:
        await page.wait_for_function(
            'typeof turnstile !== "undefined" && !!document.getElementById("_ts_box")',
            timeout=10_000,
        )
    except Exception:
        raise RuntimeError("turnstile api.js did not load")
    _step(req_id, f"turnstile ready ({loop.time() - t0:.1f}s)")

    deadline = t0 + timeout
    rect = None
    fallback_rect = {"x": 20.0, "y": 20.0, "w": 300.0, "h": 65.0}
    clicks = 0
    last_click = 0.0

    while loop.time() < deadline:
        token = await page.evaluate(_GET_TOKEN_JS)
        if token:
            _step(req_id, f"token obtained ({loop.time() - t0:.1f}s)")
            return token

        if rect is None:
            try:
                rect = await page.evaluate(_GET_IFRAME_RECT_JS)
                if rect:
                    _step(req_id, f"iframe detected at ({rect['x']:.0f},{rect['y']:.0f})")
            except Exception:
                rect = None

        now = loop.time()
        target = rect or fallback_rect

        if clicks == 0 or (now - last_click > 6 and clicks < 3):
            cx = target["x"] + 28 + random.uniform(-3, 3)
            cy = target["y"] + target["h"] / 2 + random.uniform(-3, 3)
            _step(req_id, f"click #{clicks + 1} at ({cx:.0f},{cy:.0f})")
            try:
                await page.mouse.move(cx - 60, cy - 20)
                await asyncio.sleep(0.05)
                await page.mouse.move(cx, cy)
                await asyncio.sleep(0.03)
                await page.mouse.click(cx, cy)
            except Exception as e:
                _step(req_id, f"click error: {e}")
            last_click = now
            clicks += 1

        await asyncio.sleep(0.2)

    raise TimeoutError(f"turnstile timeout after {timeout}s")


async def solve_async(sitekey: str, siteurl: str, req_id: str = "-",
                      timeout: int = 45) -> str:
    pool = await get_pool()
    async with pool.sem:
        async with pool.solve_lock:
            _step(req_id, f"opening tab -> {siteurl}")
            page = None
            try:
                page = await pool.new_page(siteurl)
                return await _turnstile_on_page(page, sitekey, req_id, timeout)
            finally:
                pool.solve_count += 1
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass


# ---------- JS Challenge ("Just a moment...") ----------

_CF_WIDGET_RECT_JS = """
(() => {
    for (const f of document.querySelectorAll('iframe')) {
        const src = f.src || f.getAttribute('src') || '';
        if (!src.includes('challenges.cloudflare.com')) continue;
        const r = f.getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
    }
    const el = document.querySelector('#hQLfM7, .main-wrapper .ch-title-zone + div');
    if (el) {
        const r = el.getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
    }
    return null;
})()
"""


def _match_host(target_host: str, cdomain: str) -> bool:
    d = (cdomain or "").lstrip(".").lower()
    h = (target_host or "").lower()
    return bool(h) and (h == d or h.endswith("." + d))


def _challenge_proxy() -> tuple[Optional[str], str]:
    """Return (base_url, kind) for the configured challenge proxy."""
    url = os.environ.get("CHALLENGE_PROXY_URL") or os.environ.get("FLARESOLVERR_URL") or ""
    url = url.rstrip("/")
    if not url:
        return None, ""
    kind = (os.environ.get("CHALLENGE_PROXY_KIND")
            or ("flaresolverr" if os.environ.get("FLARESOLVERR_URL") else "byparr")).lower()
    return url, kind


async def _solve_via_proxy(siteurl: str, req_id: str, timeout: int) -> Optional[dict]:
    url, kind = _challenge_proxy()
    if not url:
        return None
    _step(req_id, f"delegating to {kind} -> {url}")

    candidates = [siteurl]
    try:
        u = urlparse(siteurl)
        if u.path and not u.path.endswith("/") and "." not in u.path.rsplit("/", 1)[-1] and not u.query:
            fixed = siteurl.rstrip() + "/"
            if fixed != siteurl:
                candidates.append(fixed)
    except Exception:
        pass

    if kind == "byparr":
        payload_base = {"cmd": "request.get", "max_timeout": max(5, timeout)}
    else:
        payload_base = {"cmd": "request.get", "maxTimeout": max(5000, timeout * 1000)}

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    data = None
    last_err = None
    for i, try_url in enumerate(candidates):
        if i:
            _step(req_id, f"retrying with trailing slash -> {try_url}")
        payload = {**payload_base, "url": try_url}
        try:
            conn_timeout = aiohttp.ClientTimeout(total=timeout + 15)
            async with aiohttp.ClientSession(timeout=conn_timeout) as s:
                async with s.post(f"{url}/v1", json=payload) as resp:
                    body_text = await resp.text()
                    if resp.status == 200:
                        data = json.loads(body_text)
                        if (data.get("status") or "").lower() == "ok":
                            break
                        last_err = f"{kind}: {data.get('message') or data}"
                        data = None
                        continue
                    last_err = f"{kind} HTTP {resp.status}: {body_text[:200]}"
        except asyncio.TimeoutError:
            last_err = f"{kind} did not respond within {timeout + 15}s"
        except aiohttp.ClientError as e:
            last_err = f"{kind} connection error: {e}"

    if data is None:
        raise RuntimeError(last_err or f"{kind}: unknown failure")

    sol = data.get("solution") or {}
    final_url = sol.get("url") or siteurl
    target_host = urlparse(final_url).hostname or ""
    raw_cookies = sol.get("cookies") or []
    cookies = []
    for c in raw_cookies:
        if not _match_host(target_host, c.get("domain", "")):
            continue
        cookies.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "expires": c.get("expiry") if c.get("expiry") is not None else c.get("expires", -1),
        })

    html = sol.get("response") or ""
    title = ""
    low = html.lower()
    a = low.find("<title")
    if a != -1:
        b = low.find(">", a)
        c_ = low.find("</title>", b)
        if b != -1 and c_ != -1:
            title = html[b + 1:c_].strip()

    user_agent = sol.get("userAgent") or sol.get("user_agent") or ""
    _step(req_id, f"{kind} cleared ({loop.time() - t0:.1f}s, cookies={len(cookies)})")
    return {
        "url": final_url,
        "title": title,
        "user_agent": user_agent,
        "cookies": cookies,
        "html": html,
    }


# Back-compat alias
_solve_via_flaresolverr = _solve_via_proxy


async def solve_challenge_async(siteurl: str, req_id: str = "-",
                                 timeout: int = 45) -> dict:
    """Open page, wait for CF challenge to clear, return cookies + final html."""
    proxy_url, proxy_kind = _challenge_proxy()
    if proxy_url:
        try:
            result = await _solve_via_proxy(siteurl, req_id, timeout)
            if result is not None:
                return result
        except Exception as e:
            _step(req_id, f"{proxy_kind or 'proxy'} failed, falling back to camoufox: {e}")

    pool = await get_pool()
    async with pool.sem:
        async with pool.solve_lock:
            _step(req_id, f"opening tab -> {siteurl}")
            page = None
            try:
                page = await pool.new_page(siteurl)
                loop = asyncio.get_event_loop()
                t0 = loop.time()
                _step(req_id, "waiting for navigation...")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                _step(req_id, f"page loaded ({loop.time() - t0:.1f}s)")

                deadline = t0 + timeout
                cleared = False
                attempts = 0
                clicks = 0
                last_click = 0.0

                while loop.time() < deadline:
                    is_challenge = await page.evaluate(_IS_CHALLENGE_JS)
                    if not is_challenge:
                        cleared = True
                        break
                    attempts += 1
                    if attempts == 1:
                        _step(req_id, "CF challenge detected, waiting for clear...")

                    now = loop.time()
                    if clicks < 3 and (clicks == 0 or now - last_click > 6):
                        rect = await page.evaluate(_CF_WIDGET_RECT_JS)
                        if rect:
                            cx = rect["x"] + 28 + random.uniform(-3, 3)
                            cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                            _step(req_id, f"interactive click #{clicks + 1} at ({cx:.0f},{cy:.0f})")
                            try:
                                await page.mouse.move(cx - 60, cy - 20)
                                await asyncio.sleep(0.05)
                                await page.mouse.move(cx, cy)
                                await asyncio.sleep(0.03)
                                await page.mouse.click(cx, cy)
                            except Exception as e:
                                _step(req_id, f"click error: {e}")
                            last_click = now
                            clicks += 1
                    await asyncio.sleep(0.3)

                if not cleared:
                    raise TimeoutError(f"challenge did not clear within {timeout}s")

                final_url = page.url
                title = await page.title()
                user_agent = await page.evaluate("navigator.userAgent")
                html = await page.content()
                target_host = urlparse(final_url or siteurl).hostname or ""
                try:
                    raw_cookies = await pool.browser.cookies()
                    cookies = [
                        {"name": c["name"], "value": c["value"], "domain": c["domain"],
                         "path": c["path"], "expires": c.get("expires", -1)}
                        for c in raw_cookies
                        if _match_host(target_host, c.get("domain", ""))
                    ]
                except Exception as e:
                    _step(req_id, f"cookie fetch failed: {e}")
                    cookies = []

                _step(req_id, f"challenge cleared ({loop.time() - t0:.1f}s, attempts={attempts})")
                return {
                    "url": final_url,
                    "title": title,
                    "user_agent": user_agent,
                    "cookies": cookies,
                    "html": html,
                }
            finally:
                pool.solve_count += 1
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass


def solve(sitekey: str, siteurl: str, timeout: int = 45) -> str:
    """Legacy sync wrapper."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return asyncio.run(solve_async(sitekey, siteurl, timeout=timeout))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    if len(sys.argv) < 3:
        print("Usage: python solver.py <sitekey> <siteurl>")
        sys.exit(1)
    t0 = time.time()
    tok = solve(sys.argv[1], sys.argv[2])
    print(f"{tok}\nelapsed: {time.time()-t0:.2f}s")
