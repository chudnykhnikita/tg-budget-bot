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


def _money_col(sheet_id: int, col: int) -> dict:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0" ₽";-#,##0" ₽";"-"'}}},
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
    # Running balance on expense sheets.
    for title in (m.SHEET_OPS_CARD_OUT, m.SHEET_OPS_CASH_OUT):
        sid = ids[title]
        reqs.append(_money_col(sid, 5))
        formulas = []
        for row in range(2, 1002):
            formulas.append([
                f'=IF(B{row}="";"";SUMIFS($B$2:B{row};$H$2:H{row};"Активна"))'
            ])
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


def write_summary(svc, ids: dict[str, int]) -> None:
    sid = ids[m.SHEET_SUMMARY]
    # ru_RU formulas
    values = [
        ["Бюджет ИП Чудных Н.Е.", "", "Период с", "=DATE(YEAR(TODAY());MONTH(TODAY());1)"],
        ["Журналы пишет бот. Остатки и итоги — формулы по активным строкам."],
        [],
        ["Деньги сейчас"],
        ["Наличные", (
            f"=SUMIFS('{m.SHEET_OPS_CASH_IN}'!B:B;'{m.SHEET_OPS_CASH_IN}'!G:G;\"Активна\")"
            f"-SUMIFS('{m.SHEET_OPS_CASH_OUT}'!B:B;'{m.SHEET_OPS_CASH_OUT}'!H:H;\"Активна\")"
            f"+SUMIFS('{m.SHEET_TRANSFERS}'!B:B;'{m.SHEET_TRANSFERS}'!C:C;\"Карта → Наличные\";'{m.SHEET_TRANSFERS}'!G:G;\"Активна\")"
            f"-SUMIFS('{m.SHEET_TRANSFERS}'!B:B;'{m.SHEET_TRANSFERS}'!C:C;\"Наличные → Карта\";'{m.SHEET_TRANSFERS}'!G:G;\"Активна\")"
        )],
        ["Тиньков", (
            f"=SUMIFS('{m.SHEET_OPS_CARD_IN}'!B:B;'{m.SHEET_OPS_CARD_IN}'!C:C;\"Тиньков\";'{m.SHEET_OPS_CARD_IN}'!G:G;\"Активна\")"
            f"-SUMIFS('{m.SHEET_OPS_CARD_OUT}'!B:B;'{m.SHEET_OPS_CARD_OUT}'!E:E;\"Тиньков\";'{m.SHEET_OPS_CARD_OUT}'!H:H;\"Активна\")"
            f"-SUMIFS('{m.SHEET_TRANSFERS}'!B:B;'{m.SHEET_TRANSFERS}'!C:C;\"Карта → Наличные\";'{m.SHEET_TRANSFERS}'!D:D;\"Тиньков\";'{m.SHEET_TRANSFERS}'!G:G;\"Активна\")"
            f"+SUMIFS('{m.SHEET_TRANSFERS}'!B:B;'{m.SHEET_TRANSFERS}'!C:C;\"Наличные → Карта\";'{m.SHEET_TRANSFERS}'!E:E;\"Тиньков\";'{m.SHEET_TRANSFERS}'!G:G;\"Активна\")"
        )],
        ["ВТБ", (
            f"=SUMIFS('{m.SHEET_OPS_CARD_IN}'!B:B;'{m.SHEET_OPS_CARD_IN}'!C:C;\"ВТБ\";'{m.SHEET_OPS_CARD_IN}'!G:G;\"Активна\")"
            f"-SUMIFS('{m.SHEET_OPS_CARD_OUT}'!B:B;'{m.SHEET_OPS_CARD_OUT}'!E:E;\"ВТБ\";'{m.SHEET_OPS_CARD_OUT}'!H:H;\"Активна\")"
        )],
        ["Всего", "=B5+B6+B7"],
        [],
        ["За выбранный месяц"],
        ["Выручка наличными", (
            f"=SUMIFS('{m.SHEET_OPS_CASH_IN}'!B:B;'{m.SHEET_OPS_CASH_IN}'!D:D;\"{m.SRC_SALE}\";"
            f"'{m.SHEET_OPS_CASH_IN}'!A:A;\">=\"&$D$1;'{m.SHEET_OPS_CASH_IN}'!A:A;\"<\"&EDATE($D$1;1);"
            f"'{m.SHEET_OPS_CASH_IN}'!G:G;\"Активна\")"
        )],
        ["Пополнения карты", (
            f"=SUMIFS('{m.SHEET_OPS_CARD_IN}'!B:B;'{m.SHEET_OPS_CARD_IN}'!A:A;\">=\"&$D$1;"
            f"'{m.SHEET_OPS_CARD_IN}'!A:A;\"<\"&EDATE($D$1;1);'{m.SHEET_OPS_CARD_IN}'!G:G;\"Активна\")"
        )],
        ["Расходы карты", (
            f"=SUMIFS('{m.SHEET_OPS_CARD_OUT}'!B:B;'{m.SHEET_OPS_CARD_OUT}'!A:A;\">=\"&$D$1;"
            f"'{m.SHEET_OPS_CARD_OUT}'!A:A;\"<\"&EDATE($D$1;1);'{m.SHEET_OPS_CARD_OUT}'!H:H;\"Активна\")"
        )],
        ["Расходы наличных", (
            f"=SUMIFS('{m.SHEET_OPS_CASH_OUT}'!B:B;'{m.SHEET_OPS_CASH_OUT}'!A:A;\">=\"&$D$1;"
            f"'{m.SHEET_OPS_CASH_OUT}'!A:A;\"<\"&EDATE($D$1;1);'{m.SHEET_OPS_CASH_OUT}'!H:H;\"Активна\")"
        )],
        ["Расходы всего", "=B13+B14"],
        [],
        ["Запросы"],
        ["Запрошено", f"=SUMIFS('{m.SHEET_REQUESTS}'!B:B;'{m.SHEET_REQUESTS}'!F:F;\"Активна\")"],
        ["Получено на карту", f"=SUMIFS('{m.SHEET_OPS_CARD_IN}'!B:B;'{m.SHEET_OPS_CARD_IN}'!G:G;\"Активна\")"],
        ["Осталось получить", "=B18-B19"],
        [],
        ["Расходы по статьям за месяц"],
        ["Статья", "Сумма"],
    ]
    start = 24
    for i, _cat in enumerate(m.EXPENSE_CATEGORIES):
        row = start + i
        col = chr(65 + i)
        values.append([
            f"='{m.SHEET_EXPENSE_CATS}'!{col}1",
            _cat_month_ref(f"A{row}"),
        ])
    svc.spreadsheets().values().update(
        spreadsheetId=store.SHEET_ID,
        range=f"'{m.SHEET_SUMMARY}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    reqs = [
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 4},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": NAVY,
                        "textFormat": {"bold": True, "foregroundColor": WHITE, "fontFamily": "Arial", "fontSize": 16},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 7, "endRowIndex": 8, "startColumnIndex": 0, "endColumnIndex": 2},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": GOLD,
                        "textFormat": {"bold": True, "fontSize": 14, "fontFamily": "Arial"},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 4, "endRowIndex": 20, "startColumnIndex": 1, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0" ₽";-#,##0" ₽";"-"'}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 23, "endRowIndex": 23 + len(m.EXPENSE_CATEGORIES), "startColumnIndex": 1, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0" ₽";-#,##0" ₽";"-"'}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "mmmm yyyy"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 280},
                "fields": "pixelSize",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True}},
                "fields": "gridProperties.hideGridlines",
            }
        },
    ]
    svc.spreadsheets().batchUpdate(spreadsheetId=store.SHEET_ID, body={"requests": reqs}).execute()


