# Бюджетный Telegram-бот ИП Чудных

Подотчёт: наличные и карта, отчёт бухгалтеру, Google Sheet как рабочий журнал.

**Как устроен проект:** [`AGENTS.md`](AGENTS.md) — карта файлов, данные, листы, правила. Дальше [`CLAUDE.md`](CLAUDE.md). SSH на прод: [`SERVER_ACCESS.md`](SERVER_ACCESS.md).

Claude / Codex / Kimi / Grok / DeepSeek / GigaCode / Copilot / Cursor читают эти же файлы (короткие указатели в корне и в `.codex/`, `.kimi-code/`, `.dsh/`, `.gigacode/`, `.github/`, `.cursor/`).

## Быстрый старт

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# .env: TELEGRAM_BOT_TOKEN, при Sheets ещё GOOGLE_CREDENTIALS_FILE
python budget_bot.py
```

Тесты: `python3 -m unittest discover -s tests -v`

Прод: `./deploy.sh` после `git push origin main` (Happ включён, см. SERVER_ACCESS.md).
