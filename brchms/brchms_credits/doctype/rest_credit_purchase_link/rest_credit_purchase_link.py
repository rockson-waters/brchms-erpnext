# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Where a customer goes to buy credits.

A singleType, so there is exactly one answer to "where do I pay". Several rows would
raise the question of which applies, and the app must never have to decide that.

The app renders this URL and does NOTHING else with it: the gateway, the webform and
the settlement all live in ERPNext, which is why switching a gateway on changes
nothing about who vouches for money. `get_purchase_link` in `proposals.py` is the read.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class RestCreditPurchaseLink(Document):
	def validate(self):
		self._require_https_when_enabled()
		self._validate_the_parameter_name()

	def _require_https_when_enabled(self):
		"""
		Refused at SAVE as well as at read.

		A payment page served over plain HTTP can be rewritten in transit, and the
		customer would pay whoever got there first. Catching it when the URL is typed
		means the operator learns immediately, rather than a customer discovering it.
		"""
		if not self.is_enabled:
			return

		url = (self.purchase_url or "").strip()
		if not url:
			frappe.throw(
				_(
					"Set the Purchase URL before switching the link on. An enabled link with no "
					"destination would show customers a button that goes nowhere."
				)
			)

		if not url.lower().startswith("https://"):
			frappe.throw(
				_(
					"The Purchase URL must start with https://. A payment page served over plain HTTP "
					"can be rewritten in transit, and the customer would be paying whoever got there "
					"first."
				)
			)

	def _validate_the_parameter_name(self):
		"""
		The parameter name is query-string syntax, not prose.

		A name containing `&`, `=` or a space would produce a URL whose meaning differs
		from the intent -- and the failure is silent: the customer is sent to the right
		host with the wrong parameters.
		"""
		param = (self.reference_param or "").strip()
		if not param:
			return

		if any(character in param for character in "&=?# "):
			frappe.throw(
				_(
					"Reference Parameter must be a plain query parameter name such as 'ref', not "
					"'{0}'. It is appended to the URL as `?{1}=...`, so those characters would change "
					"what the address means."
				).format(frappe.utils.escape_html(param), frappe.utils.escape_html(param))
			)
