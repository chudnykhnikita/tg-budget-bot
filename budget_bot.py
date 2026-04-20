import os
from dotenv import load_dotenv
load_dotenv()
import json
import logging
import pytz
from datetime import datetime
from io import BytesIO
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния
ENTERING_AMOUNT, ENTERING_CATEGORY = range(2)
EDIT_CHOOSE_FIELD, EDIT_ENTERING_VALUE = range(2, 4)

DATA_FILE = "budget_data.json"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["➕ Поступление", "➖ Списание"], ["💰 Баланс", "📥 Скачать файл"]],
    resize_keyboard=True,
    is_persistent=True,
)


def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"transactions": []}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_txs(user_id):
    data = load_data()
    return [(i, t) for i, t in enumerate(data["transactions"]) if t["user_id"] == user_id]


# --- Основные хендлеры ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для ведения бюджета.\n\n"
        "Используй кнопки внизу.\n\n"
        "/history — последние 10 операций\n"
        "/edit — редактировать или удалить операцию\n"
        "/export — выгрузить в Excel\n"
        "/clear — очистить все данные",
        reply_markup=MAIN_KEYBOARD,
    )


async def handle_main_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

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
    elif "Скачать файл" in text:
        await export_excel(update, context)
        return ConversationHandler.END

    return ConversationHandler.END


async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", ".")

    if any(k in text for k in ["Поступление", "Списание", "Баланс", "Скачать файл"]):
        return await handle_main_buttons(update, context)

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (например: 1500 или 99.90)")
        return ENTERING_AMOUNT

    context.user_data["amount"] = amount
    context.user_data["date"] = update.message.date.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")

    if context.user_data["type"] == "expense":
        await update.message.reply_text("📝 На что потратил? Напиши категорию или описание:")
        return ENTERING_CATEGORY
    else:
        _save_transaction(update.effective_user.id, amount, "income", None, context.user_data["date"])
        await update.message.reply_text(f"✅ Поступление {amount:.2f} ₽ сохранено!", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END


async def enter_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip()
    amount = context.user_data["amount"]
    _save_transaction(update.effective_user.id, amount, "expense", category, context.user_data["date"])
    await update.message.reply_text(f"✅ Списание {amount:.2f} ₽ ({category}) сохранено!", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


def _save_transaction(user_id, amount, t_type, category, date):
    data = load_data()
    data["transactions"].append({
        "user_id": user_id,
        "type": t_type,
        "amount": amount,
        "category": category,
        "date": date,
    })
    save_data(data)


async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = update.effective_user.id
    txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    total_income = sum(t["amount"] for t in txs if t["type"] == "income")
    total_expense = sum(t["amount"] for t in txs if t["type"] == "expense")
    balance = total_income - total_expense

    categories: dict[str, float] = {}
    for t in txs:
        if t["type"] == "expense" and t["category"]:
            categories[t["category"]] = categories.get(t["category"], 0) + t["amount"]

    cat_lines = ""
    if categories:
        sorted_cats = sorted(categories.items(), key=lambda x: -x[1])
        cat_lines = "\n\n📊 Расходы по категориям:\n" + "\n".join(
            f"  • {cat}: {amt:.2f} ₽" for cat, amt in sorted_cats
        )

    emoji = "✅" if balance >= 0 else "⚠️"
    await update.message.reply_text(
        f"📈 Поступления: {total_income:.2f} ₽\n"
        f"📉 Списания:    {total_expense:.2f} ₽\n"
        f"{emoji} Баланс:    {balance:.2f} ₽{cat_lines}",
        reply_markup=MAIN_KEYBOARD,
    )


# --- Редактирование / удаление ---

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_txs = get_user_txs(user_id)

    if not user_txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    # Показываем последние 20 операций
    recent = user_txs[-20:][::-1]
    buttons = []
    for idx, (global_i, t) in enumerate(recent):
        if t["type"] == "income":
            label = f"➕ {t['amount']:.0f}₽  {t['date']}"
        else:
            cat = f" · {t['category']}" if t["category"] else ""
            label = f"➖ {t['amount']:.0f}₽{cat}  {t['date']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sel:{global_i}")])

    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")])
    await update.message.reply_text(
        "Выбери операцию для редактирования или удаления:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def edit_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Отменено.")
        return

    global_i = int(query.data.split(":")[1])
    data = load_data()
    t = data["transactions"][global_i]
    context.user_data["edit_index"] = global_i

    if t["type"] == "income":
        desc = f"➕ Поступление {t['amount']:.2f} ₽\n📅 {t['date']}"
        buttons = [
            [InlineKeyboardButton("✏️ Изменить сумму", callback_data="edit_field:amount")],
            [InlineKeyboardButton("🗑 Удалить", callback_data="edit_delete")],
            [InlineKeyboardButton("◀️ Назад", callback_data="edit_back")],
        ]
    else:
        cat = t.get("category") or "—"
        desc = f"➖ Списание {t['amount']:.2f} ₽\n📝 {cat}\n📅 {t['date']}"
        buttons = [
            [InlineKeyboardButton("✏️ Изменить сумму", callback_data="edit_field:amount")],
            [InlineKeyboardButton("✏️ Изменить описание", callback_data="edit_field:category")],
            [InlineKeyboardButton("🗑 Удалить", callback_data="edit_delete")],
            [InlineKeyboardButton("◀️ Назад", callback_data="edit_back")],
        ]

    await query.edit_message_text(desc, reply_markup=InlineKeyboardMarkup(buttons))


