"""
Cloudflare Turnstile + JS-Challenge ("Just a moment...") solver.

Design notes:
  - Single warm browser, persistent profile - fresh profiles get hard
    challenges from Cloudflare (~45s) while a warm profile typically
    clears in 5-10s.
  - Tab-per-request via new_tab=True, closed after solve.
  - Solves are internally serialised with a lock: concurrent tabs hitting
    the same sitekey cause CF to escalate difficulty, so real parallelism
    is not practical. HTTP callers can still fire in parallel - requests
    queue here.
  - No hardcoded sleeps. Event-driven waits on readyState, turnstile
    global, token, or cf_chl_opt absence.
  - Two public entry points:
        solve_async(sitekey, siteurl) -> str
        solve_challenge_async(siteurl) -> dict   # cleared cookies + html

  - solve_challenge_async optionally delegates to a FlareSolverr instance
    when FLARESOLVERR_URL is set. nodriver/patchright on headless
    Linux hosts are reliably fingerprinted by current Cloudflare deploys
    (the Turnstile iframe never mounts, so there is nothing to click),
    while FlareSolverr ships its own stealth Chromium build that still
    clears these pages. Fall back to the in-process browser only when
    FlareSolverr is unreachable or explicitly disabled.
"""

import asyncio
import json
import logging
import os
import platform
import random
import subprocess
import sys
import time
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import nodriver as uc


log = logging.getLogger("solver")


def _step(req_id: str, msg: str):
    """One-line stdout progress log, visible between the NEW REQUEST block."""
    print(f"  [{req_id}] {msg}", flush=True)


# ---------- Chrome / Xvfb discovery ----------

def _find_chrome() -> str:
    """Locate a Chrome/Chromium binary."""
    import glob
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]

    pw_roots = [
        os.path.expanduser("~/.cache/ms-playwright"),
        "/root/.cache/ms-playwright",
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    ]
    patterns = [
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-linux*/chrome.exe",
        "chromium_headless_shell-*/chrome-linux*/headless_shell",
    ]
    for root in pw_roots:
        if not root or not os.path.isdir(root):
            continue
        for pat in patterns:
            matches = sorted(glob.glob(os.path.join(root, pat)), reverse=True)
            if matches:
                return matches[0]

    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "Chrome not found. Install via `python -m patchright install chromium` or set CHROME_PATH."
    )


def _get_profile_dir() -> str:
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    if platform.system() == "Windows":
        base = os.environ.get("TEMP") or os.environ.get("TMP") or r"C:\Temp"
        return os.path.join(base, "ts_profile")
    return "/tmp/ts_profile"


def _start_xvfb_if_needed() -> Optional[subprocess.Popen]:
    if platform.system() != "Linux" or os.environ.get("DISPLAY"):
        return None
    proc = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    os.environ["DISPLAY"] = ":99"
    time.sleep(0.5)
    return proc


# ---------- Event-driven waits ----------

async def _wait_for_eval(tab, expr: str, timeout: float, poll: float = 0.1) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            val = await tab.evaluate(expr)
            if val:
                return True
        except Exception:
            pass
        await asyncio.sleep(poll)
    return False


async def _wait_ready(tab, timeout: float = 15.0):
    await _wait_for_eval(tab, 'document.readyState === "complete"', timeout)


# ---------- Singleton browser ----------

class BrowserSingleton:
    def __init__(self, max_concurrent: int):
        self.browser: Optional[uc.Browser] = None
        self.sem = asyncio.Semaphore(max_concurrent)
        self.solve_lock = asyncio.Lock()
        self.max_concurrent = max_concurrent
        self._start_lock = asyncio.Lock()
        self.solve_count = 0

    async def ensure(self):
        async with self._start_lock:
            if self.browser is not None and not self.browser.stopped:
                return
            log.info("launching chrome profile=%s", _get_profile_dir())
            self.browser = await uc.start(
                browser_executable_path=_find_chrome(),
                headless=False,
                no_sandbox=True,
                user_data_dir=_get_profile_dir(),
            )
            try:
                await self.browser.get("about:blank")
            except Exception:
                pass
            log.info("chrome ready")

    async def new_tab(self, url: str):
        await self.ensure()
        tab = await self.browser.get(url, new_tab=True)
        try:
            await tab.activate()
        except Exception:
            pass
        try:
            await tab.bring_to_front()
        except Exception:
            pass
        return tab

    async def shutdown(self):
        if self.browser and not self.browser.stopped:
            try:
                self.browser.stop()
            except Exception:
                pass


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


