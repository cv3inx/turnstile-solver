"""
Cloudflare Turnstile + JS-Challenge ("Just a moment...") solver.

Design notes:
  - Backend: Camoufox (stealth Firefox build with realistic OS
    fingerprints) running headed under a virtual X server (Xvfb).
    Real Cloudflare deployments fingerprint --headless=new builds
    (HeadlessChrome UA + missing GPU/audio signals) and refuse to
    mount the Turnstile iframe; running headed under Xvfb keeps the
    UA clean and lets the widget render.
  - HTML template + click strategy borrowed from Boterdrop-Solver:
    minimal host page that loads the CF api.js, plus a CSS shrink
    hack on the .cf-turnstile div so click coordinates land on the
    invisible-mode hit area predictably. Persistent click loop until
    cf-turnstile-response is filled.
  - Single warm browser, persistent profile. Solves are serialised
    per-sitekey (CF escalates difficulty when the same sitekey is hit by
    concurrent tabs of one profile); different keys run in parallel up to
    MAX_WORKERS via a semaphore.
  - solve_challenge_async optionally delegates to a Byparr /
    FlareSolverr proxy via CHALLENGE_PROXY_URL; the in-process browser
    is the fallback.
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
from camoufox import DefaultAddons
from camoufox.async_api import AsyncCamoufox


log = logging.getLogger("solver")


def _step(req_id: str, msg: str):
    """Per-request progress trace. Only emitted at DEBUG so INFO stays to one
    line per request even under heavy concurrency."""
    if log.isEnabledFor(logging.DEBUG):
        log.debug("[%s] %s", req_id, msg)


# ---------- Profile + headless mode ----------

def _get_profile_dir() -> str:
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    return "/tmp/ts_profile"


def _solver_proxy() -> Optional[str]:
    """Outbound proxy for the in-process browser (e.g. WARP HTTP proxy).
    Empty -> direct connection."""
    return (os.environ.get("SOLVER_PROXY") or "").strip() or None


def _headless_mode():
    """Default to headed under the entrypoint-managed Xvfb. Headless
    Camoufox/Firefox can still be requested via HEADLESS=true but real
    CF widgets refuse to mount on it."""
    mode = os.environ.get("HEADLESS", "false").lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("virtual",):
        return "virtual"
    return False


# ---------- Singleton browser ----------

class BrowserSingleton:
    def __init__(self, max_concurrent: int):
        self._camoufox: Optional[AsyncCamoufox] = None
        self.browser = None  # playwright BrowserContext when launched with user_data_dir
        self.sem = asyncio.Semaphore(max_concurrent)
        # Per-key locks: CF escalates difficulty when the SAME sitekey is hit
        # by concurrent tabs of one profile, so we serialise per key — but let
        # different keys/sites run in parallel up to the semaphore. A single
        # global lock here would serialise everything and make sem pointless.
        self._key_locks: dict[str, asyncio.Lock] = {}
        self.max_concurrent = max_concurrent
        self._start_lock = asyncio.Lock()
        self.solve_count = 0
        self.stopped = False

    def key_lock(self, key: str) -> asyncio.Lock:
        """Lock scoped to a sitekey/URL so same-key solves stay serial while
        different keys run concurrently."""
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    def _is_alive(self) -> bool:
        if self.browser is None or self.stopped:
            return False
        try:
            b = getattr(self.browser, "browser", None)
            if b is not None and hasattr(b, "is_connected"):
                return bool(b.is_connected())
            return True
        except Exception:
            return False

    async def ensure(self):
        async with self._start_lock:
            if self._is_alive():
                return
            if self.browser is not None or self._camoufox is not None:
                log.warning("browser handle stale, relaunching camoufox")
                try:
                    if self._camoufox is not None:
                        await self._camoufox.__aexit__(None, None, None)
                except Exception:
                    pass
                self._camoufox = None
                self.browser = None
            profile = _get_profile_dir()
            os.makedirs(profile, exist_ok=True)
            proxy = _solver_proxy()
            log.info("launching camoufox profile=%s headless=%s proxy=%s",
                     profile, _headless_mode(), proxy or "-")
            # exclude_addons=[DefaultAddons.UBO] — uBlock Origin sometimes
            # blocks the CF api.js script, which kills widget mount.
            kwargs = dict(
                headless=_headless_mode(),
                humanize=True,
                persistent_context=True,
                user_data_dir=profile,
                os=["windows", "macos", "linux"],
                locale="en-US",
                exclude_addons=[DefaultAddons.UBO],
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            if proxy:
                # geoip=True aligns the spoofed timezone/locale with the
                # proxy's exit IP, so WARP's geo doesn't contradict the UA.
                kwargs["proxy"] = {"server": proxy}
                kwargs["geoip"] = True
            self._camoufox = AsyncCamoufox(**kwargs)
            self.browser = await self._camoufox.__aenter__()
            self.stopped = False
            log.info("camoufox ready")

    async def new_page(self, url: str = ""):
        await self.ensure()
        page = await self.browser.new_page()
        if url:
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
# Minimal host page modelled after Boterdrop. The api.js loads with an
# explicit onload callback and an <p> element keeps the body non-empty
# so layout settles before the widget renders.
_HOST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>.</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback" async defer></script>
</head>
<body>
__WIDGET__
<p id="ip-display"></p>
</body>
</html>"""


