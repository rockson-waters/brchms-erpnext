# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The registry row: one Rest organization, as this site will bill it.

The BILLING-IDENTITY invariants live in `brchms.brchms_credits.registry`, not
here — a registry row is written by that module through `ignore_permissions`,
because the whitelisted methods that call it are the authorisation boundary
(the same posture as the credits API, documented there). A second
implementation of the same rules would be a second chance to get them wrong.

The COMMERCIAL TERMS on this doctype are different: an operator sets them by
hand in the Desk, so their validation belongs here, where every save (Desk,
import or API) reaches it. `registry` never writes these fields — it fills the
billing identity and the funding groups only — so a registration refresh can
never overwrite a negotiated discount or allowance.
"""

from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr

MAX_DISCOUNT_PERCENT = Decimal("100")
DISCOUNT_QUANTUM = Decimal("0.01")


class RestCreditOrganization(Document):
	def validate(self):
		self._validate_discount()
		self._validate_allowance_units()

	def _validate_discount(self):
		"""
		A discount is 0 <= x < 100, at two decimal places.

		Strictly less than 100 because a 100% discount would sell credits for
		nothing -- and credits are granted against money that arrived, so a
		zero-price purchase is a contradiction, not a promotion. Two decimals
		because the percentage is converted to integer basis points for the
		quote; a third decimal would silently round and make the stored term
		differ from the one that was priced.
		"""
		raw = self.get("discount_percent")
		try:
			percent = Decimal(str(raw if raw not in (None, "") else 0))
		except (InvalidOperation, ValueError):
			frappe.throw(
				_("Discount % must be a number, not '{0}'.").format(cstr(raw)),
				frappe.ValidationError,
			)

		if percent < 0:
			frappe.throw(_("Discount % cannot be negative."), frappe.ValidationError)

		if percent >= MAX_DISCOUNT_PERCENT:
			frappe.throw(
				_(
					"Discount % must be less than 100: a 100% discount would give credits away "
					"for nothing, and credits are only granted against money that arrived."
				),
				frappe.ValidationError,
			)

		if percent.quantize(DISCOUNT_QUANTUM) != percent:
			frappe.throw(
				_(
					"Discount % can have at most two decimal places (for example 12.50), because "
					"the price is re-derived from whole basis points on both sides."
				),
				frappe.ValidationError,
			)

		self.discount_percent = float(percent)

	def _validate_allowance_units(self):
		"""A monthly allowance is a whole, non-negative number of units."""
		for fieldname, label in (
			("monthly_free_sms_units", "Monthly Free SMS Units"),
			("monthly_free_email_units", "Monthly Free Email Units"),
		):
			raw = self.get(fieldname)
			try:
				units = int(cint(raw))
			except (TypeError, ValueError):
				frappe.throw(
					_("{0} must be a whole number, not '{1}'.").format(label, cstr(raw)),
					frappe.ValidationError,
				)

			if units < 0:
				frappe.throw(
					_("{0} cannot be negative.").format(label), frappe.ValidationError
				)
			self.set(fieldname, units)
