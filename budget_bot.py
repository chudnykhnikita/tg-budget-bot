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
    ST_CHOOSE_ACCOUNT,          # выбор счёта: Наличные / Карта
    ST_CHOOSE_DIRECTION,        # выбор: Поступление / Списание
    ST_ENTERING_AMOUNT,         # ввод суммы
    ST_CHOOSE_CATEGORY,         # выбор категории кнопками (или свободный ввод)
    ST_ENTERING_NOTE,           # ввод примечания 2-го уровня
    ST_ENTERING_ZP_DATE,        # ввод даты для ЗП упаковщиков
    ST_ENTERING_REQUEST_AMOUNT, # ввод суммы запроса
) = range(7)

(
    EDIT_CHOOSE_TYPE,       # выбор: Наличные / Карта / Запросы
    EDIT_CHOOSE_DIRECTION,  # выбор: Поступления / Списания
    EDIT_LIST,              # пагинированный список операций или запросов
    EDIT_CHOOSE_FIELD,      # выбор поля для редактирования или удаления
    EDIT_ENTERING_VALUE,    # ввод нового значения
    EDIT_CONFIRM_DELETE,    # подтверждение удаления
) = range(7, 13)

EDIT_PAGE_SIZE = 5

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
DATA_FILE       = os.environ.get("DATA_FILE", "/data/budget_data.json")
MOSCOW_TZ       = ZoneInfo("Europe/Moscow")
MAX_NOTE_LEN    = 128

# Категории (константы — используются и как лейблы кнопок, и как ключи).
CAT_BANK  = "🏦 Плата банку"
CAT_ZP    = "👷 ЗП упаковщиков"
CAT_DIV   = "💎 Дивиденды"
CAT_MEAL  = "🍽 Обеды"
CAT_OFC   = "🏢 Офис"
CAT_WH    = "📦 Склад"

CAT_SALE  = "🛒 Продажа со склада"

SUB_BANK_SERVICE = "🧾 Обслуживание счёта"
SUB_BANK_FEE     = "💸 Комиссия"

# Категории карты — списания
CARD_EXPENSE_CATEGORIES = [CAT_BANK, CAT_ZP, CAT_DIV, CAT_MEAL, CAT_OFC, CAT_WH]
# Подкатегории «Плата банку»
BANK_SUBCATEGORIES = [SUB_BANK_SERVICE, SUB_BANK_FEE]
# Подкатегории «Дивиденды» (имена без эмодзи)
DIVIDEND_SUBCATEGORIES = ["Андрей", "Алексей", "Никита"]

# Категории наличных — поступления (кнопки + свободный ввод)
CASH_INCOME_CATEGORIES = [CAT_SALE]

