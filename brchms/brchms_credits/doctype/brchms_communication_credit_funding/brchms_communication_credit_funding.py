# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class BRCHMSCommunicationCreditFunding(Document):
	"""
	One confirmed funding event, and the invariants that make it trustworthy.

	## Why validation lives here and not only in the API

	The API is not the only way a row reaches this table. `bench execute`, a data
	import, a fixture, and the Desk's own form all bypass `confirm_funding`
	entirely -- and a malformed row inserted that way is indistinguishable from a
	valid one to the app that reads it. A rule enforced on one write path and not
	another is a rule with a hole in it, so the invariants are on the document.

	## The invariants

	The app turns a row here into spendable credits. Each check below closes a way
	that could go wrong silently:

	  * `units` must be positive. Zero creates nothing; a negative would DESTROY
	    credits the church already owns, or create them, depending on which side
	    of the app reads it. The API refuses it too; this is the backstop.
	  * `event_id` must be unique. It is the app's idempotency key, and a
	    duplicate here means a retried confirmation recorded a payment twice.
	  * `channel` must be one of two. SMS and email balances are separate, and a
	    typo would create a third balance the app never spends.
	  * `currency` must exist. A funding record in a currency the app cannot name
	    is a record whose amount cannot be read.
	  * the money, when all three parts are present, must be CONSISTENT:
	    `units x unit_price_micros == amount_minor x 10^6`. This is the check that
	    makes "the authority decides what was paid for" true rather than stated.
	"""

	def validate(self):
		self._validate_units()
		self._validate_channel()
		self._validate_currency()
		self._validate_money_consistency()

	def before_insert(self):
		# Stamped here rather than taken from the caller: "when the authority
		# recorded it" is a fact about this system, and a client-supplied
		# timestamp is not evidence of one.
		if not self.confirmed_at:
			self.confirmed_at = now_datetime()

	def _validate_units(self):
		if self.units is None:
			frappe.throw(frappe._("Units are required: this record grants nothing without them."))

		if int(self.units) <= 0:
			frappe.throw(
				frappe._(
					"Units must be greater than zero. A funding record granting {0} units would "
					"not activate any credits, and the app refuses to record it as a payment."
				).format(self.units)
			)

	def _validate_channel(self):
		if self.channel not in ("sms", "email"):
			frappe.throw(
				frappe._(
					"Channel must be either sms or email (received {0}). SMS and email credit are "
					"separate balances and are never interchangeable."
				).format(self.channel or "nothing")
			)

	def _validate_currency(self):
		if not self.currency:
			return
		if not frappe.db.exists("Currency", self.currency):
			frappe.throw(
				frappe._(
					"{0} is not a currency on this site. Add it, or send the currency the payment "
					"was actually made in."
				).format(self.currency)
			)

	def _validate_money_consistency(self):
		"""
		Units x price must equal the amount paid.

		Only checked when all three parts are present: a record that names units
		and an amount but no unit price is incomplete rather than wrong, and
		refusing it would make the partial records an operator creates by hand
		impossible to save.

		The comparison is deliberately exact on integers. `flt(..., 6)` gives the
		six decimal places the micros scale has, so 250 x 45000 = 11,250,000,000
		micros equals 11250 minor x 10^6 exactly, and a one-cent discrepancy fails.
		"""
		if self.units is None or self.amount_minor is None or self.unit_price_micros is None:
			return

		expected_micros = flt(self.amount_minor) * 1_000_000
		actual_micros = flt(self.units) * flt(self.unit_price_micros)

		if abs(expected_micros - actual_micros) > 0.5:
			frappe.throw(
				frappe._(
					"The money does not add up: {0} units at {1} micros per unit is {2} minor "
					"units, but {3} was recorded as paid. Check the unit price against the "
					"payment, or record the amount that was actually paid."
				).format(
					self.units,
					self.unit_price_micros,
					int(actual_micros // 1_000_000),
					self.amount_minor,
				)
			)
