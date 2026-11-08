# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.utils import convert_utc_to_system_timezone

from erpnext.erpnext_integrations.doctype.wise_settings.wise_settings import (
	add_bank_accounts,
	automatic_synchronization,
	enqueue_synchronization,
	sync_transactions,
	utc_now,
)
from erpnext.tests.utils import ERPNextTestSuite

WISE_SETTINGS = "erpnext.erpnext_integrations.doctype.wise_settings.wise_settings"

BALANCES = [{"id": 9001, "currency": "USD"}, {"id": 9002, "currency": "EUR"}]

BANK_DETAILS = {
	"USD": {
		"address": {"country": "United States"},
		"accountNumbers": [{"accountType": "Account number", "accountNumber": "8310000001"}],
		"bankCodes": [
			{"scheme": "Routing number", "value": "026000001"},
			{"scheme": "Swift/BIC", "value": "TESTUS33XXX"},
		],
		"deprecated": False,
	},
	"EUR": {
		"address": {"country": "Belgium"},
		"accountNumbers": [{"accountType": "IBAN", "accountNumber": "BE68 5390 0754 7034"}],
		"bankCodes": [{"scheme": "Swift/BIC", "value": "TESTBEB1XXX"}],
		"deprecated": False,
	},
}


def wise_transaction(reference, amount, fee=0.0, date="2026-11-02T22:15:00.000000Z", **details):
	return {
		"type": "CREDIT" if amount > 0 else "DEBIT",
		"date": date,
		"amount": {"value": amount, "currency": "USD"},
		"totalFees": {"value": fee, "currency": "USD"},
		"details": {"type": "DEPOSIT" if amount > 0 else "TRANSFER", **details},
		"referenceNumber": reference,
	}


class FakeWise:
	transactions: ClassVar[list] = []

	def __init__(self, settings=None):
		pass

	def balances(self, profile_id):
		return BALANCES

	def statement(self, profile_id, balance_id, currency, start, end):
		return {
			"bankDetails": [BANK_DETAILS[currency]],
			"transactions": self.transactions if currency == "USD" else [],
		}


