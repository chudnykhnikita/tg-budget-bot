# Resilient Proxy Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Telegram polling and Bot API requests working when one configured proxy fails, by failing over to the next route and returning to a recovered preferred route.

**Architecture:** Add a PTB `BaseRequest` adapter that owns one `HTTPXRequest` per configured proxy and one direct route. It retries only transport exceptions, keeps a working route active, periodically probes the preferred route, and emits route labels without credentials. Wire separate instances into the normal and `getUpdates` request slots.

**Tech Stack:** Python 3.12, `python-telegram-bot==21.6`, `httpx[socks]`.

**Spec:** `docs/superpowers/specs/2026-11-10-resilient-proxy-selection-design.md`

## Global Constraints

- Preserve the existing `TELEGRAM_BOT_TOKEN`, `PROXY_URL`, and `PROXY_URL_BACKUP` environment configuration.
- Treat direct connectivity as the last fallback after configured proxies.
- Do not fail over on Bot API HTTP errors such as `409 Conflict` or `401`; fail over on `NetworkError` and transport timeouts.
- Keep normal Bot API requests and `getUpdates` isolated in separate PTB request instances.
- Do not change the JSON data path, data contents, VPS environment, or running production container.
- Never write proxy URLs or credentials into logs.

## Review Focus

- A connection-stage failure retries through the remaining routes while preserving the original request arguments; ambiguous read/write failures move the active route without replaying a potentially completed action.
- An HTTP response from Telegram is returned normally and does not trigger proxy rotation.
- The preferred route is retried periodically after failover, so recovery does not require restarting the process.
- If every route is down, PTB receives a generic network failure and continues its existing polling retry loop.
- A malformed proxy URL is skipped so another configured route or direct connectivity can still start the bot.
- Both regular API calls and `getUpdates` use the failover adapter, without sharing connection pools.

### Task 1: Add request-level proxy failover and wire it into the bot

**Files:**
- Create: `proxy_failover.py`
- Modify: `budget_bot.py` (imports, startup candidate ordering, application builder)
- Modify: `Dockerfile` (include the new request adapter in the image)
- Modify: `CLAUDE.md` (proxy behavior documentation)

**Interfaces:**
- `ProxyFailoverRequest(BaseRequest)` accepts ordered `(label, proxy_url)` routes plus PTB HTTP timeout/pool settings.
- It implements `initialize`, `shutdown`, `read_timeout`, and `do_request` by delegating to per-route `HTTPXRequest` clients.
- `budget_bot.py` supplies configured proxies followed by `("direct", None)` and wires independent instances with `.request(...)` and `.get_updates_request(...)`.

- [x] Add a route selector that validates configured proxies at startup, logs only route labels, and keeps all candidates for later retries.
- [x] Implement per-request failover for safe connection-stage errors and polling transport errors, with active-route retention and a time-limited preferred-route probe; avoid replaying ambiguous Bot API actions.
- [x] Return HTTP status/content unchanged; after every route fails, raise a generic `NetworkError` without proxy data.
- [x] Replace static builder proxy configuration with separately constructed failover request instances for Bot API and polling.
- [x] Update the Docker image and `CLAUDE.md` to include and describe runtime failover and direct fallback.
- [x] Review exception boundaries, lifecycle, ordering, recovery probing, and secret-free logging against the spec and pinned PTB 21.6 interface.

**Completion:** The running bot can fail over within the same polling/request operation, later retry the preferred route, and keep PTB's retry behavior when all routes are unavailable; JSON storage and deployment state remain untouched.
