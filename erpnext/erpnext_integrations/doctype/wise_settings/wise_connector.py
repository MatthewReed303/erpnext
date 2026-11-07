# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.integrations.utils import make_get_request

HOSTS = {"Production": "https://api.wise.com", "Sandbox": "https://api.wise-sandbox.com"}


class WiseConnector:
	"""Read-only access to a Wise account: profiles, balances and balance statements."""

	def __init__(self, settings=None):
		settings = settings or frappe.get_single("Wise Settings")
		self.host = HOSTS[settings.environment or "Production"]
		self.token = settings.get_password("api_token", raise_exception=False)
		if not self.token:
			frappe.throw(_("Set the API Token in Wise Settings"))

	def get(self, path, params=None):
		return make_get_request(
			self.host + path,
			headers={"Authorization": f"Bearer {self.token}"},
			params=params,
		)

	def profiles(self):
		return self.get("/v2/profiles")

	def balances(self, profile_id):
		return self.get(f"/v4/profiles/{profile_id}/balances", params={"types": "STANDARD"})

	def statement(self, profile_id, balance_id, currency, start, end):
		"""The balance's statement between two UTC datetimes: its bank details and transactions."""
		return self.get(
			f"/v1/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
			params={
				"currency": currency,
				"intervalStart": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
				"intervalEnd": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
				"type": "COMPACT",
			},
		)
