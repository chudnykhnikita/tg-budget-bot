import os
import json
import logging
import asyncio
import tempfile
import uuid
import warnings
from datetime import datetime
from io import BytesIO
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

warnings.filterwarnings("ignore", category=PTBUserWarning)

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import NetworkError, TimedOut
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# --- Состояния ---
ENTERING_AMOUNT, ENTERING_CATEGORY = range(2)
EDIT_LIST, EDIT_CHOOSE_FIELD, EDIT_ENTERING_VALUE = range(2, 5)

# --- Константы ---
DATA_FILE = os.environ.get("DATA_FILE", "/data/budget_data.json")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MAX_CATEGORY_LEN = 64

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Поступление", "➖ Списание"],
        ["💰 Баланс", "🕓 История"],
        ["✏️ Изменить", "📥 Скачать файл"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# --- Блокировка для безопасной записи ---
_lock = asyncio.Lock()


# --- Работа с данными ---

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"transactions": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logger.exception("Corrupted %s, backing up", DATA_FILE)
        os.rename(DATA_FILE, DATA_FILE + ".corrupt")
        return {"transactions": []}


async def save_data(data: dict):
    async with _lock:
        dir_ = os.path.dirname(DATA_FILE) or "."
        os.makedirs(dir_, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".budget_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def parse_amount(s: str) -> Decimal:
    try:
        v = Decimal(s.strip().replace(",", ".")).quantize(Decimal("0.01"), ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError
    if v <= 0:
        raise ValueError
    return v


def fmt(amount) -> str:
    return f"{Decimal(str(amount)):.2f}"


# --- Error handler ---

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient network error: %s", err)
        return
    logger.exception("Unhandled error", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Что-то пошло не так, попробуй ещё раз.")


# --- Основные хендлеры ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для ведения бюджета.\n\n"
        "Используй кнопки внизу.\n\n"
        "/export — выгрузить в Excel\n"
        "/clear — очистить все данные",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def handle_main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if "Поступление" in text:
        context.user_data["type"] = "income"
        await update.message.reply_text("💰 Введи сумму поступления:")
        return ENTERING_AMOUNT
    elif "Списание" in text:
        context.user_data["type"] = "expense"
        await update.message.reply_text("💸 Введи сумму списания:")
        return ENTERING_AMOUNT
    elif "Баланс" in text:
        await show_summary(update, context)
        return ConversationHandler.END
    elif "История" in text:
        await history(update, context)
        return ConversationHandler.END
    elif "Изменить" in text:
        return await edit_start(update, context)
    elif "Скачать файл" in text:
        await export_excel(update, context)
        return ConversationHandler.END

    return ConversationHandler.END


async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if any(k in text for k in ["Поступление", "Списание", "Баланс", "История", "Изменить", "Скачать файл"]):
        return await handle_main_buttons(update, context)

    t_type = context.user_data.get("type")
    if t_type not in ("income", "expense"):
        await update.message.reply_text("Начни заново с кнопок.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    try:
        amount = parse_amount(text)
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (например: 1500 или 99.90)")
        return ENTERING_AMOUNT

    context.user_data["amount"] = str(amount)

    if t_type == "expense":
        await update.message.reply_text("📝 На что потратил? Напиши категорию или описание:")
        return ENTERING_CATEGORY
    else:
        try:
            await _save_transaction(update.effective_user.id, amount, "income", None)
        except OSError:
            logger.exception("Failed to save transaction")
            await update.message.reply_text("⚠️ Не удалось сохранить операцию, данные НЕ записаны.", reply_markup=MAIN_KEYBOARD)
            return ConversationHandler.END
        await update.message.reply_text(f"✅ Поступление {fmt(amount)} ₽ сохранено!", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def enter_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = (update.message.text or "").strip()[:MAX_CATEGORY_LEN]
    amount = Decimal(context.user_data["amount"])
    try:
        await _save_transaction(update.effective_user.id, amount, "expense", category)
    except OSError:
        logger.exception("Failed to save transaction")
        await update.message.reply_text("⚠️ Не удалось сохранить операцию, данные НЕ записаны.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Списание {fmt(amount)} ₽ ({category}) сохранено!", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


async def _save_transaction(user_id: int, amount: Decimal, t_type: str, category):
    now = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
    data = await asyncio.to_thread(load_data)
    data["transactions"].append({
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": t_type,
        "amount": str(amount),
        "category": category,
        "date": now,
    })
    await save_data(data)


async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    total_income = sum(Decimal(t["amount"]) for t in txs if t["type"] == "income")
    total_expense = sum(Decimal(t["amount"]) for t in txs if t["type"] == "expense")
    balance = total_income - total_expense

    categories: dict[str, Decimal] = {}
    for t in txs:
        if t["type"] == "expense" and t["category"]:
            categories[t["category"]] = categories.get(t["category"], Decimal("0")) + Decimal(t["amount"])

    cat_lines = ""
    if categories:
        sorted_cats = sorted(categories.items(), key=lambda x: -x[1])
        cat_lines = "\n\n📊 Расходы по категориям:\n" + "\n".join(
            f"  • {cat}: {fmt(amt)} ₽" for cat, amt in sorted_cats
        )

    emoji = "✅" if balance >= 0 else "⚠️"
    await update.message.reply_text(
        f"📈 Поступления: {fmt(total_income)} ₽\n"
        f"📉 Списания:    {fmt(total_expense)} ₽\n"
        f"{emoji} Баланс:    {fmt(balance)} ₽{cat_lines}",
        reply_markup=MAIN_KEYBOARD,
    )


# --- Редактирование / удаление ---

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = await asyncio.to_thread(load_data)
    user_txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not user_txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    recent = user_txs[-20:][::-1]
    buttons = []
    for t in recent:
        if t["type"] == "income":
            label = f"➕ {fmt(t['amount'])}₽  {t['date']}"
        else:
            cat = f" · {t['category']}" if t["category"] else ""
            label = f"➖ {fmt(t['amount'])}₽{cat}  {t['date']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sel:{t['id']}")])

    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")])
    await update.message.reply_text(
        "Выбери операцию:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_LIST


async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    tx_id = query.data.split(":", 1)[1]
    data = await asyncio.to_thread(load_data)
    t = next((x for x in data["transactions"] if x["id"] == tx_id), None)

    if t is None or t["user_id"] != update.effective_user.id:
        await query.edit_message_text("⛔ Операция недоступна.")
        return ConversationHandler.END

    context.user_data["edit_tx_id"] = tx_id

    if t["type"] == "income":
        desc = f"➕ Поступление {fmt(t['amount'])} ₽\n📅 {t['date']}"
        buttons = [
            [InlineKeyboardButton("✏️ Изменить сумму", callback_data="edit_field:amount")],
            [InlineKeyboardButton("🗑 Удалить", callback_data="edit_delete")],
            [InlineKeyboardButton("◀️ Назад", callback_data="edit_back")],
        ]
    else:
        cat = t.get("category") or "—"
        desc = f"➖ Списание {fmt(t['amount'])} ₽\n📝 {cat}\n📅 {t['date']}"
        buttons = [
            [InlineKeyboardButton("✏️ Изменить сумму", callback_data="edit_field:amount")],
            [InlineKeyboardButton("✏️ Изменить описание", callback_data="edit_field:category")],
            [InlineKeyboardButton("🗑 Удалить", callback_data="edit_delete")],
            [InlineKeyboardButton("◀️ Назад", callback_data="edit_back")],
        ]

    await query.edit_message_text(desc, reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_CHOOSE_FIELD


async def edit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_back":
        user_id = update.effective_user.id
        data = await asyncio.to_thread(load_data)
        user_txs = [t for t in data["transactions"] if t["user_id"] == user_id]
        recent = user_txs[-20:][::-1]
        buttons = []
        for t in recent:
            if t["type"] == "income":
                label = f"➕ {fmt(t['amount'])}₽  {t['date']}"
            else:
                cat = f" · {t['category']}" if t["category"] else ""
                label = f"➖ {fmt(t['amount'])}₽{cat}  {t['date']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"sel:{t['id']}")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")])
        await query.edit_message_text("Выбери операцию:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_LIST

    if query.data == "edit_delete":
        tx_id = context.user_data.get("edit_tx_id")
        data = await asyncio.to_thread(load_data)
        t = next((x for x in data["transactions"] if x["id"] == tx_id), None)
        if t is None or t["user_id"] != update.effective_user.id:
            await query.edit_message_text("⛔ Операция недоступна.")
            return ConversationHandler.END
        data["transactions"] = [x for x in data["transactions"] if x["id"] != tx_id]
        await save_data(data)
        t_type = "Поступление" if t["type"] == "income" else "Списание"
        await query.edit_message_text(f"🗑 {t_type} {fmt(t['amount'])} ₽ удалено.")
        return ConversationHandler.END

    if query.data.startswith("edit_field:"):
        field = query.data.split(":")[1]
        context.user_data["edit_field"] = field
        if field == "amount":
            await query.edit_message_text("Введи новую сумму:")
        else:
            await query.edit_message_text("Введи новое описание:")
        return EDIT_ENTERING_VALUE

    return EDIT_CHOOSE_FIELD


async def edit_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    tx_id = context.user_data.get("edit_tx_id")
    text = (update.message.text or "").strip()

    data = await asyncio.to_thread(load_data)
    t = next((x for x in data["transactions"] if x["id"] == tx_id), None)
    if t is None or t["user_id"] != update.effective_user.id:
        await update.message.reply_text("⛔ Операция недоступна.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if field == "amount":
        try:
            new_val = parse_amount(text)
        except ValueError:
            await update.message.reply_text("❌ Введи корректную сумму:")
            return EDIT_ENTERING_VALUE
        t["amount"] = str(new_val)
        msg = f"✅ Сумма обновлена: {fmt(new_val)} ₽"
    else:
        t["category"] = text[:MAX_CATEGORY_LEN]
        msg = f"✅ Описание обновлено: {t['category']}"

    await save_data(data)
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# --- /clear с подтверждением ---

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, удалить всё", callback_data="clear_yes"),
        InlineKeyboardButton("Отмена", callback_data="clear_no"),
    ]])
    await update.message.reply_text("Точно удалить ВСЕ свои операции? Это действие необратимо.", reply_markup=kb)


async def clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "clear_yes":
        data = await asyncio.to_thread(load_data)
        user_id = update.effective_user.id
        data["transactions"] = [t for t in data["transactions"] if t["user_id"] != user_id]
        await save_data(data)
        await query.edit_message_text("🗑 Все твои данные удалены.")
    else:
        await query.edit_message_text("Отменено.")


# --- Экспорт ---

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    wb = Workbook()
    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    income_fill = PatternFill("solid", start_color="1E7E34")
    expense_fill = PatternFill("solid", start_color="C0392B")

    def style_header(cell, fill):
        cell.font = header_font
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")

    ws_income = wb.active
    ws_income.title = "Поступления"
    ws_income.append(["Дата", "Сумма (₽)"])
    style_header(ws_income["A1"], income_fill)
    style_header(ws_income["B1"], income_fill)
    income_txs = [t for t in txs if t["type"] == "income"]
    for t in income_txs:
        ws_income.append([t["date"], float(t["amount"])])
    if income_txs:
        r = len(income_txs) + 2
        ws_income[f"A{r}"] = "Итого"
        ws_income[f"A{r}"].font = Font(name="Arial", bold=True)
        ws_income[f"B{r}"] = f"=SUM(B2:B{r-1})"
        ws_income[f"B{r}"].font = Font(name="Arial", bold=True)
    ws_income.column_dimensions["A"].width = 18
    ws_income.column_dimensions["B"].width = 15

    ws_expense = wb.create_sheet("Списания")
    ws_expense.append(["Дата", "Сумма (₽)", "Описание"])
    style_header(ws_expense["A1"], expense_fill)
    style_header(ws_expense["B1"], expense_fill)
    style_header(ws_expense["C1"], expense_fill)
    expense_txs = [t for t in txs if t["type"] == "expense"]
    for t in expense_txs:
        ws_expense.append([t["date"], float(t["amount"]), t.get("category", "")])
    if expense_txs:
        r = len(expense_txs) + 2
        ws_expense[f"A{r}"] = "Итого"
        ws_expense[f"A{r}"].font = Font(name="Arial", bold=True)
        ws_expense[f"B{r}"] = f"=SUM(B2:B{r-1})"
        ws_expense[f"B{r}"].font = Font(name="Arial", bold=True)
    ws_expense.column_dimensions["A"].width = 18
    ws_expense.column_dimensions["B"].width = 15
    ws_expense.column_dimensions["C"].width = 30

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    now = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
    await update.message.reply_document(document=buf, filename=f"budget_{now}.xlsx", caption="📊 Готово!")


# --- История ---

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    lines = []
    for t in txs[-10:][::-1]:
        if t["type"] == "income":
            lines.append(f"➕ +{fmt(t['amount'])} ₽  [{t['date']}]")
        else:
            cat = f" ({t['category']})" if t["category"] else ""
            lines.append(f"➖ -{fmt(t['amount'])} ₽{cat}  [{t['date']}]")

    await update.message.reply_text("🕓 Последние операции:\n\n" + "\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# --- Запуск ---

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Установи переменную окружения TELEGRAM_BOT_TOKEN")

    proxy_url = os.environ.get("PROXY_URL")
    builder = (
        Application.builder()
        .token(token)
        .get_updates_read_timeout(25)
        .get_updates_write_timeout(25)
        .get_updates_connect_timeout(10)
        .get_updates_pool_timeout(25)
    )
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()

    app.add_error_handler(on_error)

    # Conversation: добавление операций
    add_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.Regex("^(➕ Поступление|➖ Списание|💰 Баланс|🕓 История|✏️ Изменить|📥 Скачать файл)$"),
                handle_main_buttons,
            )
        ],
        states={
            ENTERING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
            ENTERING_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_category)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    # Conversation: редактирование
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_start)],
        states={
            EDIT_LIST: [CallbackQueryHandler(edit_select, pattern="^(sel:|edit_cancel)")],
            EDIT_CHOOSE_FIELD: [CallbackQueryHandler(edit_action, pattern="^(edit_field:|edit_delete|edit_back|edit_cancel)")],
            EDIT_ENTERING_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_value)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("export", export_excel))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CallbackQueryHandler(clear_confirm, pattern="^clear_"))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