# Категории наличных — списания (кнопки + свободный ввод)
CASH_EXPENSE_CATEGORIES = [CAT_ZP]

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
        ["✏️ Изменить", "📨 Запросил"],
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
        return {"transactions": [], "requests": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        logger.exception("Corrupted %s, backing up", DATA_FILE)
        os.rename(DATA_FILE, DATA_FILE + ".corrupt")
        return {"transactions": [], "requests": []}
    data.setdefault("transactions", [])
    data.setdefault("requests", [])
    return data


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


async def _save_request(user_id: int, amount: Decimal):
    now = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
    data = await asyncio.to_thread(load_data)
    data["requests"].append({
        "id":      str(uuid.uuid4()),
        "user_id": user_id,
        "amount":  str(amount),
        "date":    now,
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
    """Форматирует сумму для отображения в боте: целое число с пробельными
    разделителями тысяч. Копейки округляются (ROUND_HALF_UP).
    Пример: Decimal('10000.00') → '10 000'.
    Для Excel НЕ используется — там значения идут как float напрямую."""
    d = Decimal(str(amount)).quantize(Decimal("1"), ROUND_HALF_UP)
    return f"{d:,}".replace(",", " ")  # неразрывный пробел — чтобы число не разрывалось переносом

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


def _reply_kb(options: list[str], add_free: bool = False, add_today: bool = False,
              cols: int = 2) -> ReplyKeyboardMarkup:
    # Раскладываем варианты сеткой по `cols` колонок (по умолчанию 2),
    # как основная клавиатура.
    rows = [options[i:i + cols] for i in range(0, len(options), cols)]
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
    if "Запросил" in text:
        await update.message.reply_text(
            "Введи запрошенную сумму:",
            reply_markup=ReplyKeyboardMarkup(
                [[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True
            ),
        )
        return ST_ENTERING_REQUEST_AMOUNT

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
        if text == CAT_BANK:
            context.user_data["category"] = text
            kb = _reply_kb(BANK_SUBCATEGORIES)
            await update.message.reply_text("Уточни:", reply_markup=kb)
            context.user_data["awaiting_subcategory"] = True
            return ST_CHOOSE_CATEGORY

        if text == CAT_DIV:
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

        if text == CAT_ZP:
            context.user_data["category"] = text
            kb = _reply_kb([], add_today=True)
            await update.message.reply_text("Укажи дату выплаты (или нажми «Сегодня»):", reply_markup=kb)
            context.user_data["awaiting_zp_date"] = True
            return ST_ENTERING_ZP_DATE

        if text in (CAT_OFC, CAT_WH):
            context.user_data["category"] = text
            await update.message.reply_text(
                "Добавь пояснение (или /skip чтобы пропустить):",
                reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True, one_time_keyboard=True)
            )
            return ST_ENTERING_NOTE

        if text == CAT_MEAL:
            return await _finish(update, context, category=text, note=None)

        if text in CARD_EXPENSE_CATEGORIES:
            context.user_data["category"] = text
            return await _finish(update, context, category=text, note=None)

    # Наличные — ЗП упаковщиков
    if text == CAT_ZP:
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
# Запрос денег («Запросил»)
# ---------------------------------------------------------------------------

async def handle_request_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if BTN_CANCEL in text:
        return await cancel(update, context)

    try:
        amount = parse_amount(text)
    except ValueError:
        await update.message.reply_text("❌ Введи корректную сумму (например: 100000 или 99.90)")
        return ST_ENTERING_REQUEST_AMOUNT

    user_id = update.effective_user.id
    try:
        await _save_request(user_id, amount)
    except OSError:
        logger.exception("Failed to save request")
        await update.message.reply_text(
            "⚠️ Не удалось сохранить запрос, попробуй ещё раз.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    # Показываем сводку: сколько всего запрошено, сколько уже пришло, сколько осталось
    data    = await asyncio.to_thread(load_data)
    txs     = [t for t in data["transactions"] if t["user_id"] == user_id]
    reqs    = [r for r in data["requests"]     if r["user_id"] == user_id]
    total_req     = sum(Decimal(r["amount"]) for r in reqs)
    total_card_in = sum(
        Decimal(t["amount"]) for t in txs
        if t.get("account") == "card" and t["type"] == "income"
    )
    remaining = total_req - total_card_in

    await update.message.reply_text(
        f"✅ Запрошено {fmt(amount)} ₽\n\n"
        f"📨 Всего запрошено:  {fmt(total_req)} ₽\n"
        f"💳 Получено по карте: {fmt(total_card_in)} ₽\n"
        f"⏳ Осталось получить: {fmt(remaining)} ₽",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------

async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data    = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs     = [t for t in data["transactions"] if t["user_id"] == user_id]
    reqs    = [r for r in data["requests"]     if r["user_id"] == user_id]

    if not txs and not reqs:
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

    # Запросы: сколько запрошено и сколько ещё не пришло (минус поступления на карту)
    total_req = sum(Decimal(r["amount"]) for r in reqs)
    remaining = total_req - card_inc
    req_lines = ""
    if reqs:
        req_lines = (
            "\n\n📨 Запросы\n"
            f"  Запрошено:        {fmt(total_req)} ₽\n"
            f"  Получено (карта): {fmt(card_inc)} ₽\n"
            f"  Осталось:         {fmt(remaining)} ₽"
        )

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
        f"{req_lines}"
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
# Экспорт Excel (5 листов: Наличные поступления/списания, Карта поступления/списания, Запросы)
# ---------------------------------------------------------------------------

async def export_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data    = await asyncio.to_thread(load_data)
    user_id = update.effective_user.id
    txs     = [t for t in data["transactions"] if t["user_id"] == user_id]
    reqs    = [r for r in data["requests"]     if r["user_id"] == user_id]

    if not txs and not reqs:
        await update.message.reply_text("📭 Операций пока нет.", reply_markup=MAIN_KEYBOARD)
        return

    wb = Workbook()
    header_font    = Font(name="Arial", bold=True, color="FFFFFF")
    income_fill    = PatternFill("solid", start_color="1E7E34")
    expense_fill   = PatternFill("solid", start_color="C0392B")
    request_fill   = PatternFill("solid", start_color="2980B9")

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

    # Лист 5: Запросы (запрошено, получено по карте, осталось получить)
    ws5 = wb.create_sheet("Запросы")
    req_rows = [[r["date"], float(r["amount"])] for r in reqs]
    build_sheet(ws5, req_rows, ["Дата", "Запрошено (₽)"], request_fill)

    total_card_in = sum(
        Decimal(t["amount"]) for t in txs
        if t.get("account") == "card" and t["type"] == "income"
    )
    if req_rows:
        # Строка "Итого" уже добавлена build_sheet'ом на len(req_rows)+2
        total_r = len(req_rows) + 2
        rec_r   = total_r + 2
        rem_r   = total_r + 3
        ws5[f"A{rec_r}"] = "Получено по карте"
        ws5[f"A{rec_r}"].font = Font(name="Arial", bold=True)
        ws5[f"B{rec_r}"] = float(total_card_in)
        ws5[f"B{rec_r}"].font = Font(name="Arial", bold=True)
        ws5[f"A{rem_r}"] = "Осталось получить"
        ws5[f"A{rem_r}"].font = Font(name="Arial", bold=True)
        ws5[f"B{rem_r}"] = f"=B{total_r}-B{rec_r}"
        ws5[f"B{rem_r}"].font = Font(name="Arial", bold=True)
        ws5.column_dimensions["A"].width = 22

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

# --- Вспомогательные функции для отрисовки экранов редактирования ---------

def _edit_item_label(item: dict, is_request: bool) -> str:
    if is_request:
        return f"📨 {fmt(item['amount'])} ₽   {item['date']}"
    sign = "➕" if item["type"] == "income" else "➖"
    cat  = f" · {item['category']}" if item.get("category") else ""
    note = f" · {item['note']}"     if item.get("note")     else ""
    return f"{sign} {fmt(item['amount'])} ₽{cat}{note}   {item['date']}"


def _edit_get_filtered(data: dict, user_id: int, flt: dict) -> list[dict]:
    if flt["type"] == "requests":
        items = [r for r in data["requests"] if r["user_id"] == user_id]
    else:
        account   = flt["type"]      # "cash" | "card"
        direction = flt["direction"] # "income" | "expense"
        items = [
            t for t in data["transactions"]
            if t["user_id"] == user_id
            and t.get("account") == account
            and t["type"]       == direction
        ]
    # Свежие сверху
    return items[::-1]


async def _edit_render_type_picker(target):
    """target — это update.message (на старте) или callback_query (после кликов)."""
    buttons = [
        [InlineKeyboardButton("💵 Наличные", callback_data="edit_type:cash")],
        [InlineKeyboardButton("💳 Карта",    callback_data="edit_type:card")],
        [InlineKeyboardButton("📨 Запросы",  callback_data="edit_type:requests")],
        [InlineKeyboardButton("❌ Отмена",   callback_data="edit_cancel")],
    ]
    text = "Что редактируем?"
    kb   = InlineKeyboardMarkup(buttons)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=kb)
    else:
        await target.reply_text(text, reply_markup=kb)


async def _edit_render_direction_picker(query, account: str):
    acc_label = "💵 Наличные" if account == "cash" else "💳 Карта"
    buttons = [
        [InlineKeyboardButton("➕ Поступления", callback_data="edit_dir:income")],
        [InlineKeyboardButton("➖ Списания",    callback_data="edit_dir:expense")],
        [
            InlineKeyboardButton("◀️ Назад",  callback_data="edit_back_type"),
            InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
        ],
    ]
    await query.edit_message_text(
        f"{acc_label}\nПоступления или списания?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _edit_render_list(query, context, user_id: int):
    flt  = context.user_data["edit_filter"]
    data = await asyncio.to_thread(load_data)
    items = _edit_get_filtered(data, user_id, flt)
    is_request = flt["type"] == "requests"

    # Заголовок
    if is_request:
        title = "📨 Запросы"
    else:
        acc  = "💵 Наличные" if flt["type"] == "cash" else "💳 Карта"
        dirn = "Поступления" if flt["direction"] == "income" else "Списания"
        title = f"{acc} → {dirn}"

    if not items:
        buttons = [[
            InlineKeyboardButton(
                "◀️ Назад",
                callback_data="edit_back_type" if is_request else "edit_back_dir",
            ),
            InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
        ]]
        await query.edit_message_text(
            f"{title}\n\nПока нет операций.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    total_pages = max(1, (len(items) + EDIT_PAGE_SIZE - 1) // EDIT_PAGE_SIZE)
    page = max(0, min(flt.get("page", 0), total_pages - 1))
    flt["page"] = page

    start = page * EDIT_PAGE_SIZE
    page_items = items[start:start + EDIT_PAGE_SIZE]

    buttons = []
    for it in page_items:
        cb = f"edit_sel:{it['id']}"
        buttons.append([InlineKeyboardButton(_edit_item_label(it, is_request), callback_data=cb)])

    # Навигация по страницам
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"edit_page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"стр. {page + 1}/{total_pages}", callback_data="edit_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"edit_page:{page + 1}"))
    if len(nav) > 1:  # не показываем единственную плашку без стрелок
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(
            "◀️ Назад",
            callback_data="edit_back_type" if is_request else "edit_back_dir",
        ),
        InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
    ])

    await query.edit_message_text(
        f"{title}\nВыбери операцию:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _edit_render_item(query, context, item: dict, is_request: bool):
    if is_request:
        desc = (
            f"📨 Запрос\n"
            f"Сумма: {fmt(item['amount'])} ₽\n"
            f"Дата: {item['date']}"
        )
        buttons = [
            [InlineKeyboardButton("✏️ Сумму",  callback_data="edit_field:amount")],
            [InlineKeyboardButton("🗑 Удалить", callback_data="edit_delete")],
            [
                InlineKeyboardButton("◀️ Назад",  callback_data="edit_back_list"),
                InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
            ],
        ]
    else:
        acc    = "💵 Наличные" if item.get("account") == "cash" else "💳 Карта"
        d_type = "Поступление" if item["type"] == "income" else "Списание"
        cat    = item.get("category") or "—"
        note   = item.get("note") or "—"
        desc = (
            f"{acc} | {d_type}\n"
            f"Сумма: {fmt(item['amount'])} ₽\n"
            f"Категория: {cat}\n"
            f"Примечание: {note}\n"
            f"Дата: {item['date']}"
        )
        buttons = [
            [InlineKeyboardButton("✏️ Сумму",      callback_data="edit_field:amount")],
            [InlineKeyboardButton("✏️ Категорию",  callback_data="edit_field:category")],
            [InlineKeyboardButton("✏️ Примечание", callback_data="edit_field:note")],
            [InlineKeyboardButton("🗑 Удалить",    callback_data="edit_delete")],
            [
                InlineKeyboardButton("◀️ Назад",  callback_data="edit_back_list"),
                InlineKeyboardButton("❌ Отмена", callback_data="edit_cancel"),
            ],
        ]
    await query.edit_message_text(desc, reply_markup=InlineKeyboardMarkup(buttons))


async def _edit_render_delete_confirm(query, item: dict, is_request: bool):
    if is_request:
        text = f"Удалить запрос {fmt(item['amount'])} ₽ от {item['date']}?"
    else:
        sign = "+" if item["type"] == "income" else "−"
        cat  = f" · {item['category']}" if item.get("category") else ""
        text = f"Удалить операцию {sign}{fmt(item['amount'])} ₽{cat} от {item['date']}?"
    buttons = [[
        InlineKeyboardButton("🗑 Да, удалить", callback_data="edit_confirm_yes"),
        InlineKeyboardButton("↩️ Нет",         callback_data="edit_confirm_no"),
    ]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


# --- Обработчики ----------------------------------------------------------

async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа: команда /edit или кнопка «✏️ Изменить»."""
    context.user_data.pop("edit_filter", None)
    context.user_data.pop("edit_tx_id", None)
    context.user_data.pop("edit_is_request", None)
    context.user_data.pop("edit_field", None)
    await _edit_render_type_picker(update.message)
    return EDIT_CHOOSE_TYPE


async def edit_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if not query.data.startswith("edit_type:"):
        return EDIT_CHOOSE_TYPE

    typ = query.data.split(":", 1)[1]   # "cash" | "card" | "requests"
    context.user_data["edit_filter"] = {"type": typ, "page": 0}

    if typ == "requests":
        await _edit_render_list(query, context, update.effective_user.id)
        return EDIT_LIST

    await _edit_render_direction_picker(query, typ)
    return EDIT_CHOOSE_DIRECTION


async def edit_pick_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if query.data == "edit_back_type":
        await _edit_render_type_picker(query)
        return EDIT_CHOOSE_TYPE

    if not query.data.startswith("edit_dir:"):
        return EDIT_CHOOSE_DIRECTION

    direction = query.data.split(":", 1)[1]
    context.user_data["edit_filter"]["direction"] = direction
    context.user_data["edit_filter"]["page"] = 0

    await _edit_render_list(query, context, update.effective_user.id)
    return EDIT_LIST


async def edit_list_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if query.data == "edit_noop":
        return EDIT_LIST

    if query.data == "edit_back_type":
        await _edit_render_type_picker(query)
        return EDIT_CHOOSE_TYPE

    if query.data == "edit_back_dir":
        flt = context.user_data["edit_filter"]
        await _edit_render_direction_picker(query, flt["type"])
        return EDIT_CHOOSE_DIRECTION

    if query.data.startswith("edit_page:"):
        page = int(query.data.split(":", 1)[1])
        context.user_data["edit_filter"]["page"] = page
        await _edit_render_list(query, context, update.effective_user.id)
        return EDIT_LIST

    if query.data.startswith("edit_sel:"):
        item_id = query.data.split(":", 1)[1]
        flt     = context.user_data["edit_filter"]
        is_req  = flt["type"] == "requests"
        data    = await asyncio.to_thread(load_data)
        coll    = data["requests"] if is_req else data["transactions"]
        item    = next((x for x in coll if x["id"] == item_id), None)
        if item is None or item["user_id"] != update.effective_user.id:
            await query.edit_message_text("⛔ Операция недоступна.")
            return ConversationHandler.END
        context.user_data["edit_tx_id"]      = item_id
        context.user_data["edit_is_request"] = is_req
        await _edit_render_item(query, context, item, is_req)
        return EDIT_CHOOSE_FIELD

    return EDIT_LIST


async def edit_field_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if query.data == "edit_back_list":
        await _edit_render_list(query, context, update.effective_user.id)
        return EDIT_LIST

    if query.data == "edit_delete":
        tx_id  = context.user_data.get("edit_tx_id")
        is_req = context.user_data.get("edit_is_request", False)
        data   = await asyncio.to_thread(load_data)
        coll   = data["requests"] if is_req else data["transactions"]
        item   = next((x for x in coll if x["id"] == tx_id), None)
        if item is None or item["user_id"] != update.effective_user.id:
            await query.edit_message_text("⛔ Операция недоступна.")
            return ConversationHandler.END
        await _edit_render_delete_confirm(query, item, is_req)
        return EDIT_CONFIRM_DELETE

    if query.data.startswith("edit_field:"):
        field  = query.data.split(":", 1)[1]
        is_req = context.user_data.get("edit_is_request", False)
        # У запросов редактируется только сумма
        if is_req and field != "amount":
            return EDIT_CHOOSE_FIELD
        context.user_data["edit_field"] = field
        prompts = {
            "amount":   "Введи новую сумму:",
            "category": "Введи новую категорию:",
            "note":     "Введи новое примечание:",
        }
        await query.edit_message_text(prompts.get(field, "Введи значение:"))
        return EDIT_ENTERING_VALUE

    return EDIT_CHOOSE_FIELD


async def edit_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "edit_confirm_no":
        # Возврат к карточке операции
        tx_id  = context.user_data.get("edit_tx_id")
        is_req = context.user_data.get("edit_is_request", False)
        data   = await asyncio.to_thread(load_data)
        coll   = data["requests"] if is_req else data["transactions"]
        item   = next((x for x in coll if x["id"] == tx_id), None)
        if item is None or item["user_id"] != update.effective_user.id:
            await query.edit_message_text("⛔ Операция недоступна.")
            return ConversationHandler.END
        await _edit_render_item(query, context, item, is_req)
        return EDIT_CHOOSE_FIELD

    if query.data == "edit_confirm_yes":
        tx_id  = context.user_data.get("edit_tx_id")
        is_req = context.user_data.get("edit_is_request", False)
        data   = await asyncio.to_thread(load_data)
        key    = "requests" if is_req else "transactions"
        item   = next((x for x in data[key] if x["id"] == tx_id), None)
        if item is None or item["user_id"] != update.effective_user.id:
            await query.edit_message_text("⛔ Операция недоступна.")
            return ConversationHandler.END
        data[key] = [x for x in data[key] if x["id"] != tx_id]
        await save_data(data)
        if is_req:
            msg = f"🗑 Запрос {fmt(item['amount'])} ₽ удалён."
        else:
            msg = f"🗑 Операция {fmt(item['amount'])} ₽ удалена."
        await query.edit_message_text(msg)
        return ConversationHandler.END

    return EDIT_CONFIRM_DELETE


async def edit_receive_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field  = context.user_data.get("edit_field")
    tx_id  = context.user_data.get("edit_tx_id")
    is_req = context.user_data.get("edit_is_request", False)
    text   = (update.message.text or "").strip()

    data = await asyncio.to_thread(load_data)
    key  = "requests" if is_req else "transactions"
    item = next((x for x in data[key] if x["id"] == tx_id), None)
    if item is None or item["user_id"] != update.effective_user.id:
        await update.message.reply_text("⛔ Операция недоступна.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if field == "amount":
        try:
            new_val = parse_amount(text)
        except ValueError:
            await update.message.reply_text("❌ Введи корректную сумму:")
            return EDIT_ENTERING_VALUE
        item["amount"] = str(new_val)
        msg = f"✅ Сумма обновлена: {fmt(new_val)} ₽"
    elif field == "category":
        item["category"] = text[:MAX_NOTE_LEN]
        msg = f"✅ Категория обновлена: {item['category']}"
    else:
        item["note"] = text[:MAX_NOTE_LEN]
        msg = f"✅ Примечание обновлено: {item['note']}"

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
        data["requests"]     = [r for r in data["requests"]     if r["user_id"] != user_id]
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
        "^(💵 Наличные|💳 Карта|💰 Баланс|🕓 История|📨 Запросил)$"
    )

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(main_filter, handle_account)],
        states={
            ST_CHOOSE_DIRECTION:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_direction)],
            ST_ENTERING_AMOUNT:         [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
            ST_CHOOSE_CATEGORY:         [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_category)],
            ST_ENTERING_ZP_DATE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_zp_date)],
            ST_ENTERING_NOTE:           [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_note)],
            ST_ENTERING_REQUEST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_request_amount)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit_start),
            MessageHandler(filters.Regex("^✏️ Изменить$"), edit_start),
        ],
        states={
            EDIT_CHOOSE_TYPE: [
                CallbackQueryHandler(edit_pick_type, pattern="^(edit_type:|edit_cancel$)"),
            ],
            EDIT_CHOOSE_DIRECTION: [
                CallbackQueryHandler(edit_pick_direction, pattern="^(edit_dir:|edit_back_type$|edit_cancel$)"),
            ],
            EDIT_LIST: [
                CallbackQueryHandler(
                    edit_list_action,
                    pattern="^(edit_sel:|edit_page:|edit_back_type$|edit_back_dir$|edit_noop$|edit_cancel$)",
                ),
            ],
            EDIT_CHOOSE_FIELD: [
                CallbackQueryHandler(
                    edit_field_action,
                    pattern="^(edit_field:|edit_delete$|edit_back_list$|edit_cancel$)",
                ),
            ],
            EDIT_CONFIRM_DELETE: [
                CallbackQueryHandler(edit_confirm_delete, pattern="^edit_confirm_(yes|no)$"),
            ],
            EDIT_ENTERING_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_value),
            ],
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
    # edit_conv ставим первым: чтобы кнопка «✏️ Изменить» захватывалась им,
    # а не общей add_conv (которая иначе съест регекспом из main_filter).
    app.add_handler(edit_conv)
    app.add_handler(add_conv)

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