_IS_CHALLENGE_JS = """
(() => {
    if (document.title.toLowerCase().includes('just a moment')) return true;
    if (document.querySelector('div.challenge-form, #challenge-form, .ray-id')) return true;
    if (document.querySelector('iframe[src*="challenges.cloudflare.com/cdn-cgi"]')) return true;
    return false;
})()
"""


async def _turnstile_on_page(page, sitekey: str, siteurl: str, req_id: str,
                              timeout: int, action: Optional[str] = None,
                              cdata: Optional[str] = None) -> str:
    """Solve Turnstile via route-intercepted host HTML (Boterdrop pattern)."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()

    target = siteurl if siteurl.endswith("/") else siteurl + "/"

    widget_div = (
        f'<div class="cf-turnstile" style="background:white;" data-sitekey="{sitekey}"'
        + (f' data-action="{action}"' if action else "")
        + (f' data-cdata="{cdata}"' if cdata else "")
        + "></div>"
    )
    body = _HOST_HTML.replace("__WIDGET__", widget_div)

    try:
        await page.unroute_all()
    except Exception:
        pass
    await page.route(target, lambda route: route.fulfill(body=body, status=200))
    await page.goto(target)
    _step(req_id, f"route intercepted {target}")

    # CSS hack from Boterdrop — narrowing the cf-turnstile div makes
    # the click coords land on the checkbox hit area in invisible mode.
    try:
        await page.eval_on_selector(
            "//div[@class='cf-turnstile']", "el => el.style.width = '70px'"
        )
    except Exception:
        pass

    # 80 x 0.3s = 24s default; we follow the caller's timeout instead.
    deadline = t0 + timeout
    poll = 0.3
    while loop.time() < deadline:
        try:
            value = await page.input_value("[name=cf-turnstile-response]", timeout=400)
        except Exception:
            value = None
        if value:
            _step(req_id, f"token obtained ({loop.time() - t0:.1f}s)")
            return value
        # Click the parent div to coax invisible widgets into firing.
        try:
            await page.locator("//div[@class='cf-turnstile']").click(timeout=400)
        except Exception:
            pass
        await asyncio.sleep(poll)

    raise TimeoutError(f"turnstile timeout after {timeout}s")


_DEAD_BROWSER_HINTS = (
    "connection closed",
    "browser has been closed",
    "target page, context or browser has been closed",
    "browser context has been closed",
)


def _is_dead_browser_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in _DEAD_BROWSER_HINTS)


async def solve_async(sitekey: str, siteurl: str, req_id: str = "-",
                      timeout: int = 45, action: Optional[str] = None,
                      cdata: Optional[str] = None) -> str:
    pool = await get_pool()
    async with pool.sem:
        async with pool.key_lock(sitekey):
            _step(req_id, f"opening tab for {siteurl}")
            for attempt in (1, 2):
                page = None
                try:
                    await pool.ensure()
                    page = await pool.browser.new_page()
                    return await _turnstile_on_page(
                        page, sitekey, siteurl, req_id, timeout, action, cdata
                    )
                except Exception as exc:
                    if attempt == 1 and _is_dead_browser_error(exc):
                        _step(req_id, f"browser dead, relaunching: {exc}")
                        await pool.shutdown()
                        continue
                    raise
                finally:
                    pool.solve_count += 1
                    if page is not None:
                        try:
                            await page.unroute_all()
                        except Exception:
                            pass
                        try:
                            await page.close()
                        except Exception:
                            pass
            raise RuntimeError("solve_async: unreachable")


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

    # Route the challenge fetch through the same egress proxy as the browser.
    proxy = _solver_proxy()
    if proxy:
        payload_base["proxy"] = {"url": proxy}

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
        async with pool.key_lock(siteurl):
            _step(req_id, f"opening tab -> {siteurl}")
            for attempt in (1, 2):
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
                except Exception as exc:
                    if attempt == 1 and _is_dead_browser_error(exc):
                        _step(req_id, f"browser dead, relaunching: {exc}")
                        await pool.shutdown()
                        continue
                    raise
                finally:
                    pool.solve_count += 1
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
            raise RuntimeError("solve_challenge_async: unreachable")


# ---------- reCAPTCHA v3 (Boterdrop pattern) ----------

_RECAPTCHA_READY_JS = """
() => typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'
"""

_RECAPTCHA_INJECT_JS = """
(key) => new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/api.js?render=' + key;
    s.onload = () => {
        const i = setInterval(() => {
            if (typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function') {
                clearInterval(i); res(true);
            }
        }, 100);
        setTimeout(() => { clearInterval(i); rej(new Error('recaptcha inject timeout')); }, 10000);
    };
    s.onerror = () => rej(new Error('recaptcha script load failed'));
    document.head.appendChild(s);
})
"""

_RECAPTCHA_EXEC_JS = """
([key, act]) => new Promise((res, rej) => {
    grecaptcha.ready(() => {
        grecaptcha.execute(key, { action: act }).then(res)
            .catch(e => rej(new Error(e.message || 'recaptcha execute failed')));
    });
})
"""


async def solve_recaptcha_v3_async(sitekey: str, siteurl: str, req_id: str = "-",
                                    timeout: int = 45, action: str = "verify") -> str:
    """Load the target page, ensure grecaptcha is present (inject api.js if
    not), then run grecaptcha.execute to mint a v3 token. Score-based, so a
    clean Camoufox fingerprint + WARP egress is what makes it pass."""
    pool = await get_pool()
    async with pool.sem:
        async with pool.key_lock(sitekey):
            for attempt in (1, 2):
                page = None
                try:
                    _step(req_id, f"recaptcha v3 -> {siteurl} (action={action})")
                    page = await pool.new_page(siteurl)
                    if not await page.evaluate(_RECAPTCHA_READY_JS):
                        _step(req_id, "grecaptcha absent, injecting api.js")
                        await page.evaluate(_RECAPTCHA_INJECT_JS, sitekey)
                    token = await page.evaluate(_RECAPTCHA_EXEC_JS, [sitekey, action])
                    if not token:
                        raise RuntimeError("recaptcha returned empty token")
                    _step(req_id, "recaptcha token obtained")
                    return token
                except Exception as exc:
                    if attempt == 1 and _is_dead_browser_error(exc):
                        _step(req_id, f"browser dead, relaunching: {exc}")
                        await pool.shutdown()
                        continue
                    raise
                finally:
                    pool.solve_count += 1
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
            raise RuntimeError("solve_recaptcha_v3_async: unreachable")


# ---------- AWS WAF token (Boterdrop pattern) ----------

async def solve_aws_token_async(siteurl: str, req_id: str = "-",
                                 timeout: int = 45) -> dict:
    """Navigate to the target and poll for the `aws-waf-token` cookie that
    the AWS WAF challenge JS sets once it clears. Returns the cookie + UA."""
    pool = await get_pool()
    async with pool.sem:
        async with pool.key_lock(siteurl):
            for attempt in (1, 2):
                page = None
                try:
                    _step(req_id, f"aws-waf -> {siteurl}")
                    page = await pool.new_page(siteurl)
                    loop = asyncio.get_event_loop()
                    deadline = loop.time() + timeout
                    token = None
                    while loop.time() < deadline:
                        cookies = await pool.browser.cookies()
                        token = next((c for c in cookies
                                      if c.get("name") == "aws-waf-token"), None)
                        if token:
                            break
                        await asyncio.sleep(1)
                    if not token:
                        raise TimeoutError(f"aws-waf-token not set within {timeout}s")
                    user_agent = await page.evaluate("navigator.userAgent")
                    _step(req_id, "aws-waf-token obtained")
                    return {
                        "token": token.get("value"),
                        "cookie": f"aws-waf-token={token.get('value')}",
                        "user_agent": user_agent,
                        "url": page.url,
                    }
                except Exception as exc:
                    if attempt == 1 and _is_dead_browser_error(exc):
                        _step(req_id, f"browser dead, relaunching: {exc}")
                        await pool.shutdown()
                        continue
                    raise
                finally:
                    pool.solve_count += 1
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
            raise RuntimeError("solve_aws_token_async: unreachable")


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
