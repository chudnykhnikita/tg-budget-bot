# Resilient Telegram proxy selection

## Intent

Keep the budget bot polling and responding when one configured proxy becomes unavailable, without losing stored budget data. The change affects network routing only; existing JSON storage and its volume stay untouched.

## Approaches

- **Startup-only selection (current):** checks candidates once, then pins both PTB request clients to the selected proxy. A later outage leaves polling stuck on that proxy.
- **Periodic health checks only:** detects changes but does not move in-flight PTB clients to another proxy, so it does not reliably restore polling.
- **Request-level failover (chosen):** wrap PTB's HTTPX request transport so a network failure retries the request through the next configured candidate. Use this for both normal Bot API calls and `getUpdates`; retain retry behavior when all candidates are unavailable. This directly addresses the observed failure without restarting the bot or touching data.

## Design

Keep `PROXY_URL`, `PROXY_URL_BACKUP`, and direct connection as ordered candidates. Validate candidates at startup, without printing credentials. The request transport tracks the active candidate and, on connection-stage failures, advances through remaining candidates and retries before returning a network error to PTB. Polling can safely retry the same `getUpdates` request after any transport failure. For other Bot API methods, ambiguous read/write failures switch the route for the next request but do not replay the current operation, avoiding duplicate messages or uploads when Telegram may already have processed it. PTB's existing polling retry loop continues when every route fails. A later request can probe candidates again so a recovered higher-priority route can be used. Do not fail over on Bot API responses such as `409 Conflict`, `401`, or other HTTP errors; those are not proxy transport failures.

Use the same failover policy for the regular request client and the dedicated `getUpdates` request client. Redact proxy URLs from all proxy-selection logs. Preserve the existing startup lock, polling registration, and data path.

## Verification

Review the transport lifecycle, retry order, and exception classification against the pinned `python-telegram-bot` 21.6 request interface. Avoid sending test updates to Telegram or modifying the live data file. Automated tests are outside the requested scope unless requested separately.

## Constraints and limits

No client can restore service while every configured proxy and direct route is unavailable or denies access to Telegram. In that case the bot should keep retrying and log a concise, credential-free warning. Existing production JSON and backups are not changed by this work.
