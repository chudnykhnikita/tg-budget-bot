import os
import json
import logging
import asyncio
import tempfile
import uuid
import warnings
from datetime import datetime, date
from io import BytesIO
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import NetworkError, TimedOut
from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", category=PTBUserWarning)

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

# ---------------------------------------------------------------------------
# Состояния диалога
# ---------------------------------------------------------------------------
(
    ST_CHOOSE_ACCOUNT,      # выбор счёта: Наличные / Карта
    ST_CHOOSE_DIRECTION,    # выбор: Поступление / Списание
    ST_ENTERING_AMOUNT,     # ввод суммы
    ST_CHOOSE_CATEGORY,     # выбор категории кнопками (или свободный ввод)
    ST_ENTERING_NOTE,       # ввод примечания 2-го уровня
    ST_ENTERING_ZP_DATE,    # ввод даты для ЗП упаковщиков
) = range(6)

EDIT_LIST, EDIT_CHOOSE_FIELD, EDIT_ENTERING_VALUE = range(6, 9)

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
DATA_FILE       = os.environ.get("DATA_FILE", "/data/budget_data.json")
MOSCOW_TZ       = ZoneInfo("Europe/Moscow")
MAX_NOTE_LEN    = 128

# Категории карты — списания
CARD_EXPENSE_CATEGORIES = [
    "Плата банку",
    "ЗП упаковщиков",
    "Дивиденды",
    "Обеды",
    "Офис",
    "Склад",
]
# Подкатегории «Плата банку»
BANK_SUBCATEGORIES = ["Обслуживание счёта", "Комиссия"]
# Подкатегории «Дивиденды»
DIVIDEND_SUBCATEGORIES = ["Андрей", "Алексей", "Никита"]

# Категории наличных — поступления (кнопки + свободный ввод)
CASH_INCOME_CATEGORIES = ["Продажа со склада"]

# Категории наличных — списания (кнопки + свободный ввод)
CASH_EXPENSE_CATEGORIES = ["ЗП упаковщиков"]

BTN_FREE   = "✏️ Свой вариант"
BTN_TODAY  = "📅 Сегодня"
BTN_CANCEL = "❌ Отмена"

# ---------------------------------------------------------------------------
# Главная клавиатура
# ---------------------------------------------------------------------------
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["💵 Наличные", "💳 Карта"],
        ["💰 Баланс",   "🕓 История"],
        ["✏️ Изменить", "📥 Скачать файл"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Работа с данными
# ---------------------------------------------------------------------------

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


async def _save_transaction(user_id: int, amount: Decimal, t_type: str,
                             account: str, category: str | None, note: str | None):
    now = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
    data = await asyncio.to_thread(load_data)
    data["transactions"].append({
        "id":       str(uuid.uuid4()),
        "user_id":  user_id,
        "type":     t_type,        # "income" | "expense"
        "account":  account,       # "cash" | "card"
        "amount":   str(amount),
        "category": category,
        "note":     note,
        "date":     now,
    })
    await save_data(data)


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

# ---------------------------------------------------------------------------
# Вспомогательные функции — inline-клавиатуры
# ---------------------------------------------------------------------------

def _kb(rows: list[list[str]], prefix: str = "") -> InlineKeyboardMarkup:
    """Строит InlineKeyboardMarkup из списка строк."""
    buttons = []
    for row in rows:
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"{prefix}{label}")
            for label in row
        ])
    return InlineKeyboardMarkup(buttons)


