import copy
import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import budget_cards as cards
import budget_map as m
import budget_period as period

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=TZ)


def _tx(**kwargs):
    rec = {
        "id": kwargs.get("id", "t"),
        "user_id": 1,
        "type": "expense",
        "account": "card",
        "amount": "100.00",
        "category": m.CAT_BANK,
        "note": None,
        "bank": "tinkoff",
        "date": "2026-11-10 10:00",
        "status": m.STATUS_ACTIVE,
    }
    rec.update(kwargs)
    return rec


class PeriodCloseTests(unittest.TestCase):
    def _open_journal(self):
        return {
            "transactions": [
                _tx(
                    id="open",
                    type="income",
                    account="cash",
                    amount="19000.00",
                    category="остаток",
                    bank=None,
                    date="2026-10-22 12:00",
                ),
                _tx(
                    id="sale",
                    type="income",
                    account="cash",
                    amount="5000.00",
                    category=m.CAT_SALE_BOT,
                    bank=None,
                    date="2026-11-05 11:00",
                ),
                _tx(
                    id="card-in",
                    type="income",
                    account="card",
                    amount="10000.00",
                    category=m.SRC_TRADE,
                    date="2026-11-05 12:00",
                ),
                _tx(
                    id="exp",
                    type="expense",
                    account="cash",
                    amount="3000.00",
                    category=m.CAT_MEAL,
                    bank=None,
                    date="2026-11-06 12:00",
                ),
                _tx(
                    id="card-exp",
                    amount="2000.00",
                    category=m.CAT_BANK,
                    note="Комиссия",
                    date="2026-11-06 13:00",
                ),
            ],
            "requests": [],
            "transfers": [
                {
                    "id": "tr1",
                    "user_id": 1,
                    "amount": "4000.00",
                    "from": "cash",
                    "to": "card",
                    "bank": "tinkoff",
                    "bank_to": None,
                    "date": "2026-11-07 10:00",
                    "status": m.STATUS_ACTIVE,
                }
            ],
            "periods": [],
        }

    def test_close_archives_ops_and_keeps_remainder(self):
        data = self._open_journal()
        before = cards.build_snapshot(data, 1, now=NOW)
        plan = period.prepare_close(data, 1, now=NOW)
        self.assertTrue(plan.ok)
        period.apply_close(data, plan)
        after = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(after["cash_bal"], before["cash_bal"])
        self.assertEqual(after["card_bal"], before["card_bal"])
        self.assertEqual(after["total"], before["total"])
        self.assertEqual(after["cash_sales"], Decimal("0"))
        self.assertEqual(after["card_in"], Decimal("0"))
        self.assertEqual(after["exp_total"], Decimal("0"))
        html = cards.report_html(after)
        self.assertIn("Стартовый остаток", html)
        self.assertNotIn("Комиссия", html)
        self.assertNotIn("Продажа товара", html)
        self.assertEqual(len(data["periods"]), 1)
        self.assertEqual(len(data["periods"][0]["transactions"]), 5)
        self.assertEqual(data["transfers"], [])

    def test_second_close_without_new_ops_is_refused(self):
        data = self._open_journal()
        plan = period.prepare_close(data, 1, now=NOW)
        period.apply_close(data, plan)
        again = period.prepare_close(data, 1, now=NOW)
        self.assertFalse(again.ok)

    def test_new_expense_after_close_does_not_bring_old_ops_back(self):
        data = self._open_journal()
        period.apply_close(data, period.prepare_close(data, 1, now=NOW))
        data["transactions"].append(_tx(
            id="new-exp",
            amount="80.00",
            category=m.CAT_OFC,
            note="Уборка",
            date="2026-12-01 10:00",
        ))
        html = cards.report_html(cards.build_snapshot(data, 1, now=NOW))
        self.assertIn("Уборка", html)
        self.assertNotIn("Комиссия", html)
        self.assertIn("Стартовый остаток", html)

    def test_negative_card_becomes_prior_debt(self):
        data = {
            "transactions": [
                _tx(id="debt-exp", amount="500.00", category=m.CAT_BANK, date="2026-11-01 10:00"),
            ],
            "requests": [],
            "transfers": [],
        }
        plan = period.prepare_close(data, 1, now=NOW)
        self.assertTrue(plan.ok)
        period.apply_close(data, plan)
        live = data["transactions"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["type"], "expense")
        self.assertEqual(live[0]["category"], m.CAT_PRIOR_DEBT)
        snap = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(snap["card_bal"], Decimal("-500.00"))

    def test_other_user_untouched(self):
        data = self._open_journal()
        other = _tx(id="other", user_id=2, amount="1.00")
        data["transactions"].append(other)
        period.apply_close(data, period.prepare_close(data, 1, now=NOW))
        self.assertTrue(any(t["id"] == "other" for t in data["transactions"]))

    def test_apply_close_is_idempotent_on_same_plan(self):
        data = self._open_journal()
        plan = period.prepare_close(data, 1, now=NOW)
        period.apply_close(data, plan)
        copy_after = copy.deepcopy(data)
        period.apply_close(data, plan)
        self.assertEqual(len(data["periods"]), 1)
        self.assertEqual(len(data["transactions"]), len(copy_after["transactions"]))