def _cat_month_ref(label_cell: str) -> str:
    return (
        f"=SUMIFS('{m.SHEET_OPS_CARD_OUT}'!B:B;'{m.SHEET_OPS_CARD_OUT}'!C:C;{label_cell};"
        f"'{m.SHEET_OPS_CARD_OUT}'!A:A;\">=\"&$D$1;'{m.SHEET_OPS_CARD_OUT}'!A:A;\"<\"&EDATE($D$1;1);"
        f"'{m.SHEET_OPS_CARD_OUT}'!H:H;\"Активна\")"
        f"+SUMIFS('{m.SHEET_OPS_CASH_OUT}'!B:B;'{m.SHEET_OPS_CASH_OUT}'!C:C;{label_cell};"
        f"'{m.SHEET_OPS_CASH_OUT}'!A:A;\">=\"&$D$1;'{m.SHEET_OPS_CASH_OUT}'!A:A;\"<\"&EDATE($D$1;1);"
        f"'{m.SHEET_OPS_CASH_OUT}'!H:H;\"Активна\")"
    )


def write_report(svc, ids: dict[str, int]) -> None:
    """Месячный отчёт для бухгалтера: те же формулы, период с листа Сводный."""
    n = len(m.EXPENSE_CATEGORIES)
    last = 23 + n
    values = [
        ["Отчет за месяц", "=Сводный!D1"],
        ["Показатель", "Сумма"],
        ["Выручка наличными", "=Сводный!B11"],
        ["Пополнения карты", "=Сводный!B12"],
        ["Расходы", "=Сводный!B15"],
        ["Осталось получить по запросам", "=Сводный!B20"],
        ["Наличные на конец", "=Сводный!B5"],
        ["Карты на конец", "=Сводный!B6+Сводный!B7"],
        ["Всего денег", "=Сводный!B8"],
        [],
        ["Расходы по статьям", ""],
        ["Статья", "Сумма"],
    ]
    for i in range(n):
        src = 24 + i
        values.append([f"=Сводный!A{src}", f"=Сводный!B{src}"])
    svc.spreadsheets().values().update(
        spreadsheetId=store.SHEET_ID,
        range=f"'{m.SHEET_REPORT}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    sid = ids[m.SHEET_REPORT]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=store.SHEET_ID,
        body={"requests": [
            {
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 12 + n, "startColumnIndex": 1, "endColumnIndex": 2},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": '#,##0" ₽";-#,##0" ₽";"-"'}}},
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        ]},
    ).execute()


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
                values[5] = store._balance_formula(i + 2)
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
    write_summary(svc, ids)
    write_report(svc, ids)
    if args.import_json and not args.layout_only:
        import_json(svc, JSON_PATH)
    print("Готово:", store.sheet_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