class TestWiseSettings(ERPNextTestSuite):
	def setUp(self):
		settings = frappe.get_single("Wise Settings")
		settings.update(
			{"enabled": 1, "api_token": "test-token", "company": "_Test Company", "profile_id": "100"}
		)
		settings.save()
		patcher = patch(f"{WISE_SETTINGS}.WiseConnector", FakeWise)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_bank_accounts_from_wise_balances(self):
		usd, eur = add_bank_accounts()

		usd = frappe.get_doc("Bank Account", usd)
		self.assertEqual(usd.integration_id, "9001")
		self.assertEqual(usd.bank, "Wise United States")
		self.assertEqual(usd.bank_account_no, "8310000001")
		self.assertEqual(usd.branch_code, "026000001")
		self.assertTrue(usd.is_company_account)
		self.assertEqual(frappe.db.get_value("Account", usd.account, "account_currency"), "USD")
		self.assertEqual(frappe.db.get_value("Bank", usd.bank, "swift_number"), "TESTUS33XXX")

		eur = frappe.get_doc("Bank Account", eur)
		self.assertEqual(eur.iban, "BE68 5390 0754 7034")
		self.assertFalse(eur.bank_account_no)
		self.assertEqual(frappe.db.get_value("Bank", eur.bank, "swift_number"), "TESTBEB1XXX")

		# getting them again updates the same Bank Accounts
		self.assertEqual(add_bank_accounts(), [usd.name, eur.name])

	def test_links_a_bank_account_made_by_hand(self):
		usd = frappe.get_doc("Bank Account", add_bank_accounts()[0])
		usd.db_set("integration_id", None)

		self.assertEqual(add_bank_accounts()[0], usd.name)
		self.assertEqual(frappe.db.get_value("Bank Account", usd.name, "integration_id"), "9001")

	def test_sync_makes_bank_transactions(self):
		usd = add_bank_accounts()[0]
		FakeWise.transactions = [
			wise_transaction("TRANSFER-2", -25.0, fee=1.5, date="2026-11-03T01:00:00.000000Z"),
			wise_transaction(
				"DEPOSIT-1",
				93.89,
				fee=6.11,
				senderName="Jane Buyer",
				senderAccount="TESTUS33 1234",
				paymentReference="SAL-ORD-2026-00042",
				description="Received money from Jane Buyer",
			),
		]

		sync_transactions()

		deposit = frappe.get_doc("Bank Transaction", {"transaction_id": "DEPOSIT-1", "bank_account": usd})
		self.assertEqual(deposit.docstatus, 1)
		self.assertEqual(deposit.deposit, 93.89)
		self.assertEqual(deposit.included_fee, 6.11)
		self.assertEqual(deposit.currency, "USD")
		self.assertEqual(deposit.reference_number, "SAL-ORD-2026-00042")
		self.assertEqual(deposit.bank_party_name, "Jane Buyer")
		self.assertEqual(deposit.bank_party_account_number, "TESTUS33 1234")
		self.assertEqual(
			deposit.transaction_type,
			"Wire Transfer" if frappe.db.exists("Mode of Payment", "Wire Transfer") else None,
		)

		withdrawal = frappe.get_doc("Bank Transaction", {"transaction_id": "TRANSFER-2", "bank_account": usd})
		self.assertEqual(withdrawal.withdrawal, 25.0)
		self.assertEqual(withdrawal.included_fee, 1.5)
		self.assertEqual(withdrawal.reference_number, "TRANSFER-2")

		# read up to today, so the next sync asks only for what came after
		self.assertEqual(
			str(frappe.db.get_value("Bank Account", usd, "last_integration_date")),
			str(convert_utc_to_system_timezone(utc_now()).date()),
		)

		# a second sync imports nothing twice
		sync_transactions()
		self.assertEqual(frappe.db.count("Bank Transaction", {"bank_account": usd}), 2)

	def test_sync_skips_balances_without_a_bank_account(self):
		FakeWise.transactions = [wise_transaction("DEPOSIT-ORPHAN", 10.0)]
		sync_transactions()
		self.assertFalse(frappe.db.exists("Bank Transaction", {"transaction_id": "DEPOSIT-ORPHAN"}))

	def test_sync_needs_permission(self):
		with self.set_user("Guest"), self.assertRaises(frappe.PermissionError):
			enqueue_synchronization()

	def test_a_transaction_that_fails_is_read_again(self):
		usd = add_bank_accounts()[0]
		FakeWise.transactions = [
			wise_transaction("DEPOSIT-LATER", 5.0, date="2026-11-04T01:00:00.000000Z"),
			wise_transaction("DEPOSIT-BAD", 7.0, date="2026-11-02T01:00:00.000000Z"),
		]
		from erpnext.erpnext_integrations.doctype.wise_settings import wise_settings

		real = wise_settings.new_bank_transaction

		def failing(transaction, bank_account):
			if transaction["referenceNumber"] == "DEPOSIT-BAD":
				raise frappe.ValidationError("bad")
			return real(transaction, bank_account)

		with patch(f"{WISE_SETTINGS}.new_bank_transaction", side_effect=failing), patch("frappe.log_error"):
			sync_transactions()
		# the date stays at the one that failed, so the next sync reads it again
		self.assertEqual(
			str(frappe.db.get_value("Bank Account", usd, "last_integration_date")),
			str(wise_settings.transaction_date(FakeWise.transactions[1])),
		)
		sync_transactions()
		self.assertTrue(frappe.db.exists("Bank Transaction", {"transaction_id": "DEPOSIT-BAD"}))

	def test_automatic_sync_waits_for_the_setup(self):
		frappe.db.set_single_value("Wise Settings", {"automatic_sync": 1, "profile_id": None})
		with patch(f"{WISE_SETTINGS}.enqueue_synchronization") as enqueue:
			automatic_synchronization()
		enqueue.assert_not_called()