# ---------- Turnstile solver ----------

_INJECT_JS = """
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
JSON.stringify((() => {
    for (const f of document.querySelectorAll('iframe')) {
        const src = f.src || f.getAttribute('src') || '';
        if (!src.includes('challenges.cloudflare.com')) continue;
        const r = f.getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
    }
    return null;
})())
"""


async def _turnstile_on_tab(tab, sitekey: str, req_id: str, timeout: int) -> str:
    loop = asyncio.get_event_loop()
    t0 = loop.time()

    _step(req_id, "waiting for page load...")
    await _wait_for_eval(
        tab,
        "performance.timing && performance.timing.loadEventEnd > 0",
        timeout=15,
    )
    _step(req_id, f"page loaded ({loop.time() - t0:.1f}s)")

    _step(req_id, "injecting turnstile widget")
    await tab.evaluate(_INJECT_JS.replace("__SITEKEY__", sitekey))

    if not await _wait_for_eval(
        tab,
        'typeof turnstile !== "undefined" && !!document.getElementById("_ts_box")',
        timeout=10,
    ):
        raise RuntimeError("turnstile api.js did not load")
    _step(req_id, f"turnstile ready ({loop.time() - t0:.1f}s)")

    deadline = t0 + timeout
    rect = None
    fallback_rect = {"x": 20.0, "y": 20.0, "w": 300.0, "h": 65.0}
    clicks = 0
    last_click = 0.0

    while loop.time() < deadline:
        token = await tab.evaluate(_GET_TOKEN_JS)
        if token:
            _step(req_id, f"token obtained ({loop.time() - t0:.1f}s)")
            return token

        if rect is None:
            raw = await tab.evaluate(_GET_IFRAME_RECT_JS)
            if raw and raw != "null":
                try:
                    rect = json.loads(raw)
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
                await tab.mouse_move(cx - 50, cy - 20)
                await asyncio.sleep(0.06)
                await tab.mouse_move(cx, cy)
                await asyncio.sleep(0.04)
                await tab.mouse_click(cx, cy)
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
            tab = None
            try:
                tab = await pool.new_tab(siteurl)
                return await _turnstile_on_tab(tab, sitekey, req_id, timeout)
            finally:
                pool.solve_count += 1
                if tab is not None:
                    try:
                        await tab.close()
                    except Exception:
                        pass


# ---------- JS Challenge ("Just a moment...") ----------

_IS_CHALLENGE_JS = """
(() => {
    if (document.title.toLowerCase().includes('just a moment')) return true;
    if (document.querySelector('div.challenge-form, #challenge-form, .ray-id')) return true;
    if (document.querySelector('iframe[src*="challenges.cloudflare.com/cdn-cgi"]')) return true;
    return false;
})()
"""


_CF_WIDGET_RECT_JS = """
JSON.stringify((() => {
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
})())
"""


def _match_host(target_host: str, cdomain: str) -> bool:
    d = (cdomain or "").lstrip(".").lower()
    h = (target_host or "").lower()
    return bool(h) and (h == d or h.endswith("." + d))


async def _solve_via_flaresolverr(siteurl: str, req_id: str, timeout: int) -> Optional[dict]:
    """Delegate JS-challenge clearance to a running FlareSolverr instance.

    FlareSolverr ships a stealth Chromium that reliably renders the CF
    interactive widget in environments where nodriver is fingerprinted
    (docker + Xvfb on VPS ranges CF has flagged). Returns the same shape
    as the in-process path so callers are unchanged.
    """
    url = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
    if not url:
        return None
    _step(req_id, f"delegating to FlareSolverr -> {url}")

    # CF's challenge platform is sensitive to the exact URL — on this
    # deployment api.sawit.biz.id/docs consistently times out inside FS
    # while /docs/ and / succeed. When we see a non-slash-terminated path
    # that has no query, try a trailing-slash variant as a second attempt.
    candidates = [siteurl]
    try:
        u = urlparse(siteurl)
        if u.path and not u.path.endswith("/") and "." not in u.path.rsplit("/", 1)[-1] and not u.query:
            fixed = siteurl.rstrip() + "/"
            if fixed != siteurl:
                candidates.append(fixed)
    except Exception:
        pass

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    data = None
    last_err = None
    for i, try_url in enumerate(candidates):
        if i:
            _step(req_id, f"retrying with trailing slash -> {try_url}")
        payload = {
            "cmd": "request.get",
            "url": try_url,
            "maxTimeout": max(5000, timeout * 1000),
        }
        try:
            conn_timeout = aiohttp.ClientTimeout(total=timeout + 15)
            async with aiohttp.ClientSession(timeout=conn_timeout) as s:
                async with s.post(f"{url}/v1", json=payload) as resp:
                    body_text = await resp.text()
                    if resp.status == 200:
                        data = json.loads(body_text)
                        if (data.get("status") or "").lower() == "ok":
                            break
                        last_err = f"flaresolverr: {data.get('message') or data}"
                        data = None
                        continue
                    last_err = f"flaresolverr HTTP {resp.status}: {body_text[:300]}"
        except asyncio.TimeoutError:
            last_err = f"flaresolverr did not respond within {timeout + 15}s"

    if data is None:
        raise RuntimeError(last_err or "flaresolverr: unknown failure")

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
            "expires": c.get("expiry", -1),
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

    _step(req_id, f"flaresolverr cleared ({loop.time() - t0:.1f}s, cookies={len(cookies)})")
    return {
        "url": final_url,
        "title": title,
        "user_agent": sol.get("userAgent") or "",
        "cookies": cookies,
        "html": html,
    }


