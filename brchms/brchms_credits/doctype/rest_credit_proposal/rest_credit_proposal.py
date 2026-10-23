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

from decimal import Decimal

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, now_datetime

from brchms.brchms_credits import invoicing, terms

# Doctypes whose records already carry a gateway's own transaction identifier, so the
# proposal does not need a hand-entered reference on top of them.
GATEWAY_MANAGED_DOCTYPES = {"Payment Request", "Integration Request"}


class RestCreditProposal(Document):
	def validate(self):
		self._require_the_essentials()
		self._validate_discount_snapshot()
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

		# When the proposal knows its selling company, its currency must be that
		# company's own. Legacy rows (no company) are left to the single-company
		# migration rather than refused retroactively.
		if cstr(self.company).strip():
			company_currency = cstr(
				frappe.db.get_value("Company", self.company, "default_currency")
			).upper()
			if company_currency and cstr(self.currency).strip().upper() != company_currency:
				frappe.throw(
					_(
						"Proposal {0} is in {1}, but {2} keeps its books in {3}. A cross-currency "
						"settlement would make the two ledgers disagree."
					).format(
						frappe.utils.escape_html(self.name or _("new")),
						frappe.utils.escape_html(self.currency),
						frappe.utils.escape_html(self.company),
						frappe.utils.escape_html(company_currency),
					)
				)

		# Integers throughout: `flt` is an IEEE double and silently rounded a
		# 2^53-scale value earlier in this integration. This is money.
		for fieldname, label in (
			("units", "Units"),
			("amount_minor", "Amount"),
			("unit_price_micros", "Unit Price"),
			("list_unit_price_micros", "List Unit Price"),
			("discount_basis_points", "Discount (basis points)"),
			("list_amount_minor", "List Amount"),
			("discount_minor", "Discount"),
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

	def _validate_discount_snapshot(self):
		"""
		The discount snapshot must agree with its own price.

		The effective price is RE-DERIVED from `(list_unit_price_micros,
		discount_basis_points)` with the same integer half-up rule the quote, the
		invoice and the platform application use. A hand-edited row -- or an
		older client trying to name its own price -- therefore cannot produce a
		proposal whose effective price nobody can derive.

		This deliberately does NOT re-check the SITE MINIMUM against the current
		settings: a minimum may be raised after a purchase was quoted, and
		refusing an already-quoted purchase retroactively would strand money
		that genuinely moved. The minimum is enforced at raise time (quote,
		checkout, create_proposal) and the settled amount is a fact.

		Legacy rows raised before terms existed have no list price: their own
		`unit_price_micros` IS the list price, and they are normalised here so
		every later read sees the same shape.
		"""
		list_micros = cint(self.list_unit_price_micros or 0)
		basis_points = cint(self.discount_basis_points or 0)
		effective = cint(self.unit_price_micros or 0)
		units = cint(self.units or 0)

		if not list_micros:
			if basis_points:
				frappe.throw(
					_(
						"A proposal claiming a {0}% discount must also record the list price it was "
						"discounted from."
					).format(basis_points / 100),
					frappe.ValidationError,
				)
			self.list_unit_price_micros = effective
			self.discount_basis_points = 0
			self.discount_percent = 0.0
			self.list_amount_minor = cint(self.amount_minor or 0)
			self.discount_minor = 0
			return

		if basis_points < 0 or basis_points > terms.MAX_BASIS_POINTS:
			frappe.throw(
				_(
					"Discount must be between 0% and 99.99%; {0} basis points is not a price that "
					"can be charged."
				).format(basis_points),
				frappe.ValidationError,
			)

		expected = terms.effective_unit_price_micros(list_micros, basis_points)
		if effective != expected:
			frappe.throw(
				_(
					"The discount snapshot on this proposal disagrees with its own price: {0} micros "
					"with a {1}% discount comes to {2} micros, not {3}. Re-quote the purchase."
				).format(
					list_micros,
					Decimal(basis_points) / 100,
					expected,
					effective,
				),
				frappe.ValidationError,
			)

		self.discount_percent = round(basis_points / 100, 2)

		if not units or not cstr(self.currency).strip():
			# Raised before the essentials are complete; the derived amounts are
			# filled once the purchase has a quantity and a currency.
			return

		list_amount = invoicing.minor_amount_for(units, list_micros, self.currency)
		amount = cint(self.amount_minor or 0)
		if amount > list_amount:
			frappe.throw(
				_(
					"The discounted amount on this proposal ({0}) is greater than its list amount "
					"({1}). A discount cannot increase what the buyer pays."
				).format(amount, list_amount),
				frappe.ValidationError,
			)

		self.list_amount_minor = list_amount
		self.discount_minor = list_amount - amount

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
			("list_unit_price_micros", "List Unit Price"),
			("discount_basis_points", "Discount (basis points)"),
			("list_amount_minor", "List Amount"),
			("discount_minor", "Discount"),
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

		Computed with the SAME half-up rounding the pricing invariant uses
		(`invoicing.minor_amount_for`), so a settlement that matches its quote
		records a variance of exactly zero in every currency -- a floor here and
		a round there would disagree by one minor unit on precisely the
		payments that were priced correctly, which is a false alarm an operator
		would have to chase.
		"""
		if not self.unit_price_micros or not self.units:
			return None

		return invoicing.minor_amount_for(self.units, self.unit_price_micros, self.currency)

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
