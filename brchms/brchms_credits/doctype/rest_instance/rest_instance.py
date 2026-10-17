# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class RestInstance(Document):
	"""
	One Rest deployment that has sent funding to this site.

	Rows are created when the app first raises a proposal, never by hand, so a
	deployment can be recognised in a funding list without anyone having to
	pre-register it. The document name IS the `instance_id`, which is what makes
	the link from a funding record resolve to something with a name.
	"""

	def validate(self):
		if not (self.instance_id or "").strip():
			frappe.throw(
				_(
					"An instance needs its identifier. It is minted by the Rest deployment and "
					"comes in with the confirmation — there is nothing to type here."
				)
			)

		if not (self.display_name or "").strip():
			# Not fatal: a deployment whose organization has no name yet is still a
			# deployment that has paid for credits. The id is the identifier.
			self.display_name = self.instance_id
