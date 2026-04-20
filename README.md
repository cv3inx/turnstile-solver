# Violetics Solver

Local HTTP service that solves Cloudflare Turnstile widgets and Cloudflare
"Just a moment..." JS/interactive challenges using a real Chromium browser
driven by `nodriver`. Replaces paid CAPTCHA services and tools like
FlareSolverr for most use cases.

## Features

- `POST /solve` — solves a Turnstile widget and returns the token.
- `POST /solve-challenge` — clears a Cloudflare JS or interactive challenge
  and returns the final URL, title, cookies (filtered to the target domain),
  user-agent, and full HTML.
- Persistent browser profile — warm Cloudflare cookies make subsequent
  challenges against the same host clear in about one second.
- Async HTTP server (aiohttp) with internal queue. Clients may send
  requests in parallel; solves are executed serially by the browser.
- Structured, single-block log per request with per-step progress output.
- Chromium installed via Patchright so the pinned, tested version is always
  available regardless of the host distribution.

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

The service listens on `http://localhost:9988`. Check it with:

```bash
curl http://localhost:9988/health
```

### Plain Docker

```bash
docker build -t violetics-solver .
docker run -d --name solver --shm-size=1gb \
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
python service.py
```

## Configuration

Environment variables:

| Variable         | Default           | Description                                                     |
|------------------|-------------------|-----------------------------------------------------------------|
| `PORT`           | `9988`            | HTTP port                                                       |
| `MAX_WORKERS`    | `8`               | Max concurrent HTTP requests (solves are serialised internally) |
| `CHROME_PATH`    | auto-detected     | Path to Chromium. Default looks up Patchright's install path.   |
| `TS_PROFILE_DIR` | `/tmp/ts_profile` | Persistent profile directory                                    |
| `DISPLAY`        | `:99`             | Xvfb display (container sets this for you)                      |

Chromium auto-detection searches:
1. `$CHROME_PATH`
2. `~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`
3. System `/usr/bin/google-chrome*` or `/usr/bin/chromium*`

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
  "elapsed": 1.10
}
```

Use the returned `cf_clearance` cookie together with `user_agent` when
proxying the protected API. Both must match — Cloudflare rejects the
cookie if the user-agent differs from the one that earned it.

### `GET /health`

Returns service status counters.

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

All output is written to stdout. Internal library warnings are suppressed.

## Concurrency

The service accepts many HTTP requests in parallel, but Cloudflare escalates
difficulty when multiple tabs on the same profile request a token for the
same sitekey at once. Solves are therefore serialised inside the service.

Typical throughput with a warm profile:

- Turnstile: one token every ~8 seconds
- JS/interactive challenge, warm profile: under 2 seconds per solve
- JS/interactive challenge, cold profile: 8–12 seconds for the first solve

Scaling beyond single-browser throughput requires multiple independent
solver instances, each with its own warm profile and IP.

## File layout

```
solver.py            Core browser automation and solve logic
service.py           aiohttp HTTP wrapper and request logging
requirements.txt     Python dependencies (nodriver, aiohttp)
Dockerfile           Container image (Python + Patchright + Xvfb)
docker-compose.yml   Compose service definition
entrypoint.sh        Container entrypoint (starts Xvfb, then service)
```

## License

MIT
