# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Local dev (requires .env with TELEGRAM_BOT_TOKEN)
python budget_bot.py

# Docker (production)
docker build -t budget-bot .
docker run -d --env-file .env -v budget_data:/data budget-bot
```

`.env` variables:
- `TELEGRAM_BOT_TOKEN` — required
- `DATA_FILE` — path to JSON storage, defaults to `/data/budget_data.json`
- `PROXY_URL` — optional HTTP proxy for the Telegram client

There are no tests or a linter configured.

## Architecture

The entire bot lives in a single file: `budget_bot.py`. It is a `python-telegram-bot` v21 async bot with two `ConversationHandler` state machines registered on the `Application`:

- **`edit_conv`** (registered first) — inline-keyboard flow for browsing, editing, and deleting existing records. States `EDIT_CHOOSE_TYPE` → `EDIT_CHOOSE_DIRECTION` → `EDIT_LIST` → `EDIT_CHOOSE_FIELD` → `EDIT_ENTERING_VALUE` / `EDIT_CONFIRM_DELETE`.
- **`add_conv`** (registered second) — reply-keyboard flow for recording new transactions. States `ST_CHOOSE_ACCOUNT` → `ST_CHOOSE_DIRECTION` → `ST_ENTERING_AMOUNT` → `ST_CHOOSE_CATEGORY` → optional sub-states (`ST_ENTERING_ZP_DATE`, `ST_ENTERING_NOTE`) → save.

`edit_conv` must remain first in `app.add_handler(...)` order so the "✏️ Изменить" button isn't swallowed by `add_conv`'s `main_filter` regex.

## Data layer

All persistence is a single JSON file with three top-level arrays: `transactions`, `requests`, `transfers`. Every record carries a UUID `id`, `user_id`, ISO-formatted `date` (Moscow time), and `amount` stored as a decimal string `"X.XX"`.

Key rules:
- All writes go through `_atomic_modify(mutator)`, which holds a global `asyncio.Lock` and writes via `tempfile + os.replace` to prevent corruption.
- `load_data()` runs `_migrate()` on every read — idempotently backfills `id`, `account`, `note`, and normalises amounts for records written before the current schema.
- Amounts everywhere are `Decimal`; the `fmt()` helper rounds to whole roubles for display (not for storage or Excel).
- `_strip_emoji()` is applied only when writing to Excel — emoji stay in JSON and in bot messages.

## Categories and subcategory flow

Categories are module-level string constants (with emoji prefixes). Certain card-expense categories trigger a subcategory prompt before `_finish()`:
- `CAT_BANK` → `BANK_SUBCATEGORIES` (stored as `note`)
- `CAT_DIV` → `DIVIDEND_SUBCATEGORIES` (stored as `note`)
- `CAT_ZP_WORKERS` → `WORKERS_SUBCATEGORIES` (stored as `note`)
- `CAT_ZP` → date picker (`ST_ENTERING_ZP_DATE`), date stored as `note`
- `CAT_OFC` / `CAT_WH` → free-text note prompt (`ST_ENTERING_NOTE`)

`handle_category` uses `context.user_data` flags (`awaiting_free_category`, `awaiting_subcategory`, `awaiting_zp_date`) to route the *next* message in the same state.

## Excel export

`export_excel()` builds a 6-sheet workbook (openpyxl): cash income, cash expenses, card income, card expenses, requests, transfers. It is triggered automatically whenever the user opens the balance view (`show_summary`), and also available via `/export`.
