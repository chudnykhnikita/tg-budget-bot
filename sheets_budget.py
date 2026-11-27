"""Google Sheets — источник правды бюджета.

Таблица: «Бюджет ИП Чудных Н.Е.». Бот пишет журналы, формулы на «Сводный»
считают остатки. JSON остаётся дневной копией на случай сбоя API.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import budget_map as m

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get(
    "BUDGET_SHEET_ID",
    "1ot1C-aPMHCKqpfn8Sa3ETWhM8uhwp78i8uhmbSI-uwo",
)
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")

_lock = threading.RLock()
_svc = None
_cache: dict | None = None

# 0-based index of Ключ / Статус after the human-facing columns.
KEY_STATUS = {
    m.SHEET_OPS_CARD_IN: (5, 6),
    m.SHEET_OPS_CASH_IN: (5, 6),
    m.SHEET_OPS_CARD_OUT: (6, 7),
    m.SHEET_OPS_CASH_OUT: (6, 7),
    m.SHEET_REQUESTS: (4, 5),
    m.SHEET_TRANSFERS: (5, 6),
}
LEDGER_KEY_STATUS = (8, 9)

READ_RANGES = [
    f"'{m.SHEET_OPS_CARD_IN}'!A:H",
    f"'{m.SHEET_OPS_CASH_IN}'!A:H",
    f"'{m.SHEET_OPS_CARD_OUT}'!A:I",
    f"'{m.SHEET_OPS_CASH_OUT}'!A:I",
    f"'{m.SHEET_REQUESTS}'!A:G",
    f"'{m.SHEET_TRANSFERS}'!A:H",
]


def configured() -> bool:
    return bool(CREDENTIALS_FILE and os.path.exists(CREDENTIALS_FILE) and SHEET_ID)


def sheet_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"


def _service():
    global _svc
    if _svc is None:
        if not configured():
            raise RuntimeError("Google Sheets не настроен: нужен GOOGLE_CREDENTIALS_FILE")
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE, scopes=SCOPES
        )
        _svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _svc


def _invalidate():
    global _cache
    _cache = None


def _col_letter(index: int) -> str:
    n = index + 1
    letters = []
    while n:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _key_status(sheet: str) -> tuple[int, int]:
    if m.ledger_index(sheet) is not None:
        return LEDGER_KEY_STATUS
    return KEY_STATUS[sheet]


def _last_row(sheet: str) -> int:
    res = _service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{sheet}'!A:A"
    ).execute()
    col_a = res.get("values", [])
    for i in range(len(col_a) - 1, -1, -1):
        if col_a[i] and str(col_a[i][0]).strip() != "":
            return i + 1
    return 1


def _balance_formula(row_number: int, sheet: str = m.SHEET_OPS_CASH_OUT) -> str:
    """«Плавающий баланс» листа списаний — остаток счёта после этой строки, как в «💰 Баланс»:
    поступления счёта + переводы на счёт − переводы со счёта − списания, всё «Активна» по дату строки включительно;
    списания того же дня — по порядку строк. У строк «Удалена» (прошлый период, удалённые) — пусто. До 27.11.2026 здесь была нарастающая сумма списаний."""
    n = row_number
    cash = sheet == m.SHEET_OPS_CASH_OUT
    inc = m.SHEET_OPS_CASH_IN if cash else m.SHEET_OPS_CARD_IN
    me, other = ("Наличные", "Карта") if cash else ("Карта", "Наличные")
    tr = m.SHEET_TRANSFERS
    on_day = f'"<="&A{n}'
    return (
        f'=IF(OR(B{n}="";H{n}<>"Активна");"";'
        f"SUMIFS('{inc}'!$B:$B;'{inc}'!$A:$A;{on_day};'{inc}'!$G:$G;\"Активна\")"
        f"+SUMIFS('{tr}'!$B:$B;'{tr}'!$C:$C;\"{other} → {me}\";'{tr}'!$A:$A;{on_day};'{tr}'!$G:$G;\"Активна\")"
        f"-SUMIFS('{tr}'!$B:$B;'{tr}'!$C:$C;\"{me} → {other}\";'{tr}'!$A:$A;{on_day};'{tr}'!$G:$G;\"Активна\")"
        f'-SUMIFS($B:$B;$A:$A;"<"&A{n};$H:$H;"Активна")'
        f'-SUMIFS($B$2:B{n};$A$2:A{n};A{n};$H$2:H{n};"Активна"))'
    )


def _ledger_balance_formula(row_number: int) -> str:
    return (
        f'=IF(E{row_number}="";"";'
        f'SUMIFS($B$2:B{row_number};$J$2:J{row_number};"Активна")'
        f'-SUMIFS($C$2:C{row_number};$J$2:J{row_number};"Активна"))'
    )


def _append(sheet: str, row: list) -> int:
    start = _last_row(sheet) + 1
    values = list(row)
    if sheet in (m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT) and len(values) > 5:
        values[5] = _balance_formula(start, sheet)
    if m.ledger_index(sheet) is not None and len(values) > 7:
        values[7] = _ledger_balance_formula(start)
    values = ["" if v is None else v for v in values]
    end = _col_letter(len(values) - 1)
    rng = f"'{sheet}'!A{start}:{end}{start}"
    _service().spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()
    _invalidate()
    return start


def _find_row(sheet: str, key: str) -> int | None:
    key_idx, _ = _key_status(sheet)
    letter = _col_letter(key_idx)
    res = _service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{sheet}'!{letter}:{letter}"
    ).execute()
    for i, cell in enumerate(res.get("values", []), start=1):
        if cell and str(cell[0]) == key:
            return i
    return None


def _update_row(sheet: str, row_number: int, row: list) -> None:
    values = ["" if v is None else v for v in row]
    end = _col_letter(len(values) - 1)
    rng = f"'{sheet}'!A{row_number}:{end}{row_number}"
    _service().spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()
    _invalidate()


def _set_status(sheet: str, key: str, status: str) -> bool:
    return _apply_status({key}, status, sheets=[sheet]) > 0


def _apply_status(keys: set[str], status: str, sheets: list[str] | None = None) -> int:
    """Mark matching Ключ rows in one batchGet + batchUpdate. Same id may sit
    on a monthly tab and on Журнал — both are updated."""
    if not keys:
        return 0
    titles = sheets if sheets is not None else list(KEY_STATUS.keys()) + _ledger_titles()
    ranges = []
    meta = []
    for title in titles:
        key_idx, status_idx = _key_status(title)
        ranges.append(f"'{title}'!{_col_letter(key_idx)}:{_col_letter(status_idx)}")
        meta.append((title, status_idx))
    result = _service().spreadsheets().values().batchGet(
        spreadsheetId=SHEET_ID, ranges=ranges
    ).execute()
    payload = []
    for (title, status_idx), block in zip(meta, result.get("valueRanges", [])):
        letter = _col_letter(status_idx)
        for i, row in enumerate(block.get("values") or [], start=1):
            if not row:
                continue
            if str(row[0]) in keys:
                payload.append({
                    "range": f"'{title}'!{letter}{i}",
                    "values": [[status]],
                })
    for i in range(0, len(payload), 100):
        _service().spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "RAW", "data": payload[i:i + 100]},
        ).execute()
    if payload:
        _invalidate()
    return len(payload)


def _sheet_titles() -> list[str]:
    meta = _service().spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="sheets.properties.title,sheets.properties.sheetId"
    ).execute()
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def _ledger_titles() -> list[str]:
    found = []
    for title in _sheet_titles():
        idx = m.ledger_index(title)
        if idx is not None:
            found.append((idx, title))
    found.sort()
    return [title for _, title in found]


def _add_ledger_sheet(title: str) -> None:
    _service().spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {
            "title": title,
            "gridProperties": {"rowCount": m.LEDGER_MAX_ROWS, "columnCount": len(m.LEDGER_HEADERS)},
        }}}]},
    ).execute()
    _service().spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{title}'!A1:J1",
        valueInputOption="RAW",
        body={"values": [m.LEDGER_HEADERS]},
    ).execute()
    _invalidate()


def _last_ledger_balance(sheet: str) -> float:
    last = _last_row(sheet)
    if last < 2:
        return 0.0
    res = _service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet}'!H{last}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    values = res.get("values") or []
    if not values or not values[0]:
        return 0.0
    try:
        return float(values[0][0])
    except (TypeError, ValueError):
        return 0.0


def current_ledger_sheet() -> str:
    titles = _ledger_titles()
    if not titles:
        _add_ledger_sheet(m.SHEET_LEDGER)
        return m.SHEET_LEDGER
    current = titles[-1]
    if _last_row(current) < m.LEDGER_MAX_ROWS - 10:
        return current
    nxt = m.ledger_title((m.ledger_index(current) or 1) + 1)
    if nxt not in titles:
        balance = _last_ledger_balance(current)
        _add_ledger_sheet(nxt)
        carry = [
            datetime_date(),
            balance if balance >= 0 else "",
            abs(balance) if balance < 0 else "",
            "—",
            abs(balance),
            "Перенос с предыдущего листа",
            current,
            None,
            f"ledger-carry-{nxt}",
            m.STATUS_ACTIVE,
        ]
        _append(nxt, carry)
    return nxt


def datetime_date() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")


def _find_ledger_row(key: str) -> tuple[str, int] | None:
    for title in _ledger_titles():
        row_number = _find_row(title, key)
        if row_number is not None:
            return title, row_number
    return None


def seed_ledger(data: dict) -> int:
    """Write open txs/transfers into an empty journal. Skips if rows already exist."""
    sheet = current_ledger_sheet()
    if _last_row(sheet) > 1:
        return 0
    items = []
    for rec in data.get("transactions") or []:
        if rec.get("status") == m.STATUS_DELETED:
            continue
        items.append((rec.get("date") or "", "transaction", rec))
    for rec in data.get("transfers") or []:
        if rec.get("status") == m.STATUS_DELETED:
            continue
        items.append((rec.get("date") or "", "transfer", rec))
    items.sort(key=lambda item: item[0])
    payload = []
    for i, (_, kind, rec) in enumerate(items, start=2):
        row = m.ledger_row_from_tx(rec) if kind == "transaction" else m.ledger_row_from_transfer(rec)
        row[7] = _ledger_balance_formula(i)
        payload.append(["" if v is None else v for v in row])
    if payload:
        end = _col_letter(len(m.LEDGER_HEADERS) - 1)
        _service().spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet}'!A2:{end}{len(payload) + 1}",
            valueInputOption="USER_ENTERED",
            body={"values": payload},
        ).execute()
        _invalidate()
    return len(payload)


def _sync_ledger(kind: str, record: dict, *, skip_carry: bool = True) -> None:
    if skip_carry and kind == "transaction" and m.is_period_carry(record):
        return
    if kind == "transaction":
        row = m.ledger_row_from_tx(record)
    else:
        row = m.ledger_row_from_transfer(record)
    key = record.get("id") or ""
    found = _find_ledger_row(key) if key else None
    if found:
        sheet, row_number = found
        if m.ledger_index(sheet) is not None and len(row) > 7:
            row[7] = _ledger_balance_formula(row_number)
        _update_row(sheet, row_number, row)
        return
    sheet = current_ledger_sheet()
    _append(sheet, row)


def append_transaction(record: dict) -> None:
    sheet, row = m.transaction_to_row(record)
    _append(sheet, row)
    _sync_ledger("transaction", record)


def append_request(record: dict) -> None:
    _append(m.SHEET_REQUESTS, m.request_to_row(record))


def append_transfer(record: dict) -> None:
    _append(m.SHEET_TRANSFERS, m.transfer_to_row(record))
    _sync_ledger("transfer", record)


def replace_transaction(record: dict) -> None:
    sheet, row = m.transaction_to_row(record)
    row_number = _find_row(sheet, record["id"])
    if row_number is None:
        _append(sheet, row)
    else:
        _update_row(sheet, row_number, row)
    _sync_ledger("transaction", record)


def replace_request(record: dict) -> None:
    row_number = _find_row(m.SHEET_REQUESTS, record["id"])
    row = m.request_to_row(record)
    if row_number is None:
        _append(m.SHEET_REQUESTS, row)
        return
    _update_row(m.SHEET_REQUESTS, row_number, row)


def replace_transfer(record: dict) -> None:
    row_number = _find_row(m.SHEET_TRANSFERS, record["id"])
    row = m.transfer_to_row(record)
    if row_number is None:
        _append(m.SHEET_TRANSFERS, row)
    else:
        _update_row(m.SHEET_TRANSFERS, row_number, row)
    _sync_ledger("transfer", record)


def delete_by_id(kind: str, key: str) -> bool:
    if kind == "requests":
        sheets = [m.SHEET_REQUESTS]
    elif kind == "transfers":
        sheets = [m.SHEET_TRANSFERS] + _ledger_titles()
    else:
        sheets = [
            m.SHEET_OPS_CARD_IN,
            m.SHEET_OPS_CARD_OUT,
            m.SHEET_OPS_CASH_IN,
            m.SHEET_OPS_CASH_OUT,
        ] + _ledger_titles()
    return _apply_status({key}, m.STATUS_DELETED, sheets=sheets) > 0


def _rows(block: dict | None) -> list[list]:
    values = (block or {}).get("values") or []
    if not values:
        return []
    # skip header
    return values[1:]


def external_payroll() -> list[dict]:
    """Выплаты zp: с листов списаний. Пустой список, если таблицы нет."""
    if not configured():
        return []
    ranges = [
        f"'{m.SHEET_OPS_CARD_OUT}'!A:I",
        f"'{m.SHEET_OPS_CASH_OUT}'!A:I",
    ]
    result = _service().spreadsheets().values().batchGet(
        spreadsheetId=SHEET_ID,
        ranges=ranges,
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    found = []
    for sheet, block in zip((m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT), result.get("valueRanges", [])):
        for row in _rows(block):
            item = m.payroll_tx_from_expense_row(sheet, row)
            if item:
                found.append(item)
    return found


def load_data() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        result = _service().spreadsheets().values().batchGet(
            spreadsheetId=SHEET_ID,
            ranges=READ_RANGES,
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        ).execute()
        blocks = result.get("valueRanges", [])
        by_range = {b.get("range", "").split("!")[0].strip("'"): b for b in blocks}

        txs: list[dict] = []
        for sheet in (
            m.SHEET_OPS_CARD_IN,
            m.SHEET_OPS_CASH_IN,
            m.SHEET_OPS_CARD_OUT,
            m.SHEET_OPS_CASH_OUT,
        ):
            for row in _rows(by_range.get(sheet)):
                item = m.row_to_transaction(sheet, row)
                if item:
                    txs.append(item)
        reqs = []
        for row in _rows(by_range.get(m.SHEET_REQUESTS)):
            item = m.row_to_request(row)
            if item:
                reqs.append(item)
        trs = []
        for row in _rows(by_range.get(m.SHEET_TRANSFERS)):
            item = m.row_to_transfer(row)
            if item:
                trs.append(item)
        _cache = {"transactions": txs, "requests": reqs, "transfers": trs}
        return _cache


def modify(mutator: Callable[[dict], Any]) -> dict:
    """Совместимость со старым JSON-мутатором: применяет к снимку и пишет дельту.

    Для новых записей вызывайте append_*; этот путь нужен редактированию,
    которое меняет поля существующего id.
    """
    with _lock:
        data = load_data()
        before = {
            "transactions": {t["id"]: t.copy() for t in data["transactions"]},
            "requests": {r["id"]: r.copy() for r in data["requests"]},
            "transfers": {t["id"]: t.copy() for t in data["transfers"]},
        }
        mutator(data)
        after_tx = {t["id"]: t for t in data["transactions"]}
        after_rq = {r["id"]: r for r in data["requests"]}
        after_tr = {t["id"]: t for t in data["transfers"]}

        for key, rec in after_tx.items():
            if key not in before["transactions"]:
                append_transaction(rec)
            elif rec != before["transactions"][key]:
                replace_transaction(rec)
        for key, rec in after_rq.items():
            if key not in before["requests"]:
                append_request(rec)
            elif rec != before["requests"][key]:
                replace_request(rec)
        for key, rec in after_tr.items():
            if key not in before["transfers"]:
                append_transfer(rec)
            elif rec != before["transfers"][key]:
                replace_transfer(rec)

        gone = set()
        gone.update(k for k in before["transactions"] if k not in after_tx)
        gone.update(k for k in before["requests"] if k not in after_rq)
        gone.update(k for k in before["transfers"] if k not in after_tr)
        if gone:
            _apply_status(gone, m.STATUS_DELETED)

        _invalidate()
        return load_data()
