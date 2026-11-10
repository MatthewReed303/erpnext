# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import datetime

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists
from frappe.utils import add_months, flt, get_datetime, getdate
from frappe.utils.data import convert_utc_to_system_timezone

from erpnext.erpnext_integrations.doctype.wise_settings.wise_connector import WiseConnector

# statements are read again from a few days back, for transactions Wise dates earlier than when they appear
SYNC_OVERLAP_DAYS = 7

MODES_OF_PAYMENT = {
	"DEPOSIT": "Wire Transfer",
	"MONEY_ADDED": "Wire Transfer",
	"TRANSFER": "Wire Transfer",
	"DIRECT_DEBIT": "Wire Transfer",
	"CARD": "Credit Card",
}


class WiseSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_token: DF.Password | None
		automatic_sync: DF.Check
		company: DF.Link | None
		enabled: DF.Check
		environment: DF.Literal["Production", "Sandbox"]
		profile_id: DF.Data | None
		profile_name: DF.Data | None
	# end: auto-generated types

	pass


@frappe.whitelist(methods=["POST"])
def get_profiles():
	frappe.has_permission("Wise Settings", throw=True)

	return [
		{
			"id": str(profile["id"]),
			"type": profile.get("type"),
			"name": profile.get("businessName") or profile.get("fullName"),
		}
		for profile in WiseConnector().profiles()
	]


@frappe.whitelist(methods=["POST"])
def add_bank_accounts():
	"""Make or update a company Bank Account for each of the profile's Wise balances, with the bank
	details Wise gives for it."""
	frappe.has_permission("Wise Settings", ptype="write", throw=True)

	settings = get_settings()
	wise = WiseConnector(settings)
	now = utc_now()

	result = []
	for balance in wise.balances(settings.profile_id):
		statement = wise.statement(
			settings.profile_id, balance["id"], balance["currency"], now - datetime.timedelta(days=1), now
		)
		details = next((d for d in statement.get("bankDetails") or [] if not d.get("deprecated")), None)
		result.append(add_bank_account(settings.company, balance, details))

	return result


def add_bank_account(company, balance, details=None):
	currency = balance["currency"]
	details = details or {}
	codes = {code["scheme"]: code["value"] for code in details.get("bankCodes") or []}
	numbers = {
		number["accountType"]: number["accountNumber"] for number in details.get("accountNumbers") or []
	}
	swift = codes.pop("Swift/BIC", None)

	bank = get_bank((details.get("address") or {}).get("country"), currency, swift)
	values = {
		"bank": bank,
		"iban": numbers.pop("IBAN", None),
		"bank_account_no": next(iter(numbers.values()), None),
		"branch_code": next(iter(codes.values()), None),
	}

	existing = get_existing_bank_account(
		company, str(balance["id"]), values["iban"] or values["bank_account_no"]
	)
	if existing:
		bank_account = frappe.get_doc("Bank Account", existing)
		bank_account.update({**values, "integration_id": str(balance["id"])})
		bank_account.save()
		return bank_account.name

	account_name = f"Wise {currency}"
	bank_account = frappe.get_doc(
		{
			"doctype": "Bank Account",
			"account_name": account_name,
			"account": get_gl_account(company, account_name, currency),
			"is_company_account": 1,
			"company": company,
			"integration_id": str(balance["id"]),
			**values,
		}
	)
	bank_account.insert(set_name=append_number_if_name_exists("Bank Account", f"{account_name} - {bank}"))
	return bank_account.name


def get_existing_bank_account(company, integration_id, number):
	"""The company's Bank Account linked to this balance, or one made by hand with its account number."""
	filters = {"company": company, "is_company_account": 1}
	return frappe.db.get_value("Bank Account", {**filters, "integration_id": integration_id}) or (
		number
		and (
			frappe.db.get_value("Bank Account", {**filters, "iban": number})
			or frappe.db.get_value("Bank Account", {**filters, "bank_account_no": number})
		)
	)


def get_bank(country, currency, swift):
	"""The Bank holding a balance's account details. Wise holds each currency's details in the country of
	that currency's banking system, each with its own SWIFT code."""
	bank_name = f"Wise {country or currency}"
	if frappe.db.exists("Bank", bank_name):
		if swift:
			frappe.db.set_value("Bank", bank_name, "swift_number", swift)
		return bank_name

	return frappe.get_doc({"doctype": "Bank", "bank_name": bank_name, "swift_number": swift}).insert().name


def get_gl_account(company, account_name, currency):
	name = frappe.db.get_value(
		"Account", {"company": company, "account_name": account_name, "account_currency": currency}
	)
	if name:
		return name
	# an account of that name in another currency is left alone: the new one is numbered
	base_name, number = account_name, 1
	while frappe.db.exists("Account", {"company": company, "account_name": account_name}):
		number += 1
		account_name = f"{base_name} {number}"
		name = frappe.db.get_value(
			"Account", {"company": company, "account_name": account_name, "account_currency": currency}
		)
		if name:
			return name

	# the Bank group, else the group the company's bank accounts are in
	parent_account = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Bank", "is_group": 1, "disabled": 0}
	) or frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0},
		"parent_account",
	)
	if not parent_account:
		frappe.throw(
			_(
				"Please setup and enable a group account with the Account Type - {0} for the company {1}"
			).format(frappe.bold(_("Bank")), company)
		)

	return (
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent_account,
				"account_type": "Bank",
				"account_currency": currency,
				"company": company,
			}
		)
		.insert()
		.name
	)


