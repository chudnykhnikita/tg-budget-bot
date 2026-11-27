"""HTML cards for budget bot reports. No Telegram imports."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import budget_map as m
from tg_rich import esc, table

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_RU_MONTHS = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)
BANK_ORDER = [(m.BANK_TINKOFF, "Тиньков"), (m.BANK_VTB, "ВТБ")]


def money(amount) -> str:
    d = Decimal(str(amount)).quantize(Decimal("1"), ROUND_HALF_UP)
    body = f"{abs(d):,}".replace(",", "\u00a0")
    if d < 0:
        return f"−{body}"
    return body


def money_cell(amount, *, signed: bool = False) -> str:
    d = Decimal(str(amount))
    text = money(d)
    if signed and d > 0:
        text = f"+{text}"
    if d < 0:
        return f"🔴 {esc(text)}"
    return esc(text)


def _alive(rec: dict) -> bool:
    return rec.get("status") != m.STATUS_DELETED


def _amt(rec: dict) -> Decimal:
    return Decimal(str(rec.get("amount") or 0))


def _for_user(rows: list[dict], user_id: int) -> list[dict]:
    return [r for r in rows if r.get("user_id") == user_id and _alive(r)]


def _short_date(stamp: str | None) -> str:
    day = m.date_only(stamp)
    if len(day) >= 10:
        return f"{day[8:10]}.{day[5:7]}"
    return stamp or ""


def _full_date(stamp: str | None) -> str:
    day = m.date_only(stamp)
    if len(day) >= 10:
        return f"{day[8:10]}.{day[5:7]}.{day[:4]}"
    return stamp or ""


def _period_label(stamps: list[str]) -> str:
    days = sorted({m.date_only(s) for s in stamps if s})
    days = [d for d in days if d]
    if not days:
        return ""
    if days[0] == days[-1]:
        return _full_date(days[0])
    a, b = days[0], days[-1]
    if a[:4] == b[:4]:
        return f"{a[8:10]}.{a[5:7]}–{b[8:10]}.{b[5:7]}.{b[:4]}"
    return f"{_full_date(a)}–{_full_date(b)}"


def _account_label(account: str | None) -> str:
    return "Наличные" if account == "cash" else "Карта"


def _bank_tr_net(trs: list[dict], bank_key: str) -> Decimal:
    incoming = Decimal("0")
    outgoing = Decimal("0")
    for t in trs:
        amount = _amt(t)
        if t.get("to") == "card":
            # Банк получателя — bank_to (и для «Наличные → Карта»); bank — отправитель, у наличных пуст.
            # До 27.11.2026 здесь брался bank: перевод 26.11 на 40 827 ₽ не попадал в «Тиньков».
            dest = t.get("bank_to") or (t.get("bank") if t.get("from") != "card" else None)
            if dest == bank_key:
                incoming += amount
        if t.get("from") == "card" and t.get("bank") == bank_key:
            outgoing += amount
    return incoming - outgoing


def build_snapshot(data: dict, user_id: int, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(MOSCOW_TZ)
    txs = _for_user(data.get("transactions") or [], user_id)
    reqs = _for_user(data.get("requests") or [], user_id)
    trs = _for_user(data.get("transfers") or [], user_id)

    def totals(account: str, bank: str | None = None) -> tuple[Decimal, Decimal]:
        inc = Decimal("0")
        exp = Decimal("0")
        for t in txs:
            if t.get("account") != account:
                continue
            if bank is not None and t.get("bank") != bank:
                continue
            amount = _amt(t)
            if t.get("type") == "income":
                inc += amount
            elif t.get("type") == "expense":
                exp += amount
        return inc, exp

    cash_inc, cash_exp = totals("cash")
    card_inc, card_exp = totals("card")
    cash_tr_in = sum((_amt(t) for t in trs if t.get("to") == "cash"), Decimal("0"))
    cash_tr_out = sum((_amt(t) for t in trs if t.get("from") == "cash"), Decimal("0"))
    card_tr_in = sum((_amt(t) for t in trs if t.get("to") == "card"), Decimal("0"))
    card_tr_out = sum((_amt(t) for t in trs if t.get("from") == "card"), Decimal("0"))
    cash_tr_net = cash_tr_in - cash_tr_out
    card_tr_net = card_tr_in - card_tr_out
    cash_bal = cash_inc - cash_exp + cash_tr_net
    card_bal = card_inc - card_exp + card_tr_net

    banks = []
    for key, label in BANK_ORDER:
        b_inc, b_exp = totals("card", bank=key)
        b_tr = _bank_tr_net(trs, key)
        if b_inc == 0 and b_exp == 0 and b_tr == 0:
            continue
        banks.append({
            "key": key,
            "label": label,
            "inc": b_inc,
            "exp": b_exp,
            "tr_net": b_tr,
            "bal": b_inc - b_exp + b_tr,
        })

    cash_sales = Decimal("0")
    card_in = Decimal("0")
    exp_total = Decimal("0")
    by_cat: dict[str, Decimal] = {cat: Decimal("0") for cat in m.EXPENSE_CATEGORIES}
    extra_cats: dict[str, Decimal] = {}
    income_sum: dict[tuple[str, str], Decimal] = {}
    expense_sum: dict[tuple[str, str], Decimal] = {}
    income_lines: list[dict] = []
    expense_lines: list[dict] = []
    for t in txs:
        amount = _amt(t)
        if t.get("type") == "income":
            source = m.income_source_for_sheet(t.get("account") or "card", t.get("category"))
            account = t.get("account") or "card"
            income_sum[(source, account)] = income_sum.get((source, account), Decimal("0")) + amount
            income_lines.append(t)
            if account == "cash" and source == m.SRC_SALE:
                cash_sales += amount
            if account == "card" and source != m.SRC_OPENING:
                card_in += amount
        elif t.get("type") == "expense":
            cat = m.category_for_sheet(t.get("category")) or t.get("category") or m.CAT_OTHER_EXP
            note = (t.get("note") or "").strip()
            account = t.get("account") or "card"
            exp_total += amount
            if cat in by_cat:
                by_cat[cat] += amount
            else:
                extra_cats[cat] = extra_cats.get(cat, Decimal("0")) + amount
            key = (cat, note, account)
            expense_sum[key] = expense_sum.get(key, Decimal("0")) + amount
            expense_lines.append(t)

    exp_rows = [(cat, by_cat[cat]) for cat in m.EXPENSE_CATEGORIES if by_cat[cat] != 0]
    exp_rows += sorted(extra_cats.items(), key=lambda item: -item[1])
    cat_order = {cat: i for i, cat in enumerate(m.EXPENSE_CATEGORIES)}
    expense_detail = sorted(
        ((cat, note, account, amount) for (cat, note, account), amount in expense_sum.items()),
        key=lambda item: (cat_order.get(item[0], 99), 0 if item[2] == "card" else 1, -item[3], item[1]),
    )
    income_order = {src: i for i, src in enumerate(m.INCOME_SOURCES_ALL)}
    income_rows = sorted(
        ((source, account, amount) for (source, account), amount in income_sum.items()),
        key=lambda item: (income_order.get(item[0], 99), 0 if item[1] == "card" else 1, -item[2]),
    )
    income_lines.sort(key=lambda rec: rec.get("date") or "")
    expense_lines.sort(key=lambda rec: rec.get("date") or "")

    req_total = sum((_amt(r) for r in reqs), Decimal("0"))
    events = []
    for t in txs:
        events.append(("tx", t.get("date") or "", t))
    for t in trs:
        events.append(("tr", t.get("date") or "", t))
    for r in reqs:
        events.append(("req", r.get("date") or "", r))
    events.sort(key=lambda item: item[1], reverse=True)

    stamps = [t.get("date") or "" for t in txs] + [r.get("date") or "" for r in reqs] + [t.get("date") or "" for t in trs]
    return {
        "now": now,
        "month_label": f"{_RU_MONTHS[now.month - 1]} {now.year}",
        "period_label": _period_label(stamps),
        "empty": not txs and not reqs and not trs,
        "cash_inc": cash_inc,
        "cash_exp": cash_exp,
        "cash_tr_net": cash_tr_net,
        "cash_bal": cash_bal,
        "card_inc": card_inc,
        "card_exp": card_exp,
        "card_tr_net": card_tr_net,
        "card_bal": card_bal,
        "banks": banks,
        "total": cash_bal + card_bal,
        "req_total": req_total,
        "req_got": card_inc,
        "req_remaining": req_total - card_inc,
        "has_requests": bool(reqs),
        "transfers": trs,
        "cash_sales": cash_sales,
        "cash_sales_month": cash_sales,
        "card_in": card_in,
        "card_in_month": card_in,
        "exp_total": exp_total,
        "exp_month": exp_total,
        "exp_rows": exp_rows,
        "income_rows": income_rows,
        "expense_detail": expense_detail,
        "income_lines": income_lines,
        "expense_lines": expense_lines,
        "events": events[:20],
    }


def welcome_html() -> str:
    return (
        "<h3>Бюджет ИП</h3>"
        "<p>Подотчёт: наличные и карта. Запись идёт в журнал и в Google-таблицу.</p>"
        "<p>💵 Наличные и 💳 Карта — новая операция.<br>"
        "💰 Баланс — сколько денег сейчас.<br>"
        "📒 Отчёт — цифры для бухгалтера.</p>"
    )


def empty_html(title: str, hint: str) -> str:
    return f"<h3>{esc(title)}</h3><p>{esc(hint)}</p>"


def balance_html(snap: dict) -> str:
    if snap["empty"]:
        return empty_html("💰 Баланс", "Операций пока нет. Запиши поступление или списание.")
    rows = [["Наличные", money_cell(snap["cash_bal"])]]
    for bank in snap["banks"]:
        rows.append([bank["label"], money_cell(bank["bal"])])
    if len(snap["banks"]) != 1:
        rows.append(["<b>Карта</b>", f"<b>{money_cell(snap['card_bal'])}</b>"])
    rows.append(["<b>Всего</b>", f"<b>{money_cell(snap['total'])}</b>"])
    body = [
        f"<h3>💰 {esc(money(snap['total']))} ₽</h3>",
        table(["Счёт", "Сейчас"], rows),
    ]
    if snap["has_requests"]:
        body.append(
            table(
                ["Запросы", "Сумма"],
                [
                    ["Запрошено", money_cell(snap["req_total"])],
                    ["Пришло на карту", money_cell(snap["req_got"])],
                    ["Осталось", money_cell(snap["req_remaining"], signed=True)],
                ],
            )
        )
    return "".join(body)


def _income_line_row(rec: dict) -> list[str]:
    source = m.income_source_for_sheet(rec.get("account") or "card", rec.get("category"))
    where = _account_label(rec.get("account"))
    if rec.get("bank"):
        where = f"{where} · {m.bank_label(rec.get('bank'))}"
    return [esc(_short_date(rec.get("date"))), esc(f"{source} · {where}"), money_cell(_amt(rec))]


def _expense_line_row(rec: dict) -> list[str]:
    cat = m.category_for_sheet(rec.get("category")) or rec.get("category") or "Списание"
    note = (rec.get("note") or "").strip()
    what = f"{cat} · {note}" if note else cat
    if len(what) > 40:
        what = what[:39] + "…"
    where = "Нал" if rec.get("account") == "cash" else "Карта"
    return [esc(_short_date(rec.get("date"))), esc(f"{where} · {what}"), money_cell(_amt(rec))]


def _transfer_line_row(rec: dict) -> list[str]:
    label = m.direction_label(rec.get("from") or "", rec.get("to") or "")
    return [esc(_short_date(rec.get("date"))), esc(label), money_cell(_amt(rec))]


def report_html(snap: dict) -> str:
    if snap["empty"]:
        return empty_html("📒 Отчёт", "Операций пока нет — бухгалтеру пока нечего отдавать.")
    title = snap["period_label"] or snap["month_label"]
    summary = [
        [
            "Наличные",
            money_cell(snap["cash_inc"]),
            money_cell(snap["cash_exp"]),
            money_cell(snap["cash_tr_net"], signed=True),
            money_cell(snap["cash_bal"]),
        ],
        [
            "Карта",
            money_cell(snap["card_inc"]),
            money_cell(snap["card_exp"]),
            money_cell(snap["card_tr_net"], signed=True),
            money_cell(snap["card_bal"]),
        ],
        [
            "<b>Всего</b>",
            f"<b>{money_cell(snap['cash_inc'] + snap['card_inc'])}</b>",
            f"<b>{money_cell(snap['exp_total'])}</b>",
            "",
            f"<b>{money_cell(snap['total'])}</b>",
        ],
    ]
    body = [
        f"<h3>📒 Отчёт · {esc(title)}</h3>",
        table(
            ["Счёт", "Поступления", "Списания", "Переводы", "Баланс"],
            summary,
            align=["l", "r", "r", "r", "r"],
        ),
    ]
    if snap["has_requests"]:
        body.append(table(
            ["Запросы", "Сумма"],
            [
                ["Запрошено", money_cell(snap["req_total"])],
                ["Пришло на карту", money_cell(snap["req_got"])],
                ["Осталось", money_cell(snap["req_remaining"], signed=True)],
            ],
        ))
    if snap["income_rows"]:
        income_tbl = [
            [esc(source), esc(_account_label(account)), money_cell(amount)]
            for source, account, amount in snap["income_rows"]
        ]
        body.append("<h4>Откуда пришло</h4>")
        body.append(table(["Источник", "Счёт", "Сумма"], income_tbl, align=["l", "l", "r"]))
    if snap["expense_detail"]:
        dest_tbl = []
        for cat, note, account, amount in snap["expense_detail"]:
            dest_tbl.append([
                esc(cat),
                esc(note) if note else "",
                esc(_account_label(account)),
                money_cell(amount),
            ])
        body.append("<h4>Куда ушло</h4>")
        body.append(table(["Статья", "Деталь", "Счёт", "Сумма"], dest_tbl, align=["l", "l", "l", "r"]))
    elif snap["exp_rows"]:
        body.append("<h4>Куда ушло</h4>")
        body.append(table(
            ["Статья", "Сумма"],
            [[esc(name), money_cell(amount)] for name, amount in snap["exp_rows"]],
        ))
    if snap["income_lines"]:
        body.append(
            "<details><summary>Все поступления</summary>"
            + table(
                ["Дата", "Откуда", "Сумма"],
                [_income_line_row(rec) for rec in snap["income_lines"]],
                align=["l", "l", "r"],
            )
            + "</details>"
        )
    if snap["expense_lines"]:
        body.append(
            "<details><summary>Все списания</summary>"
            + table(
                ["Дата", "Куда", "Сумма"],
                [_expense_line_row(rec) for rec in snap["expense_lines"]],
                align=["l", "l", "r"],
            )
            + "</details>"
        )
    if snap["transfers"]:
        body.append(
            "<details><summary>Переводы</summary>"
            + table(
                ["Дата", "Направление", "Сумма"],
                [_transfer_line_row(rec) for rec in snap["transfers"]],
                align=["l", "l", "r"],
            )
            + "</details>"
        )
    html = "".join(body)
    if len(html) <= 30000:
        return html
    compact = [part for part in body if not str(part).startswith("<details")]
    html = "".join(compact)
    if len(html) <= 30000:
        return html
    return html[:29900] + "…"


def _event_row(kind: str, rec: dict) -> list[str]:
    when = esc(_short_date(rec.get("date")))
    amount = _amt(rec)
    if kind == "req":
        return [when, "Запрос", money_cell(amount, signed=True)]
    if kind == "tr":
        label = m.direction_label(rec.get("from") or "", rec.get("to") or "")
        signed = amount if rec.get("to") == "cash" else -amount if rec.get("from") == "cash" else amount
        if rec.get("from") == "card" and rec.get("to") == "card":
            signed = amount
        return [when, esc(f"Перевод · {label}"), money_cell(signed, signed=True)]
    account = "Нал" if rec.get("account") == "cash" else "Карта"
    if rec.get("type") == "income":
        source = m.income_source_for_sheet(rec.get("account") or "card", rec.get("category"))
        what = source
        signed = amount
    else:
        what = m.category_for_sheet(rec.get("category")) or rec.get("category") or "Списание"
        note = (rec.get("note") or "").strip()
        if what == m.CAT_ZP and note:
            name = note.split(" · ", 1)[0].strip()
            what = f"ЗП · {name}" if name else "ЗП упаковщиков"
        elif note:
            what = f"{what} · {note}"
        signed = -amount
    if len(what) > 36:
        what = what[:35] + "…"
    return [when, esc(f"{account} · {what}"), money_cell(signed, signed=True)]


def history_html(snap: dict) -> str:
    if not snap["events"]:
        return empty_html("🕓 История", "Операций пока нет.")
    rows = [_event_row(kind, rec) for kind, _stamp, rec in snap["events"]]
    return f"<h3>🕓 История</h3>" + table(["Дата", "Операция", "Сумма"], rows, align=["l", "l", "r"])


def saved_html(title: str, lines: list[str], amount, snap: dict) -> str:
    meta = "<br>".join(esc(line) for line in lines if line)
    rows = [
        ["Наличные", money_cell(snap["cash_bal"])],
        ["Карта", money_cell(snap["card_bal"])],
        ["<b>Всего</b>", f"<b>{money_cell(snap['total'])}</b>"],
    ]
    return (
        f"<h3>{esc(title)}</h3>"
        + (f"<p>{meta}</p>" if meta else "")
        + f"<p><b>{esc(money(amount))} ₽</b></p>"
        + table(["Сейчас", "Сумма"], rows)
    )
