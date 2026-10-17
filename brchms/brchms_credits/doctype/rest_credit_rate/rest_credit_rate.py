# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

MIN_MICROS = 1
MAX_MICROS = 10**15
CHANNEL_UNITS = {"sms": "sms_segment", "email": "email_message"}


class RestCreditRate(Document):
	"""
	What one service unit costs. The instance rate card, and the AUTHORITY for price.

	The Rest app reads this when a purchase is created, which is the whole point:
	the price is decided once here, by whoever manages the money, instead of being
	retyped onto every purchase where two people can disagree and nobody notices
	until a reconciliation.
	"""

	def validate(self):
		self._validate_scope()
		self._validate_price()
		self._set_display_price()
		self._validate_window()
		self._refuse_a_retroactive_change()

	def _validate_scope(self):
		if self.channel not in CHANNEL_UNITS:
			frappe.throw(_("Channel must be 'sms' or 'email', not '{0}'.").format(self.channel))

		# The unit must match the channel. A rate that billed SMS in email messages
		# would be a unit error that the price alone cannot reveal.
		expected = CHANNEL_UNITS[self.channel]
		if not self.unit:
			self.unit = expected
		elif self.unit != expected:
			frappe.throw(
				_("{0} is billed in {1}, but this rate says '{2}'.").format(
					self.channel.upper(), expected, self.unit
				)
			)

		if not self.currency:
			frappe.throw(
				_(
					"A rate needs a currency. An amount with no currency is not a price, and the "
					"app cannot quote a purchase against it."
				)
			)

		if self.country_code:
			self.country_code = self._normalise_country_code(self.country_code)

	def _normalise_country_code(self, raw):
		"""
		Store the dialling code in the one shape the app matches on.

		The app resolves a rate by the DESTINATION's dialling code, compared as a
		string. '+233', '233' and '+233 ' are the same country, and a rate stored in a
		shape the comparison never produces is a rate that silently never applies --
		the fallback would quietly charge the default instead, and nobody would see a
		price they set being ignored.
		"""
		code = str(raw).strip().replace(" ", "").replace("-", "")
		if not code:
			return None
		if not code.startswith("+"):
			code = "+" + code
		if not code[1:].isdigit():
			frappe.throw(
				_(
					"Country Code must be a dialling code such as +233, not '{0}'. Leave it blank "
					"to price every destination the same."
				).format(frappe.utils.escape_html(str(raw)))
			)
		return code

	def _validate_price(self):
		"""
		The price must be a whole number of millionths, within sane bounds.

		`flt()` is deliberately NOT used to read it: it is an IEEE double, and it
		silently rounded a 2^53-scale integer during this integration. This is money
		at a scale where that matters, so it stays an integer throughout.
		"""
		try:
			price = int(self.price_per_unit_micros)
		except (TypeError, ValueError):
			frappe.throw(
				_(
					"Price Per Unit must be a whole number of millionths (1.00 is 1000000), not "
					"'{0}'."
				).format(frappe.utils.escape_html(str(self.price_per_unit_micros)))
			)

		if price < MIN_MICROS:
			frappe.throw(
				_(
					"Price Per Unit must be at least {0} micros. A rate of zero would hand out "
					"credits for nothing, and a negative one would take money that was never paid."
				).format(MIN_MICROS)
			)

		if price > MAX_MICROS:
			frappe.throw(
				_(
					"Price Per Unit of {0} micros is not a believable price for one message. Check "
					"the scale: 1.00 is 1000000, not 100."
				).format(price)
			)

		self.price_per_unit_micros = price

	def _set_display_price(self):
		"""
		The price as a person reads it: "GHS 0.045", not 45000 micros.

		The micros column is exact and is what the app charges from; this column is
		derived so an operator setting a rate can see the number they mean. Kept in
		step here rather than in a client script, so the stored value is right
		whether the row was saved from the desk, an import or the API.
		"""
		self.price_per_unit_display = format_micros(self.price_per_unit_micros, self.currency)

	def _validate_window(self):
		if not self.effective_from:
			frappe.throw(
				_(
					"A rate needs an Effective From date. Without one there is no way to tell which "
					"of two rows for the same channel is the current price."
				)
			)

		if self.effective_to and self.effective_to < self.effective_from:
			frappe.throw(
				_(
					"This rate ends on {0}, before it starts on {1}, so it would never apply."
				).format(self.effective_to, self.effective_from)
			)

	def _refuse_a_retroactive_change(self):
		"""
		Refuse to re-price a rate that funding records were already quoted against.

		Pricing is pinned when a purchase is created and never re-priced -- a
		deliberate decision, because a rate change must never fail a confirmation for
		a payment that genuinely happened. That guarantee only holds if the row a
		purchase was quoted from cannot change underneath it afterwards.

		The fix when a price really must change is to ADD a row from today, which is
		what `effective_from` is for.
		"""
		if self.is_new():
			return

		before = self.get_doc_before_save()
		if not before:
			return

		changed = (
			int(before.price_per_unit_micros or 0) != int(self.price_per_unit_micros or 0)
			or before.currency != self.currency
			or before.channel != self.channel
		)
		if not changed:
			return

		# Count PROPOSALS, not funding records.
		#
		# The funding doctype this used to count was retired when the settled
		# proposal became the funding event, so the guard silently counted zero
		# rows and stopped protecting anything. A proposal carries the price it
		# was quoted at (`unit_price_micros`), which is the fact that matters:
		# editing a rate a proposal was quoted from would rewrite history.
		quoted = frappe.db.count(
			"Rest Credit Proposal",
			{
				"channel": before.channel,
				"currency": before.currency,
				"unit_price_micros": int(before.price_per_unit_micros or 0),
			},
		)

		if quoted:
			frappe.throw(
				_(
					"{0} proposal(s) were already quoted at {1} micros for {2}/{3}. Editing this "
					"rate would change what was charged for money already paid. Add a NEW rate with "
					"a later Effective From instead -- this row stays as the record of what was "
					"charged before."
				).format(quoted, before.price_per_unit_micros, before.channel, before.currency)
			)


def format_micros(micros, currency):
	"""
	Render a micros integer as a natural price: 45000 -> "GHS 0.045".

	Up to six decimal places, at least two; trailing zeros trimmed so 0.05 stays
	"0.05" rather than "0.050000", while a whole price keeps its cents ("1.00").
	"""
	try:
		price = int(micros)
	except (TypeError, ValueError):
		return None
	if price < 0:
		return None

	sign = "-" if price < 0 else ""
	whole, fraction = divmod(abs(price), 1_000_000)
	digits = f"{fraction:06d}".rstrip("0")
	if len(digits) < 2:
		digits = digits.ljust(2, "0")
	value = f"{sign}{whole}.{digits}"
	return f"{currency} {value}" if currency else value
