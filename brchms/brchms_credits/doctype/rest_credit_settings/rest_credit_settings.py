# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The site-wide credit purchase terms.

A Single doctype because there is exactly one answer to "what is the smallest
purchase this site will sell". Per-church terms (a discount, a monthly free
allowance) deliberately do NOT live here: they are a property of the
organization, and putting one church's discount in a site-wide record would
apply it to every church.
"""

import frappe
from frappe.model.document import Document

from brchms.brchms_credits import terms


class RestCreditSettings(Document):
	def validate(self):
		"""
		Refuse a floor that is negative or that the currency cannot express.

		The `Currency` fieldtype stores a float. This is the one place a figure
		with finer precision than the smallprint (0.001 in a 2-decimal
		currency) is caught, because it would otherwise be silently rounded
		BEFORE it became a floor -- a minimum that reads 5.00 but behaves as
		5.01 is worse than one that fails here.
		"""
		amount = terms.validate_amount_precision(self.minimum_purchase_amount)
		self.minimum_purchase_amount = float(amount)
