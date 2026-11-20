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


def _last_row(sheet: str) -> int:
    res = _service().spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{sheet}'!A:A"
    ).execute()
    col_a = res.get("values", [])
    for i in range(len(col_a) - 1, -1, -1):
        if col_a[i] and str(col_a[i][0]).strip() != "":
            return i + 1
    return 1


def _balance_formula(row_number: int) -> str:
    return f'=IF(B{row_number}="";"";SUMIFS($B$2:B{row_number};$H$2:H{row_number};"Активна"))'


def _append(sheet: str, row: list) -> int:
    start = _last_row(sheet) + 1
    values = list(row)
    if sheet in (m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT) and len(values) > 5:
        values[5] = _balance_formula(start)
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
    key_idx, _ = KEY_STATUS[sheet]
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
    row_number = _find_row(sheet, key)
    if row_number is None:
        return False
    _, status_idx = KEY_STATUS[sheet]
    letter = _col_letter(status_idx)
    _service().spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet}'!{letter}{row_number}",
        valueInputOption="RAW",
        body={"values": [[status]]},
    ).execute()
    _invalidate()
    return True


def append_transaction(record: dict) -> None:
    sheet, row = m.transaction_to_row(record)
    _append(sheet, row)


def append_request(record: dict) -> None:
    _append(m.SHEET_REQUESTS, m.request_to_row(record))


def append_transfer(record: dict) -> None:
    _append(m.SHEET_TRANSFERS, m.transfer_to_row(record))


def replace_transaction(record: dict) -> None:
    sheet, row = m.transaction_to_row(record)
    row_number = _find_row(sheet, record["id"])
    if row_number is None:
        _append(sheet, row)
        return
    _update_row(sheet, row_number, row)


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
        return
    _update_row(m.SHEET_TRANSFERS, row_number, row)


def delete_by_id(kind: str, key: str) -> bool:
    if kind == "requests":
        return _set_status(m.SHEET_REQUESTS, key, m.STATUS_DELETED)
    if kind == "transfers":
        return _set_status(m.SHEET_TRANSFERS, key, m.STATUS_DELETED)
    for sheet in (
        m.SHEET_OPS_CARD_IN,
        m.SHEET_OPS_CARD_OUT,
        m.SHEET_OPS_CASH_IN,
        m.SHEET_OPS_CASH_OUT,
    ):
        if _set_status(sheet, key, m.STATUS_DELETED):
            return True
    return False


def _rows(block: dict | None) -> list[list]:
    values = (block or {}).get("values") or []
    if not values:
        return []
    # skip header
    return values[1:]


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
        for key in before["transactions"]:
            if key not in after_tx:
                delete_by_id("transactions", key)

        for key, rec in after_rq.items():
            if key not in before["requests"]:
                append_request(rec)
            elif rec != before["requests"][key]:
                replace_request(rec)
        for key in before["requests"]:
            if key not in after_rq:
                delete_by_id("requests", key)

        for key, rec in after_tr.items():
            if key not in before["transfers"]:
                append_transfer(rec)
            elif rec != before["transfers"][key]:
                replace_transfer(rec)
        for key in before["transfers"]:
            if key not in after_tr:
                delete_by_id("transfers", key)

        _invalidate()
        return load_data()
