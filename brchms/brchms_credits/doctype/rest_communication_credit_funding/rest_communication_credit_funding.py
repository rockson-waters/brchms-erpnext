# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class RestCommunicationCreditFunding(Document):
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

		## The scale, which is where this went wrong first

		Three units meet here and two of them are easy to confuse:

		  * `amount_minor` is money in the currency's MINOR units -- cents. 11250
		    means 112.50 CAD, so MINOR:MAJOR is 10^2.
		  * `unit_price_micros` is millionths of a MAJOR unit. 450000 means 0.45
		    CAD, so MICROS:MAJOR is 10^6.
		  * `units` is a plain count.

		So the left side (`units x unit_price_micros`) is already in micros, while
		the right side needs MINOR -> MICROS, which is `10^6 / 10^2 = 10^4`, NOT
		`10^6`.

		The first version used 10^6 and was therefore 100x too strict: it refused
		every correct record, including the probe in this app's own README. The
		live test suite caught it, and this comment is the arithmetic written down
		so it cannot be re-derived wrongly:

		    250 units x 450000 micros = 112,500,000 micros   (0.45 each -> 112.50)
		    11250 minor x 10^4        = 112,500,000 micros   ✓ equal

		## And the second thing the same test caught: floats

		This ran on `frappe.utils.flt()`, which converts through a 64-bit double.
		At 2^53 + 1 that rounds:

		    flt(9007199254740993) -> 9007199254740992

		so the check compared a value the record did not contain and refused a
		record that is arithmetically exact. That is the very failure this whole
		app avoids elsewhere — the app stores units and money as bigintegers and
		parses them as BigInt precisely so large values stay exact — so the
		validator now does the same: **exact integer arithmetic, no floats
		anywhere.**
		"""
		if self.units is None or self.amount_minor is None or self.unit_price_micros is None:
			return

		def as_int(value, field):
			"""Exact int, or a refusal naming the field (never a rounded float)."""
			try:
				return int(value)
			except (TypeError, ValueError):
				frappe.throw(
					frappe._("{0} must be a whole number to check the money on this record.").format(
						frappe.unscrub(field)
					)
				)

		# MINOR -> MICROS: 10^6 micros per major, divided by 10^2 minor per major.
		MINOR_TO_MICROS = 10_000

		units = as_int(self.units, "units")
		price = as_int(self.unit_price_micros, "unit_price_micros")
		amount = as_int(self.amount_minor, "amount_minor")

		actual_micros = units * price
		expected_micros = amount * MINOR_TO_MICROS

		if actual_micros != expected_micros:
			# Both sides are reported in MICROS so the reader can see which one is
			# wrong, rather than being told only that they disagree.
			frappe.throw(
				frappe._(
					"The money does not add up: {0} units at {1} micros per unit is {2} micros, "
					"but the amount recorded ({3} in minor units) is {4} micros. Check the unit "
					"price against the payment, or record the amount that was actually paid."
				).format(
					self.units,
					self.unit_price_micros,
					actual_micros,
					self.amount_minor,
					expected_micros,
				)
			)
