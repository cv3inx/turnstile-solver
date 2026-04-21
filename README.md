# Violetics Solver

Local HTTP service that solves Cloudflare Turnstile widgets and clears
Cloudflare "Just a moment..." JS/interactive challenges. Built on
`nodriver` for the Turnstile widget path and FlareSolverr for the
JS-challenge path, behind a single unified HTTP API.

## Features

- `POST /solve` — solves a Turnstile widget and returns the token.
  Uses `nodriver` with a persistent, warm Chromium profile.
- `POST /solve-challenge` — clears a Cloudflare JS or interactive
  challenge and returns the final URL, title, cookies (filtered to the
  target domain), user-agent, and full HTML. Delegates to a bundled
  FlareSolverr instance by default; falls back to `nodriver` if
  FlareSolverr is unreachable.
- Structured, single-block log per request with per-step progress output.
- Async HTTP server (aiohttp) with internal queue. Clients may send
  requests in parallel; solves are executed serially by the browser.
- `docker compose up -d` brings up both services together — solver and
  FlareSolverr — with health-gated startup.

### Why delegate to FlareSolverr?

On headless Linux hosts (typical VPS / docker-compose targets),
Cloudflare's interactive challenge page now fingerprints every stock
Chromium build tested (Playwright, Google Chrome Stable, Debian
Chromium, Patchright, even Camoufox) and simply never mounts the
Turnstile iframe. With no iframe, there is nothing to click and the
in-process `nodriver` path times out at 60 s.

FlareSolverr ships its own stealth Chromium build that still clears
those pages (typical clearance: 10–20 s, cold profile). The Turnstile
token path (`/solve`) is unaffected — it injects the widget onto a
benign page the solver controls, so fingerprinting of the container
doesn't break iframe mount there.

## Requirements

- Docker and Docker Compose, or
- Python 3.11+ (tested on 3.13) and Xvfb for a host install

No manual Chromium install is needed. Patchright downloads and pins a
compatible Chromium build.

## Quick start

```bash
git clone git@github.com:cv3inx/turnstile-solver.git
cd turnstile-solver
docker compose up -d
```

This starts two containers:

- `violetics-solver` (this service) on `:9988`
- `violetics-flaresolverr` (FlareSolverr backend) on the internal
  compose network only — not exposed on the host.

The solver waits for FlareSolverr to report healthy before starting.

Check it:

```bash
curl http://localhost:9988/health
```

### Disabling the FlareSolverr delegation

Remove the `flaresolverr` service and the `FLARESOLVERR_URL` env from
`docker-compose.yml`, or set `FLARESOLVERR_URL=""`. `/solve-challenge`
will then run the pure-`nodriver` path. Expect timeouts on hosts where
Cloudflare fingerprints the container — see the section above.

### Plain Docker

```bash
docker build -t violetics-solver .
docker run -d --name flaresolverr --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest
docker run -d --name solver --shm-size=1gb \
  --link flaresolverr \
  -e FLARESOLVERR_URL=http://flaresolverr:8191 \
  -p 9988:9988 \
  -v solver-profile:/tmp/ts_profile \
  violetics-solver
```

`--shm-size=1gb` is required — Chromium crashes with the default 64 MB
`/dev/shm`. The volume mount preserves the Cloudflare cookie profile across
container restarts.

### Host install (optional)

```bash
pip install -r requirements.txt patchright
python -m patchright install chromium
# FlareSolverr is optional for a host install; without it /solve-challenge
# runs the nodriver path only.
export FLARESOLVERR_URL=http://localhost:8191   # if running FS locally
python service.py
```

## Configuration

Environment variables:

| Variable            | Default           | Description                                                                 |
|---------------------|-------------------|-----------------------------------------------------------------------------|
| `PORT`              | `9988`            | HTTP port                                                                   |
| `MAX_WORKERS`       | `8`               | Max concurrent HTTP requests (solves are serialised internally)             |
| `FLARESOLVERR_URL`  | _(unset)_         | Base URL of a FlareSolverr instance. When set, `/solve-challenge` delegates to it. The compose file sets this to `http://flaresolverr:8191`. |
| `CHROME_PATH`       | auto-detected     | Path to Chromium. Default looks up Patchright's install path.               |
| `TS_PROFILE_DIR`    | `/tmp/ts_profile` | Persistent profile directory                                                |
| `DISPLAY`           | `:99`             | Xvfb display (container sets this for you)                                  |

Chromium auto-detection searches:
1. `$CHROME_PATH`
2. `~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`
3. System `/usr/bin/google-chrome*` or `/usr/bin/chromium*`