def _reply_kb(options: list[str], add_free: bool = False, add_today: bool = False) -> ReplyKeyboardMarkup:
    rows = [[opt] for opt in options]
    if add_today:
        rows.append([BTN_TODAY])
    if add_free:
        rows.append([BTN_FREE])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient network error: %s", err)
        return
    logger.exception("Unhandled error", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("⚠️ Что-то пошло не так, попробуй ещё раз.")

# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я бот для ведения бюджета.\n\n"
        "Выбери счёт кнопками внизу.\n\n"
        "/clear — очистить все данные",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Шаг 1 — выбор счёта (Наличные / Карта)
# ---------------------------------------------------------------------------

async def handle_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if "Баланс" in text:
        await show_summary(update, context)
        return ConversationHandler.END
    if "История" in text:
        await history(update, context)
        return ConversationHandler.END
    if "Изменить" in text:
        return await edit_start(update, context)
    if "Скачать файл" in text:
        await export_excel(update, context)
        return ConversationHandler.END

    if "Наличные" in text:
        context.user_data["account"] = "cash"
    elif "Карта" in text:
        context.user_data["account"] = "card"
    else:
        return ConversationHandler.END

    kb = ReplyKeyboardMarkup(
        [["➕ Поступление", "➖ Списание"], [BTN_CANCEL]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await update.message.reply_text("Поступление или списание?", reply_markup=kb)
    return ST_CHOOSE_DIRECTION

# ---------------------------------------------------------------------------
# Шаг 2 — выбор направления
# ---------------------------------------------------------------------------

async def handle_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if BTN_CANCEL in text:
        return await cancel(update, context)

    if "Поступление" in text:
        context.user_data["direction"] = "income"
    elif "Списание" in text:
        context.user_data["direction"] = "expense"
    else:
        return ST_CHOOSE_DIRECTION

    await update.message.reply_text("Введи сумму:", reply_markup=ReplyKeyboardMarkup(
        [[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True
    ))
    return ST_ENTERING_AMOUNT

# ---------------------------------------------------------------------------
# Шаг 3 — ввод суммы
# ---------------------------------------------------------------------------

async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if BTN_CANCEL in text:
        return await cancel(update, context)

    try:
        amount = parse_amount(text)
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (например: 1500 или 99.90)")
        return ST_ENTERING_AMOUNT

    context.user_data["amount"] = str(amount)
    return await _go_to_category(update, context)

# ---------------------------------------------------------------------------
# Шаг 4 — выбор категории
# ---------------------------------------------------------------------------

async def _go_to_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    account   = context.user_data["account"]
    direction = context.user_data["direction"]

    # Наличные — поступление
    if account == "cash" and direction == "income":
        kb = _reply_kb(CASH_INCOME_CATEGORIES, add_free=True)
        await update.message.reply_text("Выбери категорию:", reply_markup=kb)
        return ST_CHOOSE_CATEGORY

    # Наличные — списание
    if account == "cash" and direction == "expense":
        kb = _reply_kb(CASH_EXPENSE_CATEGORIES, add_free=True)
        await update.message.reply_text("Выбери категорию:", reply_markup=kb)
        return ST_CHOOSE_CATEGORY

    # Карта — поступление (без категории)
    if account == "card" and direction == "income":
        return await _finish(update, context, category=None, note=None)

    # Карта — списание
    if account == "card" and direction == "expense":
        kb = _reply_kb(CARD_EXPENSE_CATEGORIES, add_free=True)
        await update.message.reply_text("Выбери категорию:", reply_markup=kb)
        return ST_CHOOSE_CATEGORY

    return ConversationHandler.END


async def handle_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if BTN_CANCEL in text:
        return await cancel(update, context)

    account   = context.user_data["account"]
    direction = context.user_data["direction"]

    # Свободный ввод
    if text == BTN_FREE:
        await update.message.reply_text(
            "Напиши свою категорию:",
            reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True)
        )
        context.user_data["awaiting_free_category"] = True
        return ST_CHOOSE_CATEGORY

    if context.user_data.pop("awaiting_free_category", False):
        category = text[:MAX_NOTE_LEN]
        context.user_data["category"] = category
        return await _maybe_ask_note(update, context)

    # Карта — списание: подкатегории
    if account == "card" and direction == "expense":
        if text == "Плата банку":
            context.user_data["category"] = text
            kb = _reply_kb(BANK_SUBCATEGORIES)
            await update.message.reply_text("Уточни:", reply_markup=kb)
            context.user_data["awaiting_subcategory"] = True
            return ST_CHOOSE_CATEGORY

        if text == "Дивиденды":
            context.user_data["category"] = text
            kb = _reply_kb(DIVIDEND_SUBCATEGORIES)
            await update.message.reply_text("Кому?", reply_markup=kb)
            context.user_data["awaiting_subcategory"] = True
            return ST_CHOOSE_CATEGORY

        if context.user_data.pop("awaiting_subcategory", False):
            context.user_data["note"] = text[:MAX_NOTE_LEN]
            return await _finish(update, context,
                                  category=context.user_data["category"],
                                  note=context.user_data["note"])

        if text == "ЗП упаковщиков":
            context.user_data["category"] = text
            kb = _reply_kb([], add_today=True)
            await update.message.reply_text("Укажи дату выплаты (или нажми «Сегодня»):", reply_markup=kb)
            context.user_data["awaiting_zp_date"] = True
            return ST_ENTERING_ZP_DATE

        if text in ("Офис", "Склад"):
            context.user_data["category"] = text
            await update.message.reply_text(
                "Добавь пояснение (или /skip чтобы пропустить):",
                reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True)
            )
            return ST_ENTERING_NOTE

        if text == "Обеды":
            return await _finish(update, context, category=text, note=None)

        if text in CARD_EXPENSE_CATEGORIES:
            context.user_data["category"] = text
            return await _finish(update, context, category=text, note=None)

    # Наличные — ЗП упаковщиков
    if text == "ЗП упаковщиков":
        context.user_data["category"] = text
        kb = _reply_kb([], add_today=True)
        await update.message.reply_text("Укажи дату выплаты (или нажми «Сегодня»):", reply_markup=kb)
        context.user_data["awaiting_zp_date"] = True
        return ST_ENTERING_ZP_DATE

    # Все остальные кнопки — сохраняем как категорию без доп. ввода
    context.user_data["category"] = text[:MAX_NOTE_LEN]
    return await _finish(update, context, category=context.user_data["category"], note=None)


async def handle_zp_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if BTN_CANCEL in text:
        return await cancel(update, context)

    if text == BTN_TODAY:
        note = date.today().strftime("%d.%m.%Y")
    else:
        note = text[:MAX_NOTE_LEN]

    return await _finish(update, context,
                          category=context.user_data["category"],
                          note=note)


async def handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if BTN_CANCEL in text:
        return await cancel(update, context)
    note = None if text == "/skip" else text[:MAX_NOTE_LEN]
    return await _finish(update, context,
                          category=context.user_data.get("category"),
                          note=note)

# ---------------------------------------------------------------------------
# Финальное сохранение
# ---------------------------------------------------------------------------

async def _finish(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   category: str | None, note: str | None):
    amount    = Decimal(context.user_data["amount"])
    account   = context.user_data["account"]
    direction = context.user_data["direction"]

    try:
        await _save_transaction(
            user_id=update.effective_user.id,
            amount=amount,
            t_type=direction,
            account=account,
            category=category,
            note=note,
        )
    except OSError:
        logger.exception("Failed to save transaction")
        await update.message.reply_text(
            "⚠️ Не удалось сохранить операцию, данные НЕ записаны.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    acc_label  = "💵 Наличные" if account == "cash" else "💳 Карта"
    dir_label  = "Поступление" if direction == "income" else "Списание"
    cat_label  = f" · {category}" if category else ""
    note_label = f" · {note}" if note else ""

    await update.message.reply_text(
        f"✅ {acc_label} | {dir_label} {fmt(amount)} ₽{cat_label}{note_label} сохранено!",
        reply_markup=MAIN_KEYBOARD,
    )
    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------

async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data    = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs     = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    def totals(account):
        inc = sum(Decimal(t["amount"]) for t in txs if t["account"] == account and t["type"] == "income")
        exp = sum(Decimal(t["amount"]) for t in txs if t["account"] == account and t["type"] == "expense")
        return inc, exp

    cash_inc, cash_exp = totals("cash")
    card_inc, card_exp = totals("card")
    cash_bal = cash_inc - cash_exp
    card_bal = card_inc - card_exp
    total    = cash_bal + card_bal

    # Расходы по категориям (карта)
    categories: dict[str, Decimal] = {}
    for t in txs:
        if t["type"] == "expense" and t.get("category"):
            key = t["category"]
            if t.get("note"):
                key += f" · {t['note']}"
            categories[key] = categories.get(key, Decimal("0")) + Decimal(t["amount"])

    cat_lines = ""
    if categories:
        sorted_cats = sorted(categories.items(), key=lambda x: -x[1])
        cat_lines = "\n\n📊 Расходы по категориям:\n" + "\n".join(
            f"  • {k}: {fmt(v)} ₽" for k, v in sorted_cats
        )

    text = (
        f"💵 Наличные\n"
        f"  Поступления: {fmt(cash_inc)} ₽\n"
        f"  Списания:    {fmt(cash_exp)} ₽\n"
        f"  Баланс:      {fmt(cash_bal)} ₽\n\n"
        f"💳 Карта\n"
        f"  Поступления: {fmt(card_inc)} ₽\n"
        f"  Списания:    {fmt(card_exp)} ₽\n"
        f"  Баланс:      {fmt(card_bal)} ₽\n\n"
        f"{'✅' if total >= 0 else '⚠️'} Общий баланс: {fmt(total)} ₽"
        f"{cat_lines}"
    )

    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)

    # Сразу отправляем Excel
    await export_excel(update, context)

# ---------------------------------------------------------------------------
# История
# ---------------------------------------------------------------------------

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data    = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs     = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    cash_lines, card_lines = [], []
    for t in txs[-20:][::-1]:
        acc  = t.get("account", "card")
        cat  = f" ({t['category']})" if t.get("category") else ""
        note = f" · {t['note']}" if t.get("note") else ""
        if t["type"] == "income":
            line = f"➕ +{fmt(t['amount'])} ₽{cat}{note}  [{t['date']}]"
        else:
            line = f"➖ -{fmt(t['amount'])} ₽{cat}{note}  [{t['date']}]"
        if acc == "cash":
            cash_lines.append(line)
        else:
            card_lines.append(line)

    parts = []
    if cash_lines:
        parts.append("💵 Наличные:\n" + "\n".join(cash_lines))
    if card_lines:
        parts.append("💳 Карта:\n" + "\n".join(card_lines))

    await update.message.reply_text(
        "🕓 Последние операции:\n\n" + "\n\n".join(parts),
        reply_markup=MAIN_KEYBOARD,
    )

# ---------------------------------------------------------------------------
# Экспорт Excel (4 листа: Наличные поступления, Наличные списания, Карта поступления, Карта списания)
# ---------------------------------------------------------------------------

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data    = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs     = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    wb = Workbook()
    header_font    = Font(name="Arial", bold=True, color="FFFFFF")
    income_fill    = PatternFill("solid", start_color="1E7E34")
    expense_fill   = PatternFill("solid", start_color="C0392B")

    def style_header(cell, fill):
        cell.font      = header_font
        cell.fill      = fill
        cell.alignment = Alignment(horizontal="center")

    def build_sheet(ws, rows, cols, fill, has_note=False):
        ws.append(cols)
        for i, col in enumerate(cols, 1):
            style_header(ws.cell(1, i), fill)
        for row in rows:
            ws.append(row)
        if rows:
            r = len(rows) + 2
            ws[f"A{r}"] = "Итого"
            ws[f"A{r}"].font = Font(name="Arial", bold=True)
            amount_col = "B"
            ws[f"{amount_col}{r}"] = f"=SUM({amount_col}2:{amount_col}{r-1})"
            ws[f"{amount_col}{r}"].font = Font(name="Arial", bold=True)
        ws.column_dimensions["A"].width = 18
        ws.column_dimensions["B"].width = 15
        ws.column_dimensions["C"].width = 25
        if has_note:
            ws.column_dimensions["D"].width = 25

    # Лист 1: Наличные — Поступления
    ws1 = wb.active
    ws1.title = "Наличные Поступления"
    rows = [[t["date"], float(t["amount"]), t.get("category") or ""]
            for t in txs if t.get("account") == "cash" and t["type"] == "income"]
    build_sheet(ws1, rows, ["Дата", "Сумма (₽)", "Категория"], income_fill)

    # Лист 2: Наличные — Списания
    ws2 = wb.create_sheet("Наличные Списания")
    rows = [[t["date"], float(t["amount"]), t.get("category") or "", t.get("note") or ""]
            for t in txs if t.get("account") == "cash" and t["type"] == "expense"]
    build_sheet(ws2, rows, ["Дата", "Сумма (₽)", "Категория", "Примечание"], expense_fill, has_note=True)

    # Лист 3: Карта — Поступления
    ws3 = wb.create_sheet("Карта Поступления")
    rows = [[t["date"], float(t["amount"])]
            for t in txs if t.get("account") == "card" and t["type"] == "income"]
    build_sheet(ws3, rows, ["Дата", "Сумма (₽)"], income_fill)

    # Лист 4: Карта — Списания
    ws4 = wb.create_sheet("Карта Списания")
    rows = [[t["date"], float(t["amount"]), t.get("category") or "", t.get("note") or ""]
            for t in txs if t.get("account") == "card" and t["type"] == "expense"]
    build_sheet(ws4, rows, ["Дата", "Сумма (₽)", "Категория", "Примечание"], expense_fill, has_note=True)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    now = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")
    await update.message.reply_document(
        document=buf,
        filename=f"budget_{now}.xlsx",
        caption="📊 Готово!",
    )

# ---------------------------------------------------------------------------
# Редактирование / удаление
# ---------------------------------------------------------------------------

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    data     = await asyncio.to_thread(load_data)
    user_txs = [t for t in data["transactions"] if t["user_id"] == user_id]

    if not user_txs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    recent  = user_txs[-20:][::-1]
    buttons = []
    for t in recent:
        acc  = "💵" if t.get("account") == "cash" else "💳"
        cat  = f" · {t['category']}" if t.get("category") else ""
        note = f" · {t['note']}" if t.get("note") else ""
        sign = "➕" if t["type"] == "income" else "➖"
        label = f"{acc}{sign} {fmt(t['amount'])}₽{cat}{note}  {t['date']}"
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
    data  = await asyncio.to_thread(load_data)
    t     = next((x for x in data["transactions"] if x["id"] == tx_id), None)

    if t is None or t["user_id"] != update.effective_user.id:
        await query.edit_message_text("⛔ Операция недоступна.")
        return ConversationHandler.END

    context.user_data["edit_tx_id"] = tx_id
    acc   = "💵 Наличные" if t.get("account") == "cash" else "💳 Карта"
    cat   = t.get("category") or "—"
    note  = t.get("note") or "—"
    d_type = "Поступление" if t["type"] == "income" else "Списание"
    desc  = f"{acc} | {d_type}\nСумма: {fmt(t['amount'])} ₽\nКатегория: {cat}\nПримечание: {note}\nДата: {t['date']}"

    buttons = [
        [InlineKeyboardButton("✏️ Сумму",       callback_data="edit_field:amount")],
        [InlineKeyboardButton("✏️ Категорию",   callback_data="edit_field:category")],
        [InlineKeyboardButton("✏️ Примечание",  callback_data="edit_field:note")],
        [InlineKeyboardButton("🗑 Удалить",      callback_data="edit_delete")],
        [InlineKeyboardButton("◀️ Назад",        callback_data="edit_back")],
    ]
    await query.edit_message_text(desc, reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_CHOOSE_FIELD


async def edit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_back":
        user_id  = update.effective_user.id
        data     = await asyncio.to_thread(load_data)
        user_txs = [t for t in data["transactions"] if t["user_id"] == user_id]
        recent   = user_txs[-20:][::-1]
        buttons  = []
        for t in recent:
            acc  = "💵" if t.get("account") == "cash" else "💳"
            cat  = f" · {t['category']}" if t.get("category") else ""
            note = f" · {t['note']}" if t.get("note") else ""
            sign = "➕" if t["type"] == "income" else "➖"
            label = f"{acc}{sign} {fmt(t['amount'])}₽{cat}{note}  {t['date']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"sel:{t['id']}")])
        buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel")])
        await query.edit_message_text("Выбери операцию:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_LIST

    if query.data == "edit_delete":
        tx_id = context.user_data.get("edit_tx_id")
        data  = await asyncio.to_thread(load_data)
        t     = next((x for x in data["transactions"] if x["id"] == tx_id), None)
        if t is None or t["user_id"] != update.effective_user.id:
            await query.edit_message_text("⛔ Операция недоступна.")
            return ConversationHandler.END
        data["transactions"] = [x for x in data["transactions"] if x["id"] != tx_id]
        await save_data(data)
        await query.edit_message_text(f"🗑 Операция {fmt(t['amount'])} ₽ удалена.")
        return ConversationHandler.END

    if query.data.startswith("edit_field:"):
        field = query.data.split(":")[1]
        context.user_data["edit_field"] = field
        prompts = {"amount": "Введи новую сумму:", "category": "Введи новую категорию:", "note": "Введи новое примечание:"}
        await query.edit_message_text(prompts.get(field, "Введи значение:"))
        return EDIT_ENTERING_VALUE

    return EDIT_CHOOSE_FIELD


async def edit_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    tx_id = context.user_data.get("edit_tx_id")
    text  = (update.message.text or "").strip()

    data = await asyncio.to_thread(load_data)
    t    = next((x for x in data["transactions"] if x["id"] == tx_id), None)
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
    elif field == "category":
        t["category"] = text[:MAX_NOTE_LEN]
        msg = f"✅ Категория обновлена: {t['category']}"
    else:
        t["note"] = text[:MAX_NOTE_LEN]
        msg = f"✅ Примечание обновлено: {t['note']}"

    await save_data(data)
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, удалить всё", callback_data="clear_yes"),
        InlineKeyboardButton("Отмена",             callback_data="clear_no"),
    ]])
    await update.message.reply_text("Точно удалить ВСЕ свои операции? Это необратимо.", reply_markup=kb)


async def clear_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "clear_yes":
        data    = await asyncio.to_thread(load_data)
        user_id = update.effective_user.id
        data["transactions"] = [t for t in data["transactions"] if t["user_id"] != user_id]
        await save_data(data)
        await query.edit_message_text("🗑 Все твои данные удалены.")
    else:
        await query.edit_message_text("Отменено.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

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

    main_filter = filters.Regex(
        "^(💵 Наличные|💳 Карта|💰 Баланс|🕓 История|✏️ Изменить|📥 Скачать файл)$"
    )

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(main_filter, handle_account)],
        states={
            ST_CHOOSE_DIRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_direction)],
            ST_ENTERING_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
            ST_CHOOSE_CATEGORY:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_category)],
            ST_ENTERING_ZP_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_zp_date)],
            ST_ENTERING_NOTE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_start)],
        states={
            EDIT_LIST:          [CallbackQueryHandler(edit_select, pattern="^(sel:|edit_cancel)")],
            EDIT_CHOOSE_FIELD:  [CallbackQueryHandler(edit_action, pattern="^(edit_field:|edit_delete|edit_back|edit_cancel)")],
            EDIT_ENTERING_VALUE:[MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_value)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export_excel))
    app.add_handler(CommandHandler("clear",  clear))
    app.add_handler(CallbackQueryHandler(clear_confirm, pattern="^clear_"))
    app.add_handler(add_conv)
    app.add_handler(edit_conv)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