async def edit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_back":
        # Перерисовываем список
        user_id = update.effective_user.id
        user_txs = get_user_txs(user_id)
        recent = user_txs[-20:][::-1]
        buttons = []
        for global_i, t in recent:
            if t["type"] == "income":
                label = f"➕ {t['amount']:.0f}₽  {t['date']}"
            else:
                cat = f" · {t['category']}" if t["category"] else ""
                label = f"➖ {t['amount']:.0f}₽{cat}  {t['date']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"sel:{global_i}")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")])
        await query.edit_message_text("Выбери операцию:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if query.data == "edit_delete":
        global_i = context.user_data.get("edit_index")
        data = load_data()
        deleted = data["transactions"].pop(global_i)
        save_data(data)
        t_type = "Поступление" if deleted["type"] == "income" else "Списание"
        await query.edit_message_text(f"🗑 {t_type} {deleted['amount']:.2f} ₽ удалено.")
        return

    if query.data.startswith("edit_field:"):
        field = query.data.split(":")[1]
        context.user_data["edit_field"] = field
        if field == "amount":
            await query.edit_message_text("Введи новую сумму:")
        else:
            await query.edit_message_text("Введи новое описание:")
        context.user_data["awaiting_edit"] = True


async def edit_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_edit"):
        return

    field = context.user_data.get("edit_field")
    global_i = context.user_data.get("edit_index")
    text = update.message.text.strip()

    data = load_data()
    t = data["transactions"][global_i]

    if field == "amount":
        try:
            new_val = float(text.replace(",", "."))
            if new_val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Введи корректную сумму:", reply_markup=MAIN_KEYBOARD)
            return
        t["amount"] = new_val
        msg = f"✅ Сумма обновлена: {new_val:.2f} ₽"
    else:
        t["category"] = text
        msg = f"✅ Описание обновлено: {text}"

    save_data(data)
    context.user_data["awaiting_edit"] = False
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)


# --- Экспорт ---

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
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
        ws_income.append([t["date"], t["amount"]])
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
        ws_expense.append([t["date"], t["amount"], t.get("category", "")])
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


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = update.effective_user.id
    txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    lines = []
    for t in txs[-10:][::-1]:
        if t["type"] == "income":
            lines.append(f"➕ +{t['amount']:.2f} ₽  [{t['date']}]")
        else:
            cat = f" ({t['category']})" if t["category"] else ""
            lines.append(f"➖ -{t['amount']:.2f} ₽{cat}  [{t['date']}]")

    await update.message.reply_text("🕓 Последние операции:\n\n" + "\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user_id = update.effective_user.id
    data["transactions"] = [t for t in data["transactions"] if t["user_id"] != user_id]
    save_data(data)
    await update.message.reply_text("🗑 Все твои данные удалены.", reply_markup=MAIN_KEYBOARD)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_edit"] = False
    await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# --- Запуск ---

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Установи переменную окружения TELEGRAM_BOT_TOKEN")

    proxy_url = os.environ.get("PROXY_URL")
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^(➕ Поступление|➖ Списание|💰 Баланс|📥 Скачать файл)$"), handle_main_buttons)
        ],
        states={
            ENTERING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount)],
            ENTERING_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_category)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("export", export_excel))
    app.add_handler(CommandHandler("edit", edit_start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(conv_handler)

    # Inline кнопки для редактирования
    app.add_handler(CallbackQueryHandler(edit_select, pattern="^sel:"))
    app.add_handler(CallbackQueryHandler(edit_action, pattern="^(edit_field:|edit_delete|edit_back|edit_cancel)"))

    # Ввод нового значения при редактировании
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_value))

    print("Бот запущен...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
