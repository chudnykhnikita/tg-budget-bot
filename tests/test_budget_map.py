import unittest

import budget_map as m


class BudgetMapTests(unittest.TestCase):
    def test_card_income_without_category_is_trade_plus_invoice(self):
        sheet, row = m.transaction_to_row({
            "id": "a1",
            "type": "income",
            "account": "card",
            "amount": "150000.00",
            "category": None,
            "note": None,
            "bank": "tinkoff",
            "date": "2026-06-17 13:22",
        })
        self.assertEqual(sheet, m.SHEET_OPS_CARD_IN)
        self.assertEqual(row[0], "2026-06-17")
        self.assertEqual(row[1], 150000.0)
        self.assertEqual(row[2], "Тиньков")
        self.assertEqual(row[3], m.SRC_TRADE)
        self.assertEqual(row[5], "a1")
        self.assertEqual(row[6], m.STATUS_ACTIVE)

    def test_cash_sale_maps_to_product_sale_source(self):
        sheet, row = m.transaction_to_row({
            "id": "b1",
            "type": "income",
            "account": "cash",
            "amount": "2500",
            "category": "🛒 Продажа со склада",
            "date": "2026-06-23 11:31",
        })
        self.assertEqual(sheet, m.SHEET_OPS_CASH_IN)
        self.assertEqual(row[3], m.SRC_SALE)

    def test_opening_balance_is_not_revenue(self):
        _, row = m.transaction_to_row({
            "id": "c1",
            "type": "income",
            "account": "cash",
            "amount": "44000",
            "category": "остаток",
            "date": "2026-06-17 12:20",
        })
        self.assertEqual(row[3], m.SRC_OPENING)

    def test_expense_keeps_subcategory_in_note_column(self):
        sheet, row = m.transaction_to_row({
            "id": "d1",
            "type": "expense",
            "account": "card",
            "amount": "100.5",
            "category": "🏦 Плата банку",
            "note": "💸 Комиссия",
            "bank": "tinkoff",
            "date": "2026-07-01 10:00",
        })
        self.assertEqual(sheet, m.SHEET_OPS_CARD_OUT)
        self.assertEqual(row[2], "Плата банку")
        self.assertEqual(row[3], "Комиссия")
        self.assertIsNone(row[5])  # формула баланса, не значение

    def test_request_without_bank_is_without_invoice(self):
        row = m.request_to_row({
            "id": "r1",
            "amount": "600000",
            "bank": None,
            "date": "2026-06-19 16:56",
        })
        self.assertEqual(row[2], m.INV_WITHOUT)

    def test_request_with_bank_defaults_to_invoice(self):
        row = m.request_to_row({
            "id": "r2",
            "amount": "170468",
            "bank": "tinkoff",
            "date": "2026-06-17 12:21",
        })
        self.assertEqual(row[2], m.INV_WITH)
        self.assertEqual(row[3], "Тиньков")

    def test_transfer_roundtrip(self):
        rec = {
            "id": "t1",
            "amount": "16000.00",
            "from": "card",
            "to": "cash",
            "bank": "tinkoff",
            "bank_to": None,
            "date": "2026-07-01 16:47",
        }
        row = m.transfer_to_row(rec)
        back = m.row_to_transfer(row)
        self.assertEqual(back["from"], "card")
        self.assertEqual(back["to"], "cash")
        self.assertEqual(back["amount"], "16000.00")
        self.assertEqual(back["bank"], "tinkoff")
        self.assertIsNone(back["bank_to"])

    def test_cash_to_card_writes_destination_bank(self):
        rec = {
            "id": "t2",
            "amount": "142205.00",
            "from": "cash",
            "to": "card",
            "bank": "tinkoff",
            "bank_to": None,
            "date": "2026-10-24 12:00",
        }
        rec = m.normalize_transfer(rec)
        self.assertIsNone(rec["bank"])
        self.assertEqual(rec["bank_to"], "tinkoff")
        row = m.transfer_to_row(rec)
        self.assertEqual(row[2], "Наличные → Карта")
        self.assertEqual(row[3], "")
        self.assertEqual(row[4], "Тиньков")
        back = m.row_to_transfer(row)
        self.assertEqual(back["from"], "cash")
        self.assertEqual(back["to"], "card")
        self.assertEqual(back["bank_to"], "tinkoff")
        self.assertIsNone(back["bank"])

    def test_deleted_row_is_skipped(self):
        row = ["2026-07-01", 100, "Тиньков", m.SRC_TRADE, "", "id", m.STATUS_DELETED]
        self.assertIsNone(m.row_to_transaction(m.SHEET_OPS_CARD_IN, row))

    def test_china_delivery_is_own_category(self):
        self.assertEqual(m.category_for_sheet("Доставка из Китая"), m.CAT_DELIVERY_CN)
        self.assertEqual(m.category_for_sheet("доставка из китая"), m.CAT_DELIVERY_CN)
        self.assertIn(m.CAT_DELIVERY_CN, m.EXPENSE_CATEGORIES)
        self.assertNotIn("Китай", m.EXPENSE_SUBCATEGORIES.get(m.CAT_DELIVERY, []))

    def test_refund_15_percent_is_own_category(self):
        self.assertEqual(m.category_for_sheet("Возврат 15% за кофемашину"), m.CAT_REFUND_15)
        self.assertEqual(m.category_for_sheet("Компенсация 15% за шуруповерт"), m.CAT_REFUND_15)
        self.assertEqual(
            m.category_for_sheet("Прочие расходы - 15% от цены смартфона"),
            m.CAT_REFUND_15,
        )
        self.assertIn(m.CAT_REFUND_15, m.EXPENSE_CATEGORIES)
        self.assertNotIn(m.CAT_REFUND_15, m.EXPENSE_SUBCATEGORIES.get(m.CAT_OTHER_EXP, []))

    def test_expense_followup_routes(self):
        self.assertEqual(m.expense_followup(m.CAT_ZP), m.FOLLOWUP_DATE)
        self.assertEqual(m.expense_followup(m.CAT_ZP_WORKERS), m.FOLLOWUP_SUB)
        self.assertEqual(m.expense_followup(m.CAT_OFC), m.FOLLOWUP_SUB)
        self.assertEqual(m.expense_followup(m.CAT_REFUND_15), m.FOLLOWUP_NOTE_REQUIRED)
        self.assertEqual(m.expense_followup(m.CAT_OTHER_EXP), m.FOLLOWUP_NOTE_REQUIRED)
        self.assertEqual(m.expense_followup(m.CAT_DELIVERY_CN), m.FOLLOWUP_NOTE)
        self.assertEqual(m.expense_followup(m.CAT_MEAL), m.FOLLOWUP_DONE)

    def test_income_other_requires_note(self):
        self.assertEqual(m.income_followup(m.SRC_TRADE), m.FOLLOWUP_DONE)
        self.assertEqual(m.income_followup(m.SRC_OTHER), m.FOLLOWUP_NOTE_REQUIRED)

    def test_november_income_labels(self):
        self.assertEqual(m.income_source_for_sheet("cash", "от Алексея"), m.SRC_ALEXEY)
        self.assertEqual(m.income_source_for_sheet("cash", "от Алексея Маркова"), m.SRC_ALEXEY)
        self.assertEqual(
            m.income_source_for_sheet("cash", "прочие доходы - отдал оптовый клиент из Темрюка"),
            m.SRC_OTHER,
        )
        self.assertEqual(
            m.income_source_for_sheet("cash", "остаток с прошлого периода"),
            m.SRC_OPENING,
        )

    def test_november_expense_labels(self):
        self.assertEqual(
            m.expense_from_label("ЗП Михаила 2 часть за октябрь", None),
            (m.CAT_ZP_WORKERS, "Михаил 2 часть"),
        )
        self.assertEqual(m.expense_from_label("Дивиденды Алексей", None), (m.CAT_DIV, "Алексей"))
        self.assertEqual(
            m.expense_from_label("обеды с 2.11 по 15.11", None),
            (m.CAT_MEAL, "обеды с 2.11 по 15.11"),
        )
        self.assertEqual(
            m.expense_from_label("Сергею за поддержание работы бота", None),
            (m.CAT_CONTRACTORS, "Бот (Сергей)"),
        )
        self.assertEqual(
            m.expense_from_label("Доставка Байкал сервис", None),
            (m.CAT_DELIVERY, "Байкал сервис"),
        )

    def test_ledger_row_splits_income_and_expense(self):
        income = m.ledger_row_from_tx({
            "id": "i1",
            "type": "income",
            "account": "cash",
            "amount": "19000.00",
            "category": "остаток",
            "note": None,
            "date": "2026-10-22 11:58",
        })
        self.assertEqual(income[0], "2026-10-22")
        self.assertEqual(income[1], 19000.0)
        self.assertEqual(income[2], "")
        self.assertEqual(income[3], "Наличные")
        self.assertEqual(income[4], 19000.0)
        self.assertEqual(income[5], m.SRC_OPENING)
        expense = m.ledger_row_from_tx({
            "id": "e1",
            "type": "expense",
            "account": "card",
            "amount": "68.00",
            "category": m.CAT_BANK,
            "note": "Комиссия",
            "bank": "tinkoff",
            "date": "2026-10-27 10:00",
        })
        self.assertEqual(expense[1], "")
        self.assertEqual(expense[2], 68.0)
        self.assertEqual(expense[3], "Карта · Тиньков")
        self.assertEqual(expense[6], "Комиссия")
        tr = m.ledger_row_from_transfer({
            "id": "tr",
            "amount": "150000.00",
            "from": "cash",
            "to": "card",
            "date": "2026-11-20 12:30",
        })
        self.assertEqual(tr[1], "")
        self.assertEqual(tr[2], "")
        self.assertEqual(tr[3], "Наличные → Карта")
        self.assertEqual(tr[5], "Перевод")

    def test_period_carry_detection(self):
        self.assertTrue(m.is_period_carry({
            "type": "income", "account": "cash", "category": m.SRC_OPENING,
        }))
        self.assertTrue(m.is_period_carry({
            "type": "expense", "account": "card", "category": m.CAT_PRIOR_DEBT,
        }))
        self.assertFalse(m.is_period_carry({
            "type": "expense", "account": "card", "category": m.CAT_BANK,
        }))

    def test_payroll_row_becomes_card_expense(self):
        row = [
            "23.11.2026", "5\xa0000 ₽", "ЗП упаковщиков",
            "Ярослав Фурсов · 23.11.2026", "Тиньков", "5\xa0000 ₽",
            "zp:e272ef4ec826335c", "Активна", "236305027",
        ]
        rec = m.payroll_tx_from_expense_row(m.SHEET_OPS_CARD_OUT, row)
        self.assertEqual(rec["id"], "zp:e272ef4ec826335c")
        self.assertEqual(rec["amount"], "5000.00")
        self.assertEqual(rec["account"], "card")
        self.assertEqual(rec["bank"], "tinkoff")
        self.assertEqual(rec["category"], "ЗП упаковщиков")
        self.assertEqual(rec["user_id"], 236305027)
        self.assertEqual(rec["date"], "2026-11-23 12:00")
        self.assertIsNone(m.payroll_tx_from_expense_row(m.SHEET_OPS_CARD_OUT, ["2026-11-23", 10, "Прочее", "", "Тиньков", "", "uuid", "Активна", 1]))

    def test_merge_payroll_adds_missing_and_voids(self):
        fresh = m.payroll_tx_from_expense_row(m.SHEET_OPS_CARD_OUT, [
            "23.11.2026", "5000", "ЗП упаковщиков", "Ярослав", "Тиньков", "",
            "zp:new", "Активна", "7",
        ])
        gone = dict(fresh)
        gone["id"] = "zp:gone"
        gone["status"] = m.STATUS_DELETED
        archived = dict(fresh)
        archived["id"] = "zp:old"
        data = {
            "transactions": [{
                "id": "zp:live",
                "status": m.STATUS_ACTIVE,
                "amount": "100.00",
            }],
            "periods": [{"transactions": [{"id": "zp:old"}]}],
        }
        live_void = dict(fresh)
        live_void["id"] = "zp:live"
        live_void["status"] = m.STATUS_DELETED
        self.assertTrue(m.merge_payroll(data, [fresh, gone, archived, live_void]))
        ids = [t["id"] for t in data["transactions"]]
        self.assertEqual(ids, ["zp:live", "zp:new"])
        self.assertEqual(data["transactions"][0]["status"], m.STATUS_DELETED)
        self.assertFalse(m.merge_payroll(data, [fresh, gone, archived, live_void]))

    def test_ledger_titles(self):
        self.assertEqual(m.ledger_title(1), "Журнал")
        self.assertEqual(m.ledger_title(2), "Журнал 2")
        self.assertEqual(m.ledger_index("Журнал"), 1)
        self.assertEqual(m.ledger_index("Журнал 3"), 3)
        self.assertIsNone(m.ledger_index("Переводы"))


if __name__ == "__main__":
    unittest.main()
