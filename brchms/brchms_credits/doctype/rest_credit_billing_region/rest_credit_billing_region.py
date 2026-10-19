# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class RestCreditBillingRegion(Document):
	"""One country → one selling company. Unknown countries are refused."""

	def validate(self):
		if not self.country:
			frappe.throw(_("A billing region needs a country."))
		if not self.company:
			frappe.throw(_("A billing region needs a selling company."))

		company = frappe.db.get_value(
			"Company", self.company, ["name", "default_currency"], as_dict=True
		)
		if not company:
			frappe.throw(_("Company {0} does not exist.").format(frappe.utils.escape_html(self.company)))
		if not company.default_currency:
			frappe.throw(
				_(
					"Company {0} has no default currency, so credits in {1} cannot be priced."
				).format(
					frappe.utils.escape_html(self.company),
					frappe.utils.escape_html(self.country),
				)
			)

		self.currency = company.default_currency

		duplicate = frappe.db.get_value(
			"Rest Credit Billing Region",
			{"country": self.country, "name": ("!=", self.name)},
			"name",
		)
		if duplicate:
			frappe.throw(
				_(
					"{0} is already mapped to a billing region ({1}). One country must resolve to exactly "
					"one company, so edit that row instead of adding a second."
				).format(
					frappe.utils.escape_html(self.country),
					frappe.utils.escape_html(duplicate),
				)
			)
