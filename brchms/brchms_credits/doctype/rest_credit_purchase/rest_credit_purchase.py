# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
A customer's purchase request, before any money moves.

This exists because a Web Form needs a target that can hold what the customer
types. A Sales Invoice cannot: its line item is a child table and it has no email
field, so a form pointed at it cannot collect an item, a quantity and an address —
and Frappe's own `WebForm.validate_fields` refuses the attempt rather than
silently dropping the fields.

So the form records the REQUEST here, and the invoice is raised from it by
`brchms_credits.purchase`, server-side. That split is deliberate and not merely
expedient: the buyer chooses what to buy, and the deployment chooses the item, the
rate, the company and the accounts. A form that let a buyer name the item would let
them bill themselves for anything at any price.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class RestCreditPurchase(Document):
	def validate(self):
		self._require_a_sane_order()

	def _require_a_sane_order(self):
		"""
		Refuse an order that cannot become an invoice.

		These are the mistakes a public form actually receives -- a pasted negative
		amount, a space, an empty unit box -- and each is cheaper to refuse here, with
		the field named, than to discover when the invoice fails to save after the
		customer has already been told their purchase was recorded.
		"""
		if int(self.units or 0) <= 0:
			frappe.throw(
				_("Enter how many credits you are buying — a whole number greater than zero."),
				frappe.ValidationError,
			)

		if float(self.amount or 0) <= 0:
			frappe.throw(
				_("Enter the amount you are paying — greater than zero."),
				frappe.ValidationError,
			)

		if not (self.buyer_name or "").strip():
			frappe.throw(_("Enter your name."), frappe.ValidationError)

		email = (self.buyer_email or "").strip()
		if "@" not in email or email.startswith("@") or email.endswith("@"):
			frappe.throw(
				_("Enter a valid email address, for example name@example.com."),
				frappe.ValidationError,
			)

		if self.channel not in ("sms", "email"):
			frappe.throw(
				_("Choose which kind of credit you are buying: sms or email."),
				frappe.ValidationError,
			)
