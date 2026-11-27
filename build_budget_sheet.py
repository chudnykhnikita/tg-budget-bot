#!/usr/bin/env python3
"""Раскладка «Бюджет ИП Чудных Н.Е.» + импорт из budget_data.json.

Сервис-аккаунт должен быть редактором таблицы. Создать файл он не может.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

import budget_map as m
import sheets_budget as store

ROOT = Path(__file__).resolve().parent
JSON_PATH = ROOT / "budget_data.json"

HEADERS = {
    m.SHEET_OPS_CARD_IN: ["Дата", "Сумма", "Банк", "Источник", "Примечание", "Ключ", "Статус", "Кто"],
    m.SHEET_OPS_CASH_IN: ["Дата", "Сумма", "Банк", "Источник", "Примечание", "Ключ", "Статус", "Кто"],
    m.SHEET_OPS_CARD_OUT: [
        "Дата", "Сумма", "Категория", "Подкатегория/примечание", "Банк",
        "Плавающий баланс", "Ключ", "Статус", "Кто",
    ],
    m.SHEET_OPS_CASH_OUT: [
        "Дата", "Сумма", "Категория", "Подкатегория/примечание", "Банк",
        "Плавающий баланс", "Ключ", "Статус", "Кто",
    ],
    m.SHEET_REQUESTS: ["Дата", "Сумма", "По счету/без счета", "Банк", "Ключ", "Статус", "Кто"],
    m.SHEET_TRANSFERS: [
        "Дата", "Сумма", "Направление", "Банк отправителя", "Банк получателя", "Ключ", "Статус", "Кто",
    ],
}

NAVY = {"red": 0.12, "green": 0.18, "blue": 0.29}
GOLD = {"red": 1.0, "green": 0.90, "blue": 0.60}
WHITE = {"red": 1, "green": 1, "blue": 1}
INK = {"red": 0.10, "green": 0.10, "blue": 0.10}
MUTED = {"red": 0.37, "green": 0.42, "blue": 0.48}


def _svc():
    creds = service_account.Credentials.from_service_account_file(
        store.CREDENTIALS_FILE, scopes=store.SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _meta(svc):
    return svc.spreadsheets().get(
        spreadsheetId=store.SHEET_ID,
        fields="spreadsheetId,properties,sheets.properties,sheets.basicFilter,sheets.columnGroups",
    ).execute()


def _ids(meta) -> dict[str, int]:
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def _hex(rgb):
    return rgb


def ensure_headers(svc) -> None:
    data = []
    for title, header in HEADERS.items():
        data.append({"range": f"'{title}'!A1:{chr(64 + len(header))}1", "values": [header]})
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=store.SHEET_ID,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def rename_report(svc, ids: dict[str, int]) -> dict[str, int]:
    if m.SHEET_REPORT in ids:
        return ids
    old = "Отчет Ноябрь"
    if old not in ids:
        return ids
    svc.spreadsheets().batchUpdate(
        spreadsheetId=store.SHEET_ID,
        body={"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": ids[old], "title": m.SHEET_REPORT},
                "fields": "title",
            }
        }]},
    ).execute()
    ids[m.SHEET_REPORT] = ids.pop(old)
    return ids


def resize_grids(svc, ids: dict[str, int]) -> None:
    reqs = []
    for title, header in HEADERS.items():
        reqs.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ids[title],
                    "gridProperties": {"rowCount": 2000, "columnCount": max(len(header), 9)},
                },
                "fields": "gridProperties.rowCount,gridProperties.columnCount",
            }
        })
    reqs.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": ids[m.SHEET_EXPENSE_CATS],
                "gridProperties": {"rowCount": 20, "columnCount": len(m.EXPENSE_CATEGORIES)},
            },
            "fields": "gridProperties.rowCount,gridProperties.columnCount",
        }
    })
    svc.spreadsheets().batchUpdate(spreadsheetId=store.SHEET_ID, body={"requests": reqs}).execute()


def clear_sheets(svc) -> None:
    ranges = [f"'{title}'!A:Z" for title in list(HEADERS) + [m.SHEET_SUMMARY, m.SHEET_REPORT, m.SHEET_INCOME_CATS, m.SHEET_EXPENSE_CATS]]
    svc.spreadsheets().values().batchClear(
        spreadsheetId=store.SHEET_ID, body={"ranges": ranges}
    ).execute()


def write_categories(svc) -> None:
    income = [["Источник"]] + [[src] for src in m.INCOME_SOURCES_ALL]
    subs = m.EXPENSE_SUBCATEGORIES
    depth = max((len(v) for v in subs.values()), default=0)
    expense = [list(m.EXPENSE_CATEGORIES)]
    for i in range(depth):
        expense.append([subs.get(cat, [""] * (i + 1))[i] if i < len(subs.get(cat, [])) else "" for cat in m.EXPENSE_CATEGORIES])
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=store.SHEET_ID,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"'{m.SHEET_INCOME_CATS}'!A1", "values": income},
            {"range": f"'{m.SHEET_EXPENSE_CATS}'!A1", "values": expense},
        ]},
    ).execute()


def _repeat_header(sheet_id: int, n_cols: int) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": n_cols,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": NAVY,
                    "textFormat": {"bold": True, "foregroundColor": WHITE, "fontFamily": "Arial", "fontSize": 10},
                    "horizontalAlignment": "CENTER",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
        }
    }


def _date_col(sheet_id: int) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


BALANCE_PATTERN = '#,##0" ₽";[Red]-#,##0" ₽";0" ₽"'   # «Плавающий баланс»: минус красным, не скобки (27.11.2026)


def _money_col(sheet_id: int, col: int, pattern: str = '#,##0" ₽";-#,##0" ₽";"-"') -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def _clear_filters_and_groups(svc, meta) -> None:
    reqs = []
    for sheet in meta.get("sheets", []):
        sid = sheet["properties"]["sheetId"]
        if sheet.get("basicFilter"):
            reqs.append({"clearBasicFilter": {"sheetId": sid}})
        for group in sheet.get("columnGroups", []):
            reqs.append({"deleteDimensionGroup": {"range": group["range"]}})
    if reqs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=store.SHEET_ID, body={"requests": reqs}
        ).execute()


def format_journals(svc, ids: dict[str, int]) -> None:
    reqs = []
    for title, header in HEADERS.items():
        sid = ids[title]
        reqs.append(_repeat_header(sid, len(header)))
        reqs.append(_date_col(sid))
        reqs.append(_money_col(sid, 1))
        reqs.append({
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        })
        reqs.append({
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": sid,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(header),
                    }
                }
            }
        })
        key_idx, status_idx = store.KEY_STATUS[title]
        reqs.append({
            "addDimensionGroup": {
                "range": {
                    "sheetId": sid,
                    "dimension": "COLUMNS",
                    "startIndex": key_idx,
                    "endIndex": len(header),
                }
            }
        })
        reqs.append(_dropdown(sid, status_idx, [m.STATUS_ACTIVE, m.STATUS_DELETED]))
    reqs.extend(_journal_dropdowns(ids))
    # Running balance on expense sheets: account remainder after the row (see store._balance_formula).
    for title in (m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT):
        sid = ids[title]
        reqs.append(_money_col(sid, 5, BALANCE_PATTERN))
        formulas = []
        for row in range(2, 1002):
            formulas.append([store._balance_formula(row, title)])
        svc.spreadsheets().values().update(
            spreadsheetId=store.SHEET_ID,
            range=f"'{title}'!F2:F1001",
            valueInputOption="USER_ENTERED",
            body={"values": formulas},
        ).execute()
    svc.spreadsheets().batchUpdate(
        spreadsheetId=store.SHEET_ID, body={"requests": reqs}
    ).execute()


def _dropdown(sheet_id: int, col: int, values: list[str], *, strict: bool = True) -> dict:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in values],
                },
                "showCustomUi": True,
                "strict": strict,
            },
        }
    }


def _dropdown_range(sheet_id: int, col: int, a1: str, *, strict: bool = True) -> dict:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_RANGE",
                    "values": [{"userEnteredValue": a1}],
                },
                "showCustomUi": True,
                "strict": strict,
            },
        }
    }


def _journal_dropdowns(ids: dict[str, int]) -> list[dict]:
    banks = list(m.BANK_LABELS.values())
    reqs = []
    for title in (m.SHEET_OPS_CARD_IN, m.SHEET_OPS_CASH_IN):
        sid = ids[title]
        reqs.append(_dropdown(sid, 2, banks, strict=False))
        reqs.append(_dropdown_range(sid, 3, f"='{m.SHEET_INCOME_CATS}'!A2:A"))
    for title in (m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT):
        sid = ids[title]
        last = chr(64 + len(m.EXPENSE_CATEGORIES))
        reqs.append(_dropdown_range(sid, 2, f"='{m.SHEET_EXPENSE_CATS}'!A1:{last}1"))
        reqs.append(_dropdown(sid, 4, banks, strict=False))
    reqs.append(_dropdown(ids[m.SHEET_REQUESTS], 2, [m.INV_WITH, m.INV_WITHOUT]))
    reqs.append(_dropdown(ids[m.SHEET_REQUESTS], 3, banks, strict=False))
    reqs.append(_dropdown(ids[m.SHEET_TRANSFERS], 2, [
        "Карта → Наличные", "Наличные → Карта", "Карта → Карта",
    ]))
    reqs.append(_dropdown(ids[m.SHEET_TRANSFERS], 3, banks, strict=False))
    reqs.append(_dropdown(ids[m.SHEET_TRANSFERS], 4, banks, strict=False))
    return reqs


ACTIVE = '"Активна"'
MONEY = '#,##0" ₽";[Red]-#,##0" ₽";0" ₽"'


def _sum(sheet: str, status_col: str, *conds: tuple[str, str]) -> str:
    """SUMIFS по столбцу B листа: только «Активна» (текущий период) + условия (столбец, критерий)."""
    parts = [f"'{sheet}'!B:B", f"'{sheet}'!{status_col}:{status_col}", ACTIVE]
    for col, crit in conds:
        parts += [f"'{sheet}'!{col}:{col}", crit]
    return "SUMIFS(" + ";".join(parts) + ")"


def _q(text: str) -> str:
    return '"' + text + '"'


def summary_values() -> list[list]:
    """Лист «Сводный»: все цифры бюджета — формулы по строкам «Активна». Бот читает их по подписям в A.
    Текущий период = строки «Активна»: закрытие переводит их в «Архив», D1 — день закрытия (только подпись)."""
    ci, co, ki, ko, tr, rq = (m.SHEET_OPS_CASH_IN, m.SHEET_OPS_CASH_OUT, m.SHEET_OPS_CARD_IN,
                              m.SHEET_OPS_CARD_OUT, m.SHEET_TRANSFERS, m.SHEET_REQUESTS)
    to_cash, from_cash = _sum(tr, "G", ("C", _q("Карта → Наличные"))), _sum(tr, "G", ("C", _q("Наличные → Карта")))

    def bank(label: str) -> list:
        b = _q(label)
        tr_in = _sum(tr, "G", ("C", _q("* → Карта")), ("E", b))
        tr_out = _sum(tr, "G", ("C", _q("Карта → *")), ("D", b))
        return [label, f"={_sum(ki, 'G', ('C', b))}", f"={_sum(ko, 'H', ('E', b))}", f"={tr_in}-{tr_out}"]

    rows = [
        ["Бюджет ИП Чудных Н.Е.", "", "Период с", ""],
        ["Все цифры — формулы по строкам «Активна» (текущий период). «Закрыть период» в боте переводит их в «Архив»."],
        [],
        ["Деньги сейчас", "Поступления", "Списания", "Переводы", "Остаток"],
        ["Наличные", f"={_sum(ci, 'G')}", f"={_sum(co, 'H')}", f"={to_cash}-{from_cash}"],
        bank("Тиньков"),
        bank("ВТБ"),
        ["Карта всего", f"={_sum(ki, 'G')}", f"={_sum(ko, 'H')}", f"={from_cash}-{to_cash}"],
        ["Всего", "=B5+B8", "=C5+C8", "=D5+D8"],
        [],
        ["За период (без перенесённых остатков)"],
        ["Выручка наличными", f"={_sum(ci, 'G', ('D', _q(m.SRC_SALE)))}"],
        ["Пополнения карты", f"={_sum(ki, 'G')}-{_sum(ki, 'G', ('D', _q(m.SRC_OPENING)))}"],
        ["Расходы карты", f"={_sum(ko, 'H')}-{_sum(ko, 'H', ('C', _q(m.CAT_PRIOR_DEBT)))}"],
        ["Расходы наличных", f"={_sum(co, 'H')}-{_sum(co, 'H', ('C', _q(m.CAT_PRIOR_DEBT)))}"],
        ["Расходы всего", "=B14+B15"],
        [],
        ["Запросы"],
        ["Запрошено", f"={_sum(rq, 'F')}"],
        ["Получено на карту", "=B13"],
        ["Осталось получить", "=B19-B20"],
        [],
        ["Расходы по статьям за период"],
        ["Статья", "Сумма"],
    ]
    for r in range(5, 10):
        rows[r - 1].append(f"=B{r}-C{r}+D{r}")
    start = len(rows) + 1
    for i, _cat in enumerate(m.EXPENSE_CATEGORIES):
        col = _col(i)
        cell = f"A{start + i}"
        rows.append([f"='{m.SHEET_EXPENSE_CATS}'!{col}1",
                     f"=IF({cell}=\"\";\"\";{_sum(ko, 'H', ('C', cell))}+{_sum(co, 'H', ('C', cell))})"])
    return rows


def _col(i: int) -> str:
    return store._col_letter(i)


SUMMARY_CATS_START = 25   # первая строка статей на «Сводном» (см. summary_values)


def write_summary(svc, ids: dict[str, int], period_from: str | None = None) -> None:
    sid = ids[m.SHEET_SUMMARY]
    values = summary_values()
    assert values[SUMMARY_CATS_START - 2] == ["Статья", "Сумма"]
    if period_from is not None:
        values[0][3] = period_from
    else:   # день закрытия хранится в D1 — не затирать
        cur = svc.spreadsheets().values().get(spreadsheetId=store.SHEET_ID, range=f"'{m.SHEET_SUMMARY}'!D1",
                                              valueRenderOption="FORMULA").execute().get("values") or [[""]]
        old = cur[0][0] if cur and cur[0] else ""
        values[0][3] = "" if str(old).startswith("=") else old
    svc.spreadsheets().values().clear(spreadsheetId=store.SHEET_ID, range=f"'{m.SHEET_SUMMARY}'!A1:Z200").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=store.SHEET_ID, range=f"'{m.SHEET_SUMMARY}'!A1",
        valueInputOption="USER_ENTERED", body={"values": values},
    ).execute()

    def fmt(r0, r1, c0, c1, cell, fields):
        return {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1,
                                         "startColumnIndex": c0, "endColumnIndex": c1},
                               "cell": {"userEnteredFormat": cell}, "fields": f"userEnteredFormat({fields})"}}
    money = {"numberFormat": {"type": "NUMBER", "pattern": MONEY}}
    n = len(values)
    reqs = [
        fmt(0, n, 0, 6, {}, "backgroundColor,textFormat,numberFormat"),   # сброс прежнего оформления
        fmt(0, 1, 0, 5, {"backgroundColor": NAVY, "textFormat": {"bold": True, "foregroundColor": WHITE,
                                                                   "fontFamily": "Arial", "fontSize": 16}},
            "backgroundColor,textFormat"),
        fmt(0, 1, 3, 4, {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}, "numberFormat"),
        fmt(3, 4, 0, 5, {"textFormat": {"bold": True}}, "textFormat"),
        fmt(8, 9, 0, 5, {"backgroundColor": GOLD, "textFormat": {"bold": True, "fontSize": 12}},
            "backgroundColor,textFormat"),
        fmt(4, 9, 1, 5, money, "numberFormat"),
        fmt(11, 21, 1, 2, money, "numberFormat"),
        fmt(SUMMARY_CATS_START - 1, n, 1, 2, money, "numberFormat"),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 280}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 5},
                                       "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True}},
                                   "fields": "gridProperties.hideGridlines"}},
    ]
    for r in (10, 17, SUMMARY_CATS_START - 3):
        reqs.append(fmt(r, r + 1, 0, 1, {"textFormat": {"bold": True}}, "textFormat"))
    svc.spreadsheets().batchUpdate(spreadsheetId=store.SHEET_ID, body={"requests": reqs}).execute()


def write_report(svc, ids: dict[str, int]) -> None:
    """Отчёт бухгалтеру за текущий период: ссылки на «Сводный»."""
    n = len(m.EXPENSE_CATEGORIES)
    values = [
        ["Отчет за период с", "=Сводный!D1"],
        ["Показатель", "Сумма"],
        ["Выручка наличными", "=Сводный!B12"],
        ["Пополнения карты", "=Сводный!B13"],
        ["Расходы", "=Сводный!B16"],
        ["Осталось получить по запросам", "=Сводный!B21"],
        ["Наличные на конец", "=Сводный!E5"],
        ["Карты на конец", "=Сводный!E8"],
        ["Всего денег", "=Сводный!E9"],
        [],
        ["Расходы по статьям", ""],
        ["Статья", "Сумма"],
    ]
    for i in range(n):
        src = SUMMARY_CATS_START + i
        values.append([f"=Сводный!A{src}", f"=Сводный!B{src}"])
    svc.spreadsheets().values().clear(spreadsheetId=store.SHEET_ID, range=f"'{m.SHEET_REPORT}'!A1:Z200").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=store.SHEET_ID, range=f"'{m.SHEET_REPORT}'!A1",
        valueInputOption="USER_ENTERED", body={"values": values},
    ).execute()
    sid = ids[m.SHEET_REPORT]
    svc.spreadsheets().batchUpdate(spreadsheetId=store.SHEET_ID, body={"requests": [
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 12 + n, "startColumnIndex": 1, "endColumnIndex": 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": MONEY}}},
                        "fields": "userEnteredFormat.numberFormat"}},
    ]}).execute()


def import_json(svc, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    by_sheet: dict[str, list[list]] = {title: [] for title in HEADERS}
    for rec in data.get("transactions", []):
        sheet, row = m.transaction_to_row(rec)
        by_sheet[sheet].append(row)
    for rec in data.get("requests", []):
        by_sheet[m.SHEET_REQUESTS].append(m.request_to_row(rec))
    for rec in data.get("transfers", []):
        by_sheet[m.SHEET_TRANSFERS].append(m.transfer_to_row(rec))

    payload = []
    for title, rows in by_sheet.items():
        if not rows:
            continue
        cleaned = []
        for i, row in enumerate(rows):
            values = list(row)
            if title in (m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT) and len(values) > 5:
                values[5] = store._balance_formula(i + 2, title)
            cleaned.append(["" if v is None else v for v in values])
        end = chr(64 + len(HEADERS[title]))
        payload.append({
            "range": f"'{title}'!A2:{end}{len(cleaned) + 1}",
            "values": cleaned,
        })
        print(f"{title}: {len(cleaned)} строк")
    if payload:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=store.SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": payload},
        ).execute()


def ensure_ledger(svc, ids: dict[str, int], *, backfill: bool) -> dict[str, int]:
    """Create the perpetual journal tab. Never clears existing journal rows."""
    if m.SHEET_LEDGER not in ids:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=store.SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {
                "title": m.SHEET_LEDGER,
                "gridProperties": {
                    "rowCount": m.LEDGER_MAX_ROWS,
                    "columnCount": len(m.LEDGER_HEADERS),
                },
            }}}]},
        ).execute()
        ids = _ids(_meta(svc))
        print("создан лист:", m.SHEET_LEDGER)
    svc.spreadsheets().values().update(
        spreadsheetId=store.SHEET_ID,
        range=f"'{m.SHEET_LEDGER}'!A1:J1",
        valueInputOption="RAW",
        body={"values": [m.LEDGER_HEADERS]},
    ).execute()
    sid = ids[m.SHEET_LEDGER]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=store.SHEET_ID,
        body={"requests": [
            {"updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {
                        "rowCount": m.LEDGER_MAX_ROWS,
                        "columnCount": len(m.LEDGER_HEADERS),
                        "frozenRowCount": 1,
                    },
                },
                "fields": "gridProperties",
            }},
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": NAVY,
                    "textFormat": {"foregroundColor": GOLD, "bold": True},
                }},
                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat",
            }},
        ]},
    ).execute()
    if backfill and JSON_PATH.exists():
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        n = store.seed_ledger(data)
        print(f"журнал: {n} строк" if n else "журнал уже заполнен, пропуск")
    return ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-json", action="store_true")
    parser.add_argument("--layout-only", action="store_true")
    parser.add_argument("--ensure-ledger", action="store_true")
    parser.add_argument("--summary-only", action="store_true",
                        help="переписать только «Сводный» и «Отчет бухгалтеру» (данные не трогает)")
    parser.add_argument("--period-from", help="день закрытия для «Сводный» D1, ГГГГ-ММ-ДД")
    args = parser.parse_args()
    if not store.configured():
        print(
            "Нет GOOGLE_CREDENTIALS_FILE. Положите JSON сервис-аккаунта и:\n"
            "  export GOOGLE_CREDENTIALS_FILE=/path/to/service-account.json\n"
            "Таблица должна быть расшарена на budget@protean-bazaar-496812-m5.iam.gserviceaccount.com",
            file=sys.stderr,
        )
        return 1
    svc = _svc()
    try:
        meta = _meta(svc)
    except Exception as exc:
        print(f"Нет доступа к таблице: {exc}", file=sys.stderr)
        print(
            "Откройте https://docs.google.com/spreadsheets/d/"
            f"{store.SHEET_ID}/edit и выдайте редактора "
            "budget@protean-bazaar-496812-m5.iam.gserviceaccount.com",
            file=sys.stderr,
        )
        return 1
    ids = _ids(meta)
    ids = rename_report(svc, ids)
    if args.summary_only:
        write_summary(svc, ids, args.period_from)
        write_report(svc, ids)
        print("Готово:", store.sheet_url())
        return 0
    if args.ensure_ledger:
        ensure_ledger(svc, ids, backfill=True)
        print("Готово:", store.sheet_url())
        return 0
    _clear_filters_and_groups(svc, meta)
    needed = list(HEADERS) + [m.SHEET_SUMMARY, m.SHEET_REPORT, m.SHEET_INCOME_CATS, m.SHEET_EXPENSE_CATS]
    missing = [name for name in needed if name not in ids]
    if missing:
        print("В таблице нет листов:", ", ".join(missing), file=sys.stderr)
        return 1
    resize_grids(svc, ids)
    clear_sheets(svc)
    write_categories(svc)
    ensure_headers(svc)
    format_journals(svc, ids)
    write_summary(svc, ids, args.period_from)
    write_report(svc, ids)
    if args.import_json and not args.layout_only:
        import_json(svc, JSON_PATH)
    print("Готово:", store.sheet_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
