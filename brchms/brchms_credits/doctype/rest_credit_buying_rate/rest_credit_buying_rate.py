# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

from brchms.brchms_credits import billing

CHANNEL_UNITS = {"sms": "sms_segment", "email": "email_message"}
MIN_MICROS = 1
MAX_MICROS = 10**15


class RestCreditBuyingRate(Document):
	"""A provider's management cost per unit, scoped to the selling company."""

	def validate(self):
		if not self.company:
			self.company = billing.resolve_selling_company()
		if not frappe.db.exists("Company", self.company):
			frappe.throw(_("Company {0} does not exist.").format(frappe.utils.escape_html(self.company)))

		if self.channel not in CHANNEL_UNITS:
			frappe.throw(_("Channel must be 'sms' or 'email', not '{0}'.").format(self.channel))
		expected_unit = CHANNEL_UNITS[self.channel]
		if not self.unit:
			self.unit = expected_unit
		elif self.unit != expected_unit:
			frappe.throw(
				_("{0} is bought in {1}, but this row says '{2}'.").format(
					self.channel.upper(), expected_unit, self.unit
				)
			)

		if self.provider not in ("nalo", "twilio", "smtp"):
			frappe.throw(_("Provider must be nalo, twilio or smtp."))
		if self.channel == "email" and self.provider != "smtp":
			frappe.throw(_("Email credits are bought from smtp; provider '{0}' is for SMS.").format(self.provider))

		company_currency = cstr(frappe.db.get_value("Company", self.company, "default_currency")).upper()
		self.currency = cstr(self.currency).strip().upper() or company_currency
		if self.currency != company_currency:
			frappe.throw(
				_(
					"{0} keeps its books in {1}, so a management buying cost in {2} would need an "
					"exchange rate nobody approved. Set the buying rate in the selling company's "
					"own currency."
				).format(
					frappe.utils.escape_html(self.company),
					frappe.utils.escape_html(company_currency),
					frappe.utils.escape_html(self.currency),
				)
			)

		try:
			price = int(self.price_per_unit_micros)
		except (TypeError, ValueError):
			frappe.throw(_("Buying price must be a whole number of millionths."))
		if price < MIN_MICROS or price > MAX_MICROS:
			frappe.throw(_("Buying price per unit is outside a believable range."))
		self.price_per_unit_micros = price

		if not self.effective_from:
			frappe.throw(_("A buying rate needs an Effective From date."))
		if self.effective_to and self.effective_to < self.effective_from:
			frappe.throw(_("This buying rate ends before it starts."))

		if self.destination_country_code:
			self.destination_country_code = normalize_prefix(self.destination_country_code)
		if not self.supplier_company:
			self.supplier_company = self.company


def normalize_prefix(raw):
	code = cstr(raw).strip().replace(" ", "").replace("-", "")
	if not code:
		return None
	if not code.startswith("+"):
		code = "+" + code
	if not code[1:].isdigit():
		frappe.throw(
			_("Destination Country Code must be a dialling code such as +233, not '{0}'.").format(
				frappe.utils.escape_html(cstr(raw))
			)
		)
	return code
