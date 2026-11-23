"""Соответствие записей бота и листов «Бюджет ИП Чудных Н.Е.»."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

STATUS_ACTIVE = "Активна"
STATUS_DELETED = "Удалена"

SHEET_OPS_CARD_IN = "Карта поступления"
SHEET_OPS_CARD_OUT = "Карта списания"
SHEET_OPS_CASH_IN = "Наличные поступления"
SHEET_OPS_CASH_OUT = "Наличные списания"
SHEET_REQUESTS = "Запросы"
SHEET_TRANSFERS = "Переводы"
SHEET_LEDGER = "Журнал"
SHEET_SUMMARY = "Сводный"
SHEET_REPORT = "Отчет бухгалтеру"
SHEET_INCOME_CATS = "Категории поступлений"
SHEET_EXPENSE_CATS = "Категории списаний"

BANK_TINKOFF = "tinkoff"
BANK_VTB = "vtb"
BANK_LABELS = {BANK_TINKOFF: "Тиньков", BANK_VTB: "ВТБ"}
BANK_FROM_LABEL = {"Тиньков": BANK_TINKOFF, "ВТБ": BANK_VTB, "Тинькофф": BANK_TINKOFF}

SRC_TRADE = "Перевод по счету от Трейд-Плюс"
SRC_ALEXEY = "От Алексея Маркова без счета"
SRC_ANDREY = "От Андрея Шальнева без счета"
SRC_SALE = "Продажа товара"
SRC_OTHER = "Другое (требуется примечание)"
SRC_OPENING = "Стартовый остаток"

INCOME_SOURCES = [SRC_TRADE, SRC_ALEXEY, SRC_ANDREY, SRC_SALE, SRC_OTHER]
INCOME_SOURCES_ALL = INCOME_SOURCES + [SRC_OPENING]

INV_WITH = "По счету"
INV_WITHOUT = "Без счета"

CAT_BANK = "Плата банку"
CAT_ZP = "ЗП упаковщиков"
CAT_ZP_WORKERS = "ЗП работников"
CAT_DIV = "Дивиденды"
CAT_MEAL = "Обеды"
CAT_OFC = "Офис"
CAT_WH = "Склад"
CAT_DELIVERY = "Доставка"
CAT_DELIVERY_CN = "Доставка из Китая"
CAT_TAX = "Налоги"
CAT_CONTRACTORS = "Подрядчики"
CAT_COMP = "Компенсация за брак"
CAT_REFUND_15 = "Возврат 15%"
CAT_OTHER_EXP = "Другое"
CAT_PRIOR_DEBT = "долг с прошлого периода"
CAT_SALE_BOT = "Продажа со склада"

EXPENSE_CATEGORIES = [
    CAT_ZP,
    CAT_ZP_WORKERS,
    CAT_DIV,
    CAT_MEAL,
    CAT_OFC,
    CAT_WH,
    CAT_DELIVERY_CN,
    CAT_DELIVERY,
    CAT_BANK,
    CAT_TAX,
    CAT_CONTRACTORS,
    CAT_COMP,
    CAT_REFUND_15,
    CAT_OTHER_EXP,
]

EXPENSE_SUBCATEGORIES = {
    CAT_ZP_WORKERS: [
        "Никита 1 часть",
        "Никита 2 часть",
        "Михаил 1 часть",
        "Михаил 2 часть",
        "Премия Никита",
        "Премия Михаил",
    ],
    CAT_DIV: ["Никита", "Алексей", "Андрей"],
    CAT_OFC: ["Уборка", "Хозяйство"],
    CAT_WH: ["Аренда", "Вывоз мусора", "Разгрузка", "Интернет", "Закупки"],
    CAT_BANK: ["Комиссия", "Обслуживание"],
    CAT_TAX: ["Страховые взносы", "Прочее"],
    CAT_CONTRACTORS: ["Бот (Сергей)", "Юрист", "1С", "Реклама"],
}

FOLLOWUP_DONE = "done"
FOLLOWUP_DATE = "date"
FOLLOWUP_SUB = "subcategory"
FOLLOWUP_NOTE = "note"
FOLLOWUP_NOTE_REQUIRED = "note_required"

_NOTE_REQUIRED = {CAT_OTHER_EXP, CAT_REFUND_15}
_NOTE_OPTIONAL = {CAT_DELIVERY_CN, CAT_DELIVERY, CAT_COMP}


def expense_followup(category: str) -> str:
    if category == CAT_ZP:
        return FOLLOWUP_DATE
    if EXPENSE_SUBCATEGORIES.get(category):
        return FOLLOWUP_SUB
    if category in _NOTE_REQUIRED:
        return FOLLOWUP_NOTE_REQUIRED
    if category in _NOTE_OPTIONAL:
        return FOLLOWUP_NOTE
    return FOLLOWUP_DONE


def income_followup(source: str) -> str:
    if source == SRC_OTHER:
        return FOLLOWUP_NOTE_REQUIRED
    return FOLLOWUP_DONE


def strip_emoji(text: str | None) -> str:
    if not text:
        return ""
    out = []
    for ch in text:
        o = ord(ch)
        if ch in ("️", "\u200d"):
            continue
        if 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF:
            continue
        out.append(ch)
    return "".join(out).strip(" ·-")


def category_for_sheet(category: str | None) -> str:
    raw = strip_emoji(category)
    aliases = {
        "Продажа со склада": SRC_SALE,
        "продажа бочек": SRC_SALE,
        "продажа бочки": SRC_SALE,
        "Продажа бочек": SRC_SALE,
        "Компенсация клиенту за брак": CAT_COMP,
        "Компенсация за брак": CAT_COMP,
        "остаток": SRC_OPENING,
        "Остаток": SRC_OPENING,
        "остаток с прошлого периода": SRC_OPENING,
        "Остаток с прошлого периода": SRC_OPENING,
        "Комиссия банка": CAT_BANK,
        "Комиссия банку": CAT_BANK,
        "Комиссия банка за вывод средств": CAT_BANK,
        "Доставка из Китая": CAT_DELIVERY_CN,
        "доставка из китая": CAT_DELIVERY_CN,
        "Возврат 15%": CAT_REFUND_15,
        "Компенсация 15%": CAT_REFUND_15,
        "от Алексея": SRC_ALEXEY,
        "от Алексея Маркова": SRC_ALEXEY,
        "отдал Алексей Марков на расходы": SRC_ALEXEY,
        "Дивиденды Алексей": CAT_DIV,
        "Дивиденды Андрей": CAT_DIV,
        "Дивиденды Никита": CAT_DIV,
    }
    if raw in aliases:
        return aliases[raw]
    low = raw.lower()
    if "15%" in raw.replace(" ", "") or "15 %" in raw:
        if any(w in low for w in ("возврат", "компенсация", "прочие")):
            return CAT_REFUND_15
    if "китай" in low and "достав" in low:
        return CAT_DELIVERY_CN
    return raw


def income_source_for_sheet(account: str, category: str | None) -> str:
    sheet_cat = category_for_sheet(category)
    if sheet_cat in {SRC_TRADE, SRC_ALEXEY, SRC_ANDREY, SRC_SALE, SRC_OTHER, SRC_OPENING}:
        return sheet_cat
    if account == "cash" and sheet_cat in {CAT_SALE_BOT, ""}:
        return SRC_SALE if sheet_cat == CAT_SALE_BOT else SRC_OPENING if not sheet_cat else sheet_cat
    if account == "card" and not sheet_cat:
        return SRC_TRADE
    if sheet_cat == CAT_SALE_BOT:
        return SRC_SALE
    low = (category or "").lower()
    if "алексе" in low:
        return SRC_ALEXEY
    if "андре" in low and "марков" not in low:
        return SRC_ANDREY
    if "прочие" in low or "оптовый" in low or "темрюк" in low:
        return SRC_OTHER
    return sheet_cat or SRC_OTHER


def expense_from_label(category: str | None, note: str | None) -> tuple[str, str | None]:
    """Free-text Excel category → sheet category + note."""
    raw = strip_emoji(category)
    kept = strip_emoji(note) or None
    if not raw:
        return CAT_OTHER_EXP, kept
    mapped = category_for_sheet(raw)
    if mapped in EXPENSE_CATEGORIES:
        if mapped == CAT_DIV and raw.lower().startswith("дивиденды") and " " in raw:
            who = strip_emoji(raw.split(None, 1)[1]) or kept
            return CAT_DIV, who
        return mapped, kept
    low = raw.lower()
    if low.startswith("дивиденды"):
        who = strip_emoji(raw.split(None, 1)[1]) if " " in raw else None
        return CAT_DIV, who or kept
    if "михаил" in low and ("зп" in low or "часть" in low or "зарплат" in low):
        part = "Михаил 2 часть" if "2" in raw else "Михаил 1 часть" if "1" in raw else "Михаил"
        return CAT_ZP_WORKERS, part
    if "никита" in low and ("зп" in low or "часть" in low or "зарплат" in low):
        part = "Никита 2 часть" if "2" in raw else "Никита 1 часть" if "1" in raw else "Никита"
        return CAT_ZP_WORKERS, part
    if "обед" in low:
        return CAT_MEAL, kept or raw
    if "серге" in low:
        return CAT_CONTRACTORS, "Бот (Сергей)"
    if low.startswith("доставка") and "китай" not in low:
        extra = raw[len("Доставка"):].strip(" -")
        return CAT_DELIVERY, extra or kept
    return mapped, kept


def bank_label(bank: str | None) -> str:
    if not bank:
        return ""
    return BANK_LABELS.get(bank, bank)


def normalize_transfer(record: dict) -> dict:
    """Cash has no bank; the card side of a transfer always has a bank."""
    rec = dict(record)
    src = rec.get("from")
    dst = rec.get("to")
    if src == "cash" and dst == "card":
        rec["bank_to"] = rec.get("bank_to") or rec.get("bank") or BANK_TINKOFF
        rec["bank"] = None
    elif src == "card" and dst == "cash":
        rec["bank"] = rec.get("bank") or BANK_TINKOFF
        rec["bank_to"] = None
    return rec


def bank_key(label: str | None) -> str | None:
    if not label:
        return None
    return BANK_FROM_LABEL.get(str(label).strip(), None)


def parse_amount(value) -> Decimal:
    text = str(value or "0").replace("\xa0", "").replace(" ", "").replace(",", ".")
    return Decimal(text).quantize(Decimal("0.01"), ROUND_HALF_UP)


def date_only(value: str | None) -> str:
    """JSON '2026-06-17 12:20' → '2026-06-17'."""
    if not value:
        return ""
    text = str(value).strip()
    if " " in text:
        text = text.split(" ", 1)[0]
    if "." in text and text.count(".") == 2:
        d, m, y = text.split(".")
        if len(y) == 4:
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    return text[:10]


def direction_label(src: str, dst: str) -> str:
    names = {"card": "Карта", "cash": "Наличные"}
    return f"{names.get(src, src)} → {names.get(dst, dst)}"


def parse_direction(label: str) -> tuple[str, str]:
    left, right = [p.strip() for p in label.split("→", 1)]
    names = {"Карта": "card", "Наличные": "cash"}
    return names.get(left, left), names.get(right, right)


LEDGER_HEADERS = [
    "Дата",
    "Поступление",
    "Списание",
    "Счёт",
    "Сумма",
    "Категория",
    "Подкатегория",
    "Плавающий баланс",
    "Ключ",
    "Статус",
]
LEDGER_MAX_ROWS = 10000


def ledger_title(index: int) -> str:
    return SHEET_LEDGER if index <= 1 else f"{SHEET_LEDGER} {index}"


def ledger_index(title: str) -> int | None:
    if title == SHEET_LEDGER:
        return 1
    prefix = SHEET_LEDGER + " "
    if title.startswith(prefix):
        tail = title[len(prefix):].strip()
        if tail.isdigit():
            return int(tail)
    return None


def is_period_carry(record: dict) -> bool:
    if record.get("type") == "income":
        return income_source_for_sheet(record.get("account") or "card", record.get("category")) == SRC_OPENING
    if record.get("type") == "expense":
        return (category_for_sheet(record.get("category")) or "") == CAT_PRIOR_DEBT
    return False


def _ledger_account(record: dict) -> str:
    account = "Наличные" if record.get("account") == "cash" else "Карта"
    if record.get("account") == "card" and record.get("bank"):
        return f"{account} · {bank_label(record.get('bank'))}"
    return account


def ledger_row_from_tx(record: dict) -> list:
    amount = float(parse_amount(record.get("amount")))
    income = amount if record.get("type") == "income" else ""
    expense = amount if record.get("type") == "expense" else ""
    if record.get("type") == "income":
        category = income_source_for_sheet(record.get("account") or "card", record.get("category"))
    else:
        category = category_for_sheet(record.get("category")) or record.get("category") or CAT_OTHER_EXP
    return [
        date_only(record.get("date")),
        income,
        expense,
        _ledger_account(record),
        amount,
        category,
        strip_emoji(record.get("note")),
        None,
        record.get("id") or "",
        record.get("status") or STATUS_ACTIVE,
    ]


def ledger_row_from_transfer(record: dict) -> list:
    amount = float(parse_amount(record.get("amount")))
    return [
        date_only(record.get("date")),
        "",
        "",
        direction_label(record.get("from") or "card", record.get("to") or "cash"),
        amount,
        "Перевод",
        "",
        None,
        record.get("id") or "",
        record.get("status") or STATUS_ACTIVE,
    ]


def tx_sheet(account: str, t_type: str) -> str:
    if account == "card" and t_type == "income":
        return SHEET_OPS_CARD_IN
    if account == "card" and t_type == "expense":
        return SHEET_OPS_CARD_OUT
    if account == "cash" and t_type == "income":
        return SHEET_OPS_CASH_IN
    if account == "cash" and t_type == "expense":
        return SHEET_OPS_CASH_OUT
    raise ValueError(f"unknown account/type {account}/{t_type}")


def transaction_to_row(record: dict) -> tuple[str, list]:
    account = record.get("account") or "card"
    t_type = record["type"]
    sheet = tx_sheet(account, t_type)
    amount = float(parse_amount(record.get("amount")))
    date = date_only(record.get("date"))
    bank = bank_label(record.get("bank")) if account == "card" else ""
    key = record.get("id") or ""
    status = record.get("status") or STATUS_ACTIVE
    if t_type == "income":
        source = income_source_for_sheet(account, record.get("category"))
        note = strip_emoji(record.get("note"))
        # Источник «Другое» держит свободный текст в примечании.
        if source == SRC_OTHER and not note:
            note = strip_emoji(record.get("category"))
        row = [date, amount, bank, source, note, key, status, record.get("user_id") or ""]
    else:
        category = category_for_sheet(record.get("category"))
        note = strip_emoji(record.get("note"))
        row = [date, amount, category, note, bank, None, key, status, record.get("user_id") or ""]
    return sheet, row


def request_to_row(record: dict) -> list:
    invoice = record.get("invoice") or ""
    if invoice in (INV_WITH, INV_WITHOUT):
        kind = invoice
    elif record.get("bank"):
        kind = INV_WITH
    else:
        kind = INV_WITHOUT
    return [
        date_only(record.get("date")),
        float(parse_amount(record.get("amount"))),
        kind,
        bank_label(record.get("bank")),
        record.get("id") or "",
        record.get("status") or STATUS_ACTIVE,
        record.get("user_id") or "",
    ]


def transfer_to_row(record: dict) -> list:
    rec = normalize_transfer(record)
    return [
        date_only(rec.get("date")),
        float(parse_amount(rec.get("amount"))),
        direction_label(rec.get("from") or "card", rec.get("to") or "cash"),
        bank_label(rec.get("bank")),
        bank_label(rec.get("bank_to")),
        rec.get("id") or "",
        rec.get("status") or STATUS_ACTIVE,
        rec.get("user_id") or "",
    ]


def row_to_transaction(sheet: str, row: list) -> dict | None:
    padded = list(row) + [""] * 8
    if sheet in (SHEET_OPS_CARD_IN, SHEET_OPS_CASH_IN):
        date, amount, bank, source, note, key, status, user_id = padded[:8]
        if status == STATUS_DELETED or not amount:
            return None
        account = "card" if sheet == SHEET_OPS_CARD_IN else "cash"
        category = source if source != SRC_OPENING else "остаток"
        if category == SRC_SALE:
            category = "🛒 Продажа со склада"
        return {
            "id": str(key),
            "user_id": _user_id(user_id),
            "type": "income",
            "account": account,
            "amount": str(parse_amount(amount)),
            "category": category or None,
            "note": note or None,
            "bank": bank_key(bank) if account == "card" else None,
            "date": str(date),
            "status": status or STATUS_ACTIVE,
        }
    date, amount, category, note, bank, _balance, key, status, user_id = padded[:9]
    if status == STATUS_DELETED or not amount:
        return None
    account = "card" if sheet == SHEET_OPS_CARD_OUT else "cash"
    return {
        "id": str(key),
        "user_id": _user_id(user_id),
        "type": "expense",
        "account": account,
        "amount": str(parse_amount(amount)),
        "category": category or None,
        "note": note or None,
        "bank": bank_key(bank) if account == "card" else None,
        "date": str(date),
        "status": status or STATUS_ACTIVE,
    }


def row_to_request(row: list) -> dict | None:
    padded = list(row) + [""] * 6
    date, amount, kind, bank, key, status, user_id = padded[:7]
    if status == STATUS_DELETED or not amount:
        return None
    return {
        "id": str(key),
        "user_id": _user_id(user_id),
        "amount": str(parse_amount(amount)),
        "invoice": kind,
        "bank": bank_key(bank),
        "date": str(date),
        "status": status or STATUS_ACTIVE,
    }


def row_to_transfer(row: list) -> dict | None:
    padded = list(row) + [""] * 7
    date, amount, direction, bank, bank_to, key, status, user_id = padded[:8]
    if status == STATUS_DELETED or not amount:
        return None
    src, dst = parse_direction(str(direction))
    return {
        "id": str(key),
        "user_id": _user_id(user_id),
        "amount": str(parse_amount(amount)),
        "from": src,
        "to": dst,
        "bank": bank_key(bank),
        "bank_to": bank_key(bank_to),
        "date": str(date),
        "status": status or STATUS_ACTIVE,
    }


def _user_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


PAYROLL_PREFIX = "zp:"


def _clean_money(value) -> str:
    text = str(value or "")
    for mark in ("₽", "руб.", "руб"):
        text = text.replace(mark, "")
    return text.strip()


def payroll_tx_from_expense_row(sheet: str, row: list) -> dict | None:
    """Строка списания с ключом zp: — операция журнала, в том числе со статусом «Удалена».

    Склад пишет выплату упаковщику сразу в таблицу. Бюджетный бот хранит
    историю в JSON и сам эту строку не создаёт.
    """
    padded = list(row) + [""] * 9
    date, amount, category, note, bank, _balance, key, status, user_id = padded[:9]
    key = str(key or "").strip()
    if not key.startswith(PAYROLL_PREFIX):
        return None
    money = _clean_money(amount)
    if not money:
        return None
    account = "card" if sheet == SHEET_OPS_CARD_OUT else "cash"
    day = date_only(str(date or ""))
    stamp = f"{day} 12:00" if len(day) == 10 and day[4] == "-" else str(date or "")
    try:
        money_text = str(parse_amount(money))
    except Exception:
        return None
    return {
        "id": key,
        "user_id": _user_id(user_id),
        "type": "expense",
        "account": account,
        "amount": money_text,
        "category": category or None,
        "note": note or None,
        "bank": bank_key(str(bank)) if account == "card" else None,
        "date": stamp,
        "status": status or STATUS_ACTIVE,
    }


def merge_payroll(data: dict, rows: list[dict]) -> bool:
    """Дописывает новые zp:-операции и снимает отменённые. В таблицу не пишет."""
    archived = set()
    for period in data.get("periods") or []:
        for rec in period.get("transactions") or []:
            if rec.get("id"):
                archived.add(rec["id"])
    txs = data.setdefault("transactions", [])
    by_id = {rec.get("id"): rec for rec in txs}
    changed = False
    for rec in rows:
        rid = str(rec.get("id") or "")
        if not rid.startswith(PAYROLL_PREFIX) or rid in archived:
            continue
        current = by_id.get(rid)
        if current is None:
            if rec.get("status") == STATUS_DELETED:
                continue
            txs.append(rec)
            by_id[rid] = rec
            changed = True
            continue
        if rec.get("status") == STATUS_DELETED and current.get("status") != STATUS_DELETED:
            current["status"] = STATUS_DELETED
            changed = True
    return changed