When `FLARESOLVERR_URL` is set, the solver does **not** warm the
in-process browser at startup. `/solve-challenge` goes straight to
FlareSolverr; `/solve` (Turnstile token) still starts the browser on
first use.

## API

All endpoints accept and return JSON.

### `POST /solve`

Solves a Turnstile widget.

Request:

```json
{
  "sitekey": "0x4AAAAAAC3x1HiBz5IFyj7s",
  "siteurl": "https://www.example.com/",
  "timeout": 45
}
```

Response (200):

```json
{
  "token": "1.abc...xyz",
  "elapsed": 8.39
}
```

Response (500):

```json
{
  "error": "turnstile timeout after 45s",
  "elapsed": 45.2
}
```

### `POST /solve-challenge`

Clears a Cloudflare JS or interactive challenge and returns the page state.
When `FLARESOLVERR_URL` is configured, the request is proxied to
FlareSolverr transparently — callers see the same response shape either
way.

Request:

```json
{
  "siteurl": "https://api.example.com/docs",
  "timeout": 45
}
```

Response (200):

```json
{
  "url": "https://api.example.com/docs/",
  "title": "Example API",
  "user_agent": "Mozilla/5.0 ...",
  "cookies": [
    {
      "name": "cf_clearance",
      "value": "...",
      "domain": ".example.com",
      "path": "/",
      "expires": 1811226000
    }
  ],
  "html": "<!doctype html>...",
  "elapsed": 15.52
}
```

Use the returned `cf_clearance` cookie together with `user_agent` when
proxying the protected API. Both must match — Cloudflare rejects the
cookie if the user-agent differs from the one that earned it.

Note: FlareSolverr is sensitive to the exact URL form for some sites
(e.g. `/docs` times out, but `/docs/` clears). The solver automatically
retries extensionless paths with a trailing slash.

### `GET /health`

Returns service status counters.

When in FlareSolverr-delegate mode:

```json
{
  "status": "ok",
  "mode": "flaresolverr",
  "flaresolverr_url": "http://flaresolverr:8191",
  "in_flight": 0,
  "solved": 30,
  "errors": 1,
  "challenges": 11
}
```

When running the pure `nodriver` path:

```json
{
  "status": "ok",
  "max_concurrent": 8,
  "solved_total": 42,
  "in_flight": 0,
  "solved": 30,
  "errors": 1,
  "challenges": 11
}
```

## Log format

Each request produces one block with real-time progress steps in between:

```
「 NEW REQUEST 」
» ID     : 29241879
» FROM   : 172.20.0.1
» POST   : /solve
» URL    : https://www.example.com/
» KEY    : 0x4AAAAAAC3x1H...
  [29241879] opening tab -> https://www.example.com/
  [29241879] waiting for page load...
  [29241879] page loaded (1.2s)
  [29241879] injecting turnstile widget
  [29241879] turnstile ready (1.2s)
  [29241879] click #1 at (46,51)
  [29241879] click #2 at (49,52)
  [29241879] token obtained (9.0s)
» SPEED  : 9.08s
» STATUS : 200 - token 1.1Tqrqdroaa...26cb55 (538 chars)
```

For JS-challenge requests routed through FlareSolverr:

```
「 NEW REQUEST 」
» ID     : cdd14513
» FROM   : 172.20.0.1
» POST   : /solve-challenge
» URL    : https://api.example.com/docs
  [cdd14513] delegating to FlareSolverr -> http://flaresolverr:8191
  [cdd14513] flaresolverr cleared (15.5s, cookies=1)
» SPEED  : 15.52s
» STATUS : 200 - title='Example API' cookies=1 html=74236b
```

All output is written to stdout. Internal library warnings are suppressed.

## Concurrency

The service accepts many HTTP requests in parallel, but Cloudflare escalates
difficulty when multiple tabs on the same profile request a token for the
same sitekey at once. Solves are therefore serialised inside the service.

Typical throughput:

- Turnstile (`/solve`): one token every ~8 seconds
- JS challenge via FlareSolverr: ~15 s per solve (single FS worker)
- JS challenge via in-process `nodriver`, warm profile: under 2 s
- JS challenge via in-process `nodriver`, cold profile: 8–12 s

Scaling beyond single-browser throughput requires multiple independent
solver instances, each with its own warm profile and IP.

## File layout

```
solver.py            Core browser automation + FlareSolverr delegation
service.py           aiohttp HTTP wrapper and request logging
requirements.txt     Python dependencies (nodriver, aiohttp)
Dockerfile           Container image (Python + Patchright + Xvfb)
docker-compose.yml   Compose stack: solver + flaresolverr
entrypoint.sh        Container entrypoint (starts Xvfb, then service)
```

## License

MIT