def automatic_synchronization():
	settings = frappe.get_single("Wise Settings")
	# only once it is set up, so an unfinished setup doesn't fail every hour
	if (
		settings.enabled
		and settings.automatic_sync
		and settings.profile_id
		and settings.company
		and settings.get_password("api_token", raise_exception=False)
	):
		enqueue_synchronization()


@frappe.whitelist(methods=["POST"])
def enqueue_synchronization():
	frappe.has_permission("Wise Settings", ptype="write", throw=True)
	if not frappe.db.get_single_value("Wise Settings", "enabled"):
		frappe.throw(_("Enable Wise Settings to sync transactions"))

	frappe.enqueue(
		"erpnext.erpnext_integrations.doctype.wise_settings.wise_settings.sync_transactions",
		queue="long",
		job_id="wise_sync_transactions",
		deduplicate=True,
	)


def sync_transactions():
	"""Import the new transactions of each Wise balance that has a Bank Account, from the date it was
	last read up to (a few days earlier, for transactions Wise dates back), or the last twelve months
	for a new Bank Account."""
	if not frappe.db.get_single_value("Wise Settings", "enabled"):
		return
	settings = get_settings()
	wise = WiseConnector(settings)
	end = utc_now()
	try:
		balances = wise.balances(settings.profile_id)
	except Exception:
		frappe.log_error(_("Wise balances could not be read"), reference_doctype="Wise Settings")
		return

	for balance in balances:
		bank_account = frappe.db.get_value(
			"Bank Account",
			{"integration_id": str(balance["id"]), "company": settings.company, "disabled": 0},
			["name", "last_integration_date"],
			as_dict=True,
		)
		if not bank_account:
			continue

		start = get_datetime(bank_account.last_integration_date or add_months(getdate(), -12))
		start = start.replace(tzinfo=datetime.UTC) - datetime.timedelta(days=SYNC_OVERLAP_DAYS)
		try:
			statement = wise.statement(settings.profile_id, balance["id"], balance["currency"], start, end)
		except Exception:
			frappe.log_error(
				_("Wise transactions of {0} could not be read").format(bank_account.name),
				reference_doctype="Bank Account",
				reference_name=bank_account.name,
			)
			continue

		failed = []
		for transaction in reversed(statement.get("transactions") or []):
			frappe.db.savepoint("wise_sync_transaction")
			try:
				new_bank_transaction(transaction, bank_account.name)
			except Exception:
				frappe.db.rollback(save_point="wise_sync_transaction")
				failed.append(transaction_date(transaction))
				# it is read again on every sync until it imports; said once a day
				key = f"wise_failed:{bank_account.name}:{transaction.get('referenceNumber')}"
				if frappe.cache.get_value(key):
					continue
				frappe.cache.set_value(key, 1, expires_in_sec=24 * 60 * 60)
				frappe.log_error(
					_("Wise transaction {0} could not be imported").format(
						transaction.get("referenceNumber")
					),
					reference_doctype="Bank Account",
					reference_name=bank_account.name,
				)

		# read up to now, or up to the first transaction that couldn't be imported, which is read again
		read_to = min(failed) if failed else convert_utc_to_system_timezone(end).date()
		frappe.db.set_value("Bank Account", bank_account.name, "last_integration_date", read_to)


def new_bank_transaction(transaction, bank_account):
	"""Make a Bank Transaction for a statement transaction not imported yet. The amount is what moved the
	balance; Wise's fees are part of a withdrawal, as its included fee."""
	if frappe.db.exists(
		"Bank Transaction", {"transaction_id": transaction["referenceNumber"], "bank_account": bank_account}
	):
		return None

	amount = flt(transaction["amount"]["value"])
	details = transaction.get("details") or {}
	bank_transaction = frappe.get_doc(
		{
			"doctype": "Bank Transaction",
			"date": transaction_date(transaction),
			"bank_account": bank_account,
			"deposit": max(amount, 0),
			"withdrawal": max(-amount, 0),
			"included_fee": flt((transaction.get("totalFees") or {}).get("value")),
			"currency": transaction["amount"]["currency"],
			"transaction_id": transaction["referenceNumber"],
			"transaction_type": mode_of_payment(details.get("type")),
			"reference_number": details.get("paymentReference") or transaction["referenceNumber"],
			"description": details.get("description"),
			"bank_party_name": details.get("senderName"),
			"bank_party_account_number": details.get("senderAccount"),
		}
	)
	bank_transaction.insert()
	bank_transaction.submit()
	return bank_transaction.name


def mode_of_payment(wise_type):
	"""The Mode of Payment for a kind of Wise transaction, which the Bank Reconciliation Tool uses for
	the vouchers it makes, when the site has it."""
	mode = MODES_OF_PAYMENT.get(wise_type)
	return mode if mode and frappe.db.exists("Mode of Payment", mode) else None


def transaction_date(transaction):
	return convert_utc_to_system_timezone(get_datetime(transaction["date"])).date()


def get_settings():
	settings = frappe.get_single("Wise Settings")
	if not settings.profile_id or not settings.company:
		frappe.throw(_("Select the Wise profile and the Company in Wise Settings"))
	return settings


def utc_now():
	return datetime.datetime.now(datetime.UTC)