async def solve_challenge_async(siteurl: str, req_id: str = "-",
                                 timeout: int = 45) -> dict:
    """Open page, wait for CF challenge to clear, return cookies + final html."""
    if os.environ.get("FLARESOLVERR_URL"):
        try:
            result = await _solve_via_flaresolverr(siteurl, req_id, timeout)
            if result is not None:
                return result
        except Exception as e:
            _step(req_id, f"flaresolverr failed, falling back to nodriver: {e}")

    pool = await get_pool()
    async with pool.sem:
        async with pool.solve_lock:
            _step(req_id, f"opening tab -> {siteurl}")
            tab = None
            try:
                tab = await pool.new_tab(siteurl)
                loop = asyncio.get_event_loop()
                t0 = loop.time()
                _step(req_id, "waiting for navigation...")
                await _wait_for_eval(
                    tab,
                    "location.href && location.href !== 'about:blank'",
                    timeout=15,
                )
                await _wait_ready(tab, timeout=10)
                _step(req_id, f"page loaded ({loop.time() - t0:.1f}s)")

                deadline = t0 + timeout
                cleared = False
                attempts = 0
                clicks = 0
                last_click = 0.0

                while loop.time() < deadline:
                    is_challenge = await tab.evaluate(_IS_CHALLENGE_JS)
                    if not is_challenge:
                        cleared = True
                        break
                    attempts += 1
                    if attempts == 1:
                        _step(req_id, "CF challenge detected, waiting for clear...")

                    now = loop.time()
                    if clicks < 3 and (clicks == 0 or now - last_click > 6):
                        raw = await tab.evaluate(_CF_WIDGET_RECT_JS)
                        if raw and raw != "null":
                            try:
                                rect = json.loads(raw)
                                cx = rect["x"] + 28 + random.uniform(-3, 3)
                                cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                                _step(req_id, f"interactive click #{clicks + 1} at ({cx:.0f},{cy:.0f})")
                                try:
                                    await tab.mouse_move(cx - 50, cy - 20)
                                    await asyncio.sleep(0.06)
                                    await tab.mouse_move(cx, cy)
                                    await asyncio.sleep(0.04)
                                    await tab.mouse_click(cx, cy)
                                except Exception as e:
                                    _step(req_id, f"click error: {e}")
                                last_click = now
                                clicks += 1
                            except Exception:
                                pass
                    await asyncio.sleep(0.3)

                if not cleared:
                    raise TimeoutError(f"challenge did not clear within {timeout}s")

                final_url = await tab.evaluate("location.href")
                title = await tab.evaluate("document.title")
                user_agent = await tab.evaluate("navigator.userAgent")
                html = await tab.get_content()
                target_host = urlparse(final_url or siteurl).hostname or ""
                try:
                    raw_cookies = await pool.browser.cookies.get_all()
                    cookies = [
                        {"name": c.name, "value": c.value, "domain": c.domain,
                         "path": c.path, "expires": c.expires}
                        for c in raw_cookies
                        if _match_host(target_host, c.domain or "")
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
                if tab is not None:
                    try:
                        await tab.close()
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
    xvfb = _start_xvfb_if_needed()
    try:
        t0 = time.time()
        tok = solve(sys.argv[1], sys.argv[2])
        print(f"{tok}\nelapsed: {time.time()-t0:.2f}s")
    finally:
        if xvfb:
            xvfb.terminate()
