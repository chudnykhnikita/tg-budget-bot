"""Close an open journal after the accountant has accepted the Excel."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import budget_cards
import budget_map as m
from tg_rich import esc, table

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), ROUND_HALF_UP)


def _alive(rec: dict) -> bool:
    return rec.get("status") != m.STATUS_DELETED


def _is_carry(rec: dict) -> bool:
    return m.is_period_carry(rec)


def _opening_tx(user_id: int, stamp: str, account: str, amount: Decimal, bank: str | None, *, debt: bool) -> dict:
    if debt:
        t_type, category = "expense", m.CAT_PRIOR_DEBT
    else:
        t_type, category = "income", m.SRC_OPENING
    return {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": t_type,
        "account": account,
        "amount": str(_q(abs(amount))),
        "category": category,
        "note": None,
        "bank": bank,
        "date": stamp,
        "status": m.STATUS_ACTIVE,
    }


@dataclass
class ClosePlan:
    ok: bool
    reason: str = ""
    period_label: str = ""
    cash_bal: Decimal = Decimal("0")
    card_bal: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    archived_ids: set[str] = field(default_factory=set)
    period: dict | None = None
    openings: list[dict] = field(default_factory=list)
    req_remaining: Decimal = Decimal("0")


def prepare_close(data: dict, user_id: int, *, now: datetime | None = None) -> ClosePlan:
    now = now or datetime.now(MOSCOW_TZ)
    snap = budget_cards.build_snapshot(data, user_id, now=now)
    txs = [t for t in (data.get("transactions") or []) if t.get("user_id") == user_id]
    reqs = [r for r in (data.get("requests") or []) if r.get("user_id") == user_id]
    trs = [t for t in (data.get("transfers") or []) if t.get("user_id") == user_id]
    live_txs = [t for t in txs if _alive(t)]
    live_reqs = [r for r in reqs if _alive(r)]
    live_trs = [t for t in trs if _alive(t)]
    activity = live_reqs or live_trs or any(not _is_carry(t) for t in live_txs)
    if not activity:
        return ClosePlan(ok=False, reason="После прошлого закрытия новых операций нет — закрывать нечего.")

    stamp = now.strftime("%Y-%m-%d %H:%M")
    openings: list[dict] = []
    if snap["cash_bal"] != 0:
        openings.append(_opening_tx(
            user_id, stamp, "cash", snap["cash_bal"], None, debt=snap["cash_bal"] < 0,
        ))
    assigned = Decimal("0")
    for bank in snap["banks"]:
        if bank["bal"] == 0:
            continue
        openings.append(_opening_tx(
            user_id, stamp, "card", bank["bal"], bank["key"], debt=bank["bal"] < 0,
        ))
        assigned += bank["bal"]
    rest = snap["card_bal"] - assigned
    if rest != 0:
        openings.append(_opening_tx(
            user_id, stamp, "card", rest, m.BANK_TINKOFF, debt=rest < 0,
        ))

    archived_ids = {rec["id"] for rec in txs + reqs + trs if rec.get("id")}
    period = {
        "id": str(uuid.uuid4()),
        "closed_at": stamp,
        "label": snap["period_label"] or snap["month_label"],
        "transactions": list(txs),
        "requests": list(reqs),
        "transfers": list(trs),
    }
    return ClosePlan(
        ok=True,
        period_label=period["label"],
        cash_bal=snap["cash_bal"],
        card_bal=snap["card_bal"],
        total=snap["total"],
        archived_ids=archived_ids,
        period=period,
        openings=openings,
        req_remaining=snap["req_remaining"],
    )


def apply_close(data: dict, plan: ClosePlan) -> None:
    if not plan.ok or plan.period is None:
        return
    data.setdefault("periods", [])
    if any(p.get("id") == plan.period["id"] for p in data["periods"]):
        return
    data["periods"].append(plan.period)
    data["transactions"] = [
        t for t in (data.get("transactions") or []) if t.get("id") not in plan.archived_ids
    ] + list(plan.openings)
    data["requests"] = [
        r for r in (data.get("requests") or []) if r.get("id") not in plan.archived_ids
    ]
    data["transfers"] = [
        t for t in (data.get("transfers") or []) if t.get("id") not in plan.archived_ids
    ]


def preview_html(plan: ClosePlan) -> str:
    rows = [
        ["Период", esc(plan.period_label)],
        ["Наличные остаток", budget_cards.money_cell(plan.cash_bal)],
        ["Карта остаток", budget_cards.money_cell(plan.card_bal)],
        ["<b>Всего перейдёт</b>", f"<b>{budget_cards.money_cell(plan.total)}</b>"],
    ]
    body = [
        "<h3>🔒 Закрыть период</h3>",
        "<p>После того как бухгалтер свела Excel. Старые поступления и списания уйдут в архив и не попадут в следующий отчёт.</p>",
        table(["", ""], rows, align=["l", "r"]),
    ]
    if plan.req_remaining != 0:
        body.append(
            f"<p>Незакрытый запрос {esc(budget_cards.money(plan.req_remaining))} ₽ в новый период не перейдёт. "
            "Если перевод ещё ждёшь — запроси его заново.</p>"
        )
    return "".join(body)
