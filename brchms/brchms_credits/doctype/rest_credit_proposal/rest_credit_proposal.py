# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
A request to buy credits, and -- once submitted -- the settlement that makes them
claimable.

The two states are the whole design:

  docstatus 0  DRAFT      requested and unpaid. Contributes nothing, claims nothing.
  docstatus 1  SUBMITTED  money received and evidenced. This is what the app claims.
  docstatus 2  CANCELLED  a settlement that was revoked.

Only a holder of the `submit` permission can move draft to submitted, and the
tenant application deliberately does not hold it. That is the boundary: nothing the
app says, on its own, can turn a request into credits.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, now_datetime

# Doctypes whose records already carry a gateway's own transaction identifier, so the
# proposal does not need a hand-entered reference on top of them.
GATEWAY_MANAGED_DOCTYPES = {"Payment Request", "Integration Request"}


class RestCreditProposal(Document):
	def validate(self):
		self._require_the_essentials()
		self._refuse_edits_after_submission()
		self._stamp_the_settler()
		self._record_any_variance()

	def before_submit(self):
		"""
		What must be true before this becomes a claim on real credit.

		These checks exist because submitting is the moment a request stops being
		aspirational. Everything before it is cheap to get wrong; after it, the app
		will grant units against this record.
		"""
		self._require_evidence_of_payment()
		self._require_a_trace_for_hand_settled_money()
		self._require_a_reason_to_settle_an_expired_proposal()

		self.confirmed_at = now_datetime()
		self.settled_by = frappe.session.user

	def on_submit(self):
		self._touch_instance()

	def before_cancel(self):
		"""
		Cancelling revokes the claim.

		The app treats a settlement as claimable only while it is submitted, so a
		cancellation must reach it. That is why cancelling requires a reason: a
		settlement can be revoked after credits were granted, and whoever looks at
		this later needs to know why the money stopped counting.
		"""
		reason = cstr(self.settlement_note).strip()
		if not reason:
			frappe.throw(
				_(
					"Record why this settlement is being cancelled before cancelling it. The Rest "
					"app may already have granted these credits, so the reason is what explains a "
					"balance that has gone down."
				)
			)

	# ── invariants ──────────────────────────────────────────────────────────────

	def _require_the_essentials(self):
		if not cstr(self.instance_id).strip():
			frappe.throw(
				_(
					"A proposal must name the Rest deployment that raised it, or there is nothing to "
					"grant the credits to."
				)
			)

		if self.channel not in ("sms", "email"):
			frappe.throw(_("Channel must be 'sms' or 'email', not '{0}'.").format(cstr(self.channel)))

		if not self.currency:
			frappe.throw(_("A proposal is quoted in a currency, and none was named."))

		# Integers throughout: `flt` is an IEEE double and silently rounded a
		# 2^53-scale value earlier in this integration. This is money.
		for fieldname, label in (
			("units", "Units"),
			("amount_minor", "Amount"),
			("unit_price_micros", "Unit Price"),
		):
			raw = self.get(fieldname)
			if raw in (None, ""):
				continue
			try:
				value = int(raw)
			except (TypeError, ValueError):
				frappe.throw(_("{0} must be a whole number, not '{1}'.").format(label, cstr(raw)))
			if value < 0:
				frappe.throw(_("{0} cannot be negative.").format(label))
			self.set(fieldname, value)

	def _refuse_edits_after_submission(self):
		"""
		A submitted settlement is a record of what happened, not a working document.

		`docstatus` already makes Frappe refuse most edits, but a couple of paths
		(notably `db_set` and scripted updates) can still move a submitted value. The
		amount and units are the ones that matter: editing them after the app has
		granted credits would make the two ledgers disagree about money.
		"""
		if self.docstatus != 1 or self.is_new():
			return

		before = self.get_doc_before_save()
		if not before:
			return

		for fieldname, label in (
			("units", "Units"),
			("amount_minor", "Amount"),
			("currency", "Currency"),
			("channel", "Channel"),
			("unit_price_micros", "Unit Price"),
			("instance_id", "Rest Instance"),
		):
			if self.get(fieldname) != before.get(fieldname):
				frappe.throw(
					_(
						"{0} cannot change on a submitted settlement. The credits have already been "
						"granted against this record; cancel it and raise a new proposal instead."
					).format(label)
				)

	def _stamp_the_settler(self):
		if self.docstatus == 1 and not self.settled_by:
			self.settled_by = frappe.session.user

	def _record_any_variance(self):
		"""
		How far the money that arrived differs from the quote.

		Recorded rather than enforced. A payment for a different amount is a fact,
		and refusing it would strand money that genuinely moved -- so the settled
		amount wins and this is the number that shows by how much. Zero is the normal
		case.
		"""
		if self.amount_minor in (None, "") or self.currency in (None, ""):
			self.quote_variance_minor = 0
			return

		# The quoted amount is what `units x unit_price_micros` implies. Recomputing
		# it from the settled units is deliberate: if a settler also corrects the
		# units, the quote they are being compared against moves with them, so the
		# variance measures the MONEY disagreement rather than double-counting.
		quoted = self._quoted_amount_minor()
		if quoted is None:
			self.quote_variance_minor = 0
			return

		self.quote_variance_minor = int(self.amount_minor) - quoted

	def _quoted_amount_minor(self):
		"""
		What `units x unit_price_micros` comes to, in minor units.

		Uses the currency's own subunit count rather than a fixed 10,000: most
		currencies have two decimal places, but one like MGA has five subunits per
		major unit, and a hardcoded factor would be wrong by 5x there.
		"""
		if not self.unit_price_micros or not self.units:
			return None

		subunits = _currency_subunits(self.currency)
		if not subunits:
			return None

		# micros / (10^6) * subunits  ==  units * micros * subunits / 10^6
		return (int(self.units) * int(self.unit_price_micros) * subunits) // 10**6

	def _require_evidence_of_payment(self):
		"""
		Submitting means "the money is here", so it must point at where.

		Without this the whole exercise is a claim with nothing behind it: the app
		would grant credits because somebody clicked Submit. Requiring a real
		document is what makes the books the evidence.
		"""
		if not cstr(self.payment_doctype).strip() or not cstr(self.payment_document).strip():
			frappe.throw(
				_(
					"Record the payment this settlement is for before submitting. Pick the payment "
					"document type and the document itself -- a settlement with no payment behind it "
					"would make credits claimable on nothing but a click."
				)
			)

		if not frappe.db.exists(self.payment_doctype, self.payment_document):
			frappe.throw(
				_("{0} {1} does not exist.").format(
					frappe.utils.escape_html(cstr(self.payment_doctype)),
					frappe.utils.escape_html(cstr(self.payment_document)),
				)
			)

	def _require_a_trace_for_hand_settled_money(self):
		"""
		A settlement made by hand needs the reference the payment carries.

		The three ways money arrives are genuinely different, and only one of them
		leaves a system behind that can vouch for it:

		  * a PAYMENT GATEWAY supplies its own transaction id and the linked payment
		    document is the evidence, so nothing more is needed here;
		  * a BANK TRANSFER has the bank's reference on the statement;
		  * CASH has nothing except the receipt issued at the desk.

		For the last two, this field is the only trace that money moved. Without it a
		settlement is a claim resting on somebody's memory, and a reconciliation a
		month later has nothing to match against.
		"""
		gateway_managed = cstr(self.payment_doctype).strip() in GATEWAY_MANAGED_DOCTYPES

		if gateway_managed:
			return

		if not cstr(self.payment_reference).strip():
			frappe.throw(
				_(
					"Record the reference this payment carries before submitting: the bank's "
					"transaction reference, or the receipt number issued at the desk. It is the only "
					"trace that money arrived, because nothing else vouches for a payment taken by "
					"hand."
				)
			)

	def _require_a_reason_to_settle_an_expired_proposal(self):
		"""
		An expired proposal can still be settled, but only deliberately.

		Money that arrives after the deadline is real, and refusing it permanently
		would strand a payment for a bookkeeping reason. So the settler may proceed
		-- but the note is required, because "this was settled after it expired" is
		exactly the kind of thing somebody has to explain later.
		"""
		if not self.expires_at:
			return

		if cstr(self.expires_at) >= cstr(now_datetime()):
			return

		if not cstr(self.settlement_note).strip():
			frappe.throw(
				_(
					"This proposal expired on {0}. The payment is real, so it can still be settled -- "
					"but record why it is late in the Settlement Note, so the late grant has an "
					"explanation on the record."
				).format(self.expires_at)
			)

	def _touch_instance(self):
		"""Keep the deployment's confirmed count honest as settlements land."""
		if not self.instance_id:
			return

		frappe.db.sql(
			"UPDATE `tabRest Instance` "
			"SET `confirmed_count` = COALESCE(`confirmed_count`, 0) + 1, `last_seen_at` = %(now)s "
			"WHERE `name` = %(instance)s",
			{"now": now_datetime(), "instance": self.instance_id},
		)


def _currency_subunits(currency):
	"""
	How many minor units make one major unit of `currency`.

	Read from `Currency.fraction_units` rather than assumed to be 100. The
	overwhelming majority are 100, but a currency like MGA has 5, and a hardcoded
	factor would be wrong by a clean multiple -- which is the kind of error that
	looks like a real number and survives review.

	`number_format` is NOT this: it is a display preference (whether to group digits
	as 1,000 or 1.000). Using it here was a mistake, caught by listing the doctype's
	actual fields.
	"""
	fraction = frappe.db.get_value("Currency", currency, "fraction_units")
	if fraction:
		return cint(fraction)

	# No explicit fraction recorded. Two decimal places is the norm and the
	# documented default for this integration, so it is applied knowingly rather
	# than by accident.
	return 100
