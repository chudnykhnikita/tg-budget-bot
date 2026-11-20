import unittest
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import budget_cards as cards
import budget_map as m
from tg_rich import html_to_plain, table

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=TZ)


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


class BudgetCardsTests(unittest.TestCase):
    def test_opening_remainder_is_not_cash_revenue(self):
        data = {
            "transactions": [
                _tx(
                    id="open",
                    type="income",
                    account="cash",
                    amount="44000.00",
                    category="остаток",
                    bank=None,
                    date="2026-11-01 12:00",
                ),
                _tx(
                    id="sale",
                    type="income",
                    account="cash",
                    amount="2500.00",
                    category=m.CAT_SALE_BOT,
                    bank=None,
                    date="2026-11-05 11:00",
                ),
            ],
            "requests": [],
            "transfers": [],
        }
        snap = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(snap["cash_sales_month"], Decimal("2500.00"))
        self.assertEqual(snap["cash_bal"], Decimal("46500.00"))

    def test_card_opening_is_not_card_inflow(self):
        data = {
            "transactions": [
                _tx(
                    id="open",
                    type="income",
                    account="card",
                    amount="94.00",
                    category=m.SRC_OPENING,
                    date="2026-11-20 15:00",
                ),
                _tx(
                    id="trade",
                    type="income",
                    account="card",
                    amount="100000.00",
                    category=m.SRC_TRADE,
                    date="2026-11-21 10:00",
                ),
            ],
            "requests": [],
            "transfers": [],
        }
        snap = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(snap["card_in"], Decimal("100000.00"))
        self.assertEqual(snap["card_bal"], Decimal("100094.00"))

    def test_expenses_group_by_sheet_category_not_note(self):
        data = {
            "transactions": [
                _tx(id="a", amount="100.00", note="Комиссия"),
                _tx(id="b", amount="50.00", note="Обслуживание"),
                _tx(
                    id="c",
                    amount="200.00",
                    category=m.CAT_DELIVERY_CN,
                    note="партия 12",
                ),
            ],
            "requests": [],
            "transfers": [],
        }
        snap = cards.build_snapshot(data, 1, now=NOW)
        by_name = dict(snap["exp_rows"])
        self.assertEqual(by_name[m.CAT_BANK], Decimal("150.00"))
        self.assertEqual(by_name[m.CAT_DELIVERY_CN], Decimal("200.00"))
        self.assertEqual(snap["exp_total"], Decimal("350.00"))
        notes = {(cat, note, account): amount for cat, note, account, amount in snap["expense_detail"]}
        self.assertEqual(notes[(m.CAT_BANK, "Комиссия", "card")], Decimal("100.00"))
        self.assertEqual(notes[(m.CAT_BANK, "Обслуживание", "card")], Decimal("50.00"))

    def test_deleted_records_are_ignored(self):
        data = {
            "transactions": [
                _tx(id="live", amount="10.00", type="income", account="card"),
                _tx(
                    id="gone",
                    amount="999.00",
                    type="income",
                    account="card",
                    status=m.STATUS_DELETED,
                ),
            ],
            "requests": [],
            "transfers": [],
        }
        snap = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(snap["card_inc"], Decimal("10.00"))

    def test_balance_html_has_total_first_and_no_excel_hint(self):
        data = {
            "transactions": [
                _tx(type="income", account="cash", amount="22800", category=m.CAT_SALE_BOT, bank=None),
                _tx(type="income", account="card", amount="112506", category=m.SRC_TRADE),
            ],
            "requests": [],
            "transfers": [],
        }
        html = cards.balance_html(cards.build_snapshot(data, 1, now=NOW))
        self.assertIn("<h3>💰", html)
        self.assertIn("<table", html)
        self.assertNotIn("Excel", html)
        self.assertNotIn("xlsx", html.lower())

    def test_report_html_matches_accountant_labels(self):
        data = {
            "transactions": [
                _tx(
                    type="income",
                    account="cash",
                    amount="2500",
                    category=m.CAT_SALE_BOT,
                    bank=None,
                ),
                _tx(type="expense", amount="150", category=m.CAT_BANK, note="Комиссия"),
            ],
            "requests": [{"id": "r", "user_id": 1, "amount": "10000", "date": "2026-11-02 10:00"}],
            "transfers": [],
        }
        html = cards.report_html(cards.build_snapshot(data, 1, now=NOW))
        self.assertIn("Поступления", html)
        self.assertIn("Списания", html)
        self.assertIn("Переводы", html)
        self.assertIn("Осталось", html)
        self.assertIn("Откуда пришло", html)
        self.assertIn("Куда ушло", html)
        self.assertIn("Продажа товара", html)
        self.assertIn("Плата банку", html)
        self.assertIn("Комиссия", html)
        self.assertIn("2 500", html)

    def test_report_totals_match_accountant_cash_book(self):
        data = {
            "transactions": [
                _tx(id="open", type="income", account="cash", amount="19000", category="остаток", bank=None),
                _tx(id="alex", type="income", account="cash", amount="420000", category=m.SRC_ALEXEY, bank=None),
                _tx(id="other", type="income", account="cash", amount="123205", category=m.SRC_OTHER, note="Темрюк", bank=None),
                _tx(id="trade", type="income", account="card", amount="150000", category=m.SRC_TRADE),
                _tx(id="cash-exp", type="expense", account="cash", amount="162500", category=m.CAT_ZP_WORKERS, bank=None),
                _tx(id="card-exp", type="expense", account="card", amount="442111", category=m.CAT_ZP),
            ],
            "requests": [],
            "transfers": [
                {
                    "id": "tr",
                    "user_id": 1,
                    "amount": "292205.00",
                    "from": "cash",
                    "to": "card",
                    "bank": "tinkoff",
                    "date": "2026-11-10 10:00",
                    "status": m.STATUS_ACTIVE,
                }
            ],
        }
        snap = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(snap["cash_inc"], Decimal("562205.00"))
        self.assertEqual(snap["cash_exp"], Decimal("162500.00"))
        self.assertEqual(snap["cash_tr_net"], Decimal("-292205.00"))
        self.assertEqual(snap["cash_bal"], Decimal("107500.00"))
        self.assertEqual(snap["card_inc"], Decimal("150000.00"))
        self.assertEqual(snap["card_exp"], Decimal("442111.00"))
        self.assertEqual(snap["card_tr_net"], Decimal("292205.00"))
        self.assertEqual(snap["card_bal"], Decimal("94.00"))
        self.assertEqual(snap["total"], Decimal("107594.00"))
        html = cards.report_html(snap)
        self.assertIn("562 205", html)
        self.assertIn("107 500", html)
        self.assertIn("107 594", html)
        self.assertNotIn("Выручка наличными", html)

    def test_report_covers_open_period_not_calendar_month(self):
        data = {
            "transactions": [
                _tx(
                    date="2026-07-08 10:00",
                    amount="100",
                    type="expense",
                    note="Комиссия",
                ),
                _tx(
                    id="in",
                    date="2026-06-17 12:20",
                    type="income",
                    account="card",
                    amount="150000",
                    category=m.SRC_TRADE,
                ),
            ],
            "requests": [],
            "transfers": [],
        }
        html = cards.report_html(cards.build_snapshot(data, 1, now=NOW))
        self.assertIn("17.06–08.07.2026", html)
        self.assertIn(m.SRC_TRADE, html)
        self.assertIn("Комиссия", html)
        self.assertNotIn("За этот месяц записей нет", html)
        self.assertNotIn("Осталось по запросам", html)

    def test_opening_remainder_listed_as_source_not_revenue(self):
        data = {
            "transactions": [
                _tx(
                    type="income",
                    account="cash",
                    amount="44000",
                    category="остаток",
                    bank=None,
                    date="2026-06-17 12:20",
                ),
                _tx(
                    id="sale",
                    type="income",
                    account="cash",
                    amount="2500",
                    category=m.CAT_SALE_BOT,
                    bank=None,
                    date="2026-06-23 11:00",
                ),
            ],
            "requests": [],
            "transfers": [],
        }
        snap = cards.build_snapshot(data, 1, now=NOW)
        self.assertEqual(snap["cash_sales"], Decimal("2500.00"))
        html = cards.report_html(snap)
        self.assertIn(m.SRC_OPENING, html)
        self.assertIn("Продажа товара", html)

    def test_user_text_is_escaped(self):
        data = {
            "transactions": [
                _tx(category="<script>", note="a&b", amount="1"),
            ],
            "requests": [],
            "transfers": [],
        }
        html = cards.history_html(cards.build_snapshot(data, 1, now=NOW))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("a&amp;b", html)

    def test_html_to_plain_keeps_numbers(self):
        body = "<h3>💰 10 000 ₽</h3>" + table(["Счёт", "Сейчас"], [["Наличные", "1 000"]])
        plain = html_to_plain(body)
        self.assertIn("10 000", plain)
        self.assertIn("Наличные", plain)
        self.assertNotIn("<table", plain)


if __name__ == "__main__":
    unittest.main()
