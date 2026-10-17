# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Settlement: turning a paid Sales Invoice into a claimable credit proposal.

## The state machine this drives

`Rest Credit Proposal` is DRAFT while a purchase is requested and unpaid, and
SUBMITTED once money has been received. That transition is the whole point of the
doctype — its own description says *"only a person, or a payment gateway's own
script, can submit, so nothing a tenant application says can make credits real on
its own"*. This module is that person-or-gateway step, and it is deliberately the
ONLY programmatic driver of it.

## Why the trigger is a Payment Entry and not a gateway callback

A deployment can be paid three ways: a gateway (Paystack today, Stripe later), or
offline (cash, bank transfer) entered by hand in the Desk. Every one of those ends
in exactly the same place — a submitted Payment Entry referencing the Sales
Invoice — so one hook there covers all of them. Writing per-gateway settlement
would mean the offline path had no settlement at all, and a second gateway a third
code path to keep in step.

## Why a failure here must never reach the payment

The money is the fact. A bookkeeping step that could not complete is a problem to
report, not a reason to refuse a payment that the bank has already taken. So the
hook swallows its own exceptions, records the refusal on the proposal, and leaves
it DRAFT for a retry (the escape hatch is `settle_proposal` in `proposals.py`, or
simply re-saving the Payment Entry once the cause is fixed).

## Idempotency

Submitting an already-submitted proposal is a no-op, and a second Payment Entry
against the same invoice cannot double-settle one: the proposal's `docstatus`
answers first. `purchase_reference` alone is what links the two sides — it is
unique per deployment (`after_migrate` puts the index on it), and it travels
app → webform → invoice → here.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime

PROPOSAL_DOCTYPE = "Rest Credit Proposal"
INVOICE_DOCTYPE = "Sales Invoice"
PURCHASE_REFERENCE_FIELD = "purchase_reference"


def settle_for_payment_entry(doc, method=None):
	"""
	`doc_events` entry point for Payment Entry `on_submit`.

	Never raises. A settlement failure is reported on the proposal and in the
	error log; it must not roll back a payment that has already been taken.
	"""
	try:
		results = settle_invoice_payments(
			doc,
			payment_doctype=doc.doctype,
			payment_document=doc.name,
			payment_reference=cstr(doc.get("reference_no")).strip(),
		)
	except Exception:
		frappe.log_error(
			title="brchms: credit settlement hook failed",
			message=frappe.get_traceback(),
		)
		return

	for result in results:
		if not result.get("settled") and result.get("outcome") not in (None, "no_proposal", "already"):
			frappe.log_error(
				title="brchms: a paid invoice did not settle its proposal",
				message="{0} -> {1}: {2}".format(
					result.get("invoice"), result.get("proposal"), result.get("outcome")
				),
			)


def settle_invoice_payments(
	payment, payment_doctype=None, payment_document=None, payment_reference=None
):
	"""
	Settle the proposal behind every invoice this payment references.

	`payment` is anything with a `references` child table in Payment Entry's shape —
	a real Payment Entry, or the `_ManualPayment` stand-in the escape hatch builds for
	a payment that produced no Payment Entry at all.

	Returns one result dict per referenced invoice, so a caller (or a test) can see
	exactly what happened without parsing logs:

	    {"invoice", "purchase_reference", "proposal", "outcome", "settled"}

	outcome is one of: "settled", "already", "no_reference", "no_proposal",
	"wrong_state", "settlement_fields_failed".
	"""
	payment_doctype = payment_doctype or payment.get("doctype")
	payment_document = payment_document or payment.get("name")
	results = []

	for reference in payment.get("references") or []:
		if cstr(reference.get("reference_doctype")) != INVOICE_DOCTYPE:
			continue

		invoice_name = cstr(reference.get("reference_name")).strip()
		if not invoice_name:
			continue

		# `frappe.db.get_value` rather than a document read: a submitted invoice is
		# immutable and we only need two scalars, so fetching it as a full document
		# would be two queries and a permission check for nothing.
		values = frappe.db.get_value(
			INVOICE_DOCTYPE,
			invoice_name,
			[PURCHASE_REFERENCE_FIELD, "docstatus", "company", "grand_total", "instance_id"],
			as_dict=True,
		)
		if not values:
			continue

		purchase_reference = cstr(values.get(PURCHASE_REFERENCE_FIELD)).strip()
		# Which deployment the invoice bills for. Written when the invoice is
		# raised; absent on an invoice from before the field existed, where the
		# reference-only lookup below is the honest fallback.
		invoice_instance = cstr(values.get("instance_id")).strip()

		result = {
			"invoice": invoice_name,
			"purchase_reference": purchase_reference or None,
			"proposal": None,
			"outcome": None,
			"settled": False,
		}
		results.append(result)

		if not purchase_reference:
			# An invoice with no reference is not one of ours. Ordinary commerce
			# must not be affected by a hook that came with a credits app.
			result["outcome"] = "no_reference"
			continue

		# The proposal is matched by (deployment, reference), never the reference
		# alone: organization and group ids are per-deployment, and two deployments
		# sharing one ERPNext site may both have a proposal carrying the same
		# reference. Matching on the reference alone would settle one deployment's
		# credits with another's payment -- silently, because both sides would
		# report the settle as a success.
		proposal_filters = {"purchase_reference": purchase_reference}
		if invoice_instance:
			proposal_filters["instance_id"] = invoice_instance

		proposal_name = frappe.db.get_value(PROPOSAL_DOCTYPE, proposal_filters, "name")
		result["proposal"] = proposal_name
		if not proposal_name:
			result["outcome"] = "no_proposal"
			continue

		settle_proposal_for_payment(
			proposal_name=proposal_name,
			payment=payment,
			payment_doctype=payment_doctype,
			payment_document=payment_document,
			payment_reference=payment_reference,
			invoice_name=invoice_name,
			result=result,
		)

	return results


def settle_proposal_for_payment(
	proposal_name,
	payment,
	payment_doctype,
	payment_document,
	payment_reference,
	invoice_name,
	result,
):
	"""
	Record the payment on one proposal and submit it.

	Split out so the escape hatch in `proposals.py` can reuse exactly this and
	behave identically.
	"""
	doc = frappe.get_doc(PROPOSAL_DOCTYPE, proposal_name)

	if doc.docstatus == 1:
		result["outcome"] = "already"
		result["settled"] = True
		return
	if doc.docstatus == 2:
		# Cancelled is a decision, and reviving it because money arrived would
		# contradict whoever cancelled it. Reported, not overridden.
		result["outcome"] = "wrong_state"
		return

	# ── The settlement record, saved BEFORE the submit ───────────────────────
	#
	# Order matters. `submit()` runs the doctype's validators, and one of them
	# could refuse; if the evidence were written after it, a refused submit would
	# leave no trace of WHY money arrived. Written first, the proposal can always
	# answer "this payment claims to cover me, and it is still draft".
	doc.payment_doctype = payment_doctype
	doc.payment_document = payment_document
	# The reference, in this order: what the caller said, what the payment document
	# carries, then the invoice. The first is how a hand settlement names a bank
	# line; the last is the honest fallback when a Payment Entry has no reference_no.
	doc.payment_reference = (
		cstr(payment_reference).strip()
		or cstr(payment.get("reference_no")).strip()
		or ("INVOICE:{0}".format(invoice_name) if invoice_name else cstr(payment_document))
	)
	doc.confirmed_at = now_datetime()
	doc.settled_by = _settling_user(payment)
	# The note names the invoice when there is one. A hand settlement has none, and
	# "settled invoice None" would read as a defect rather than as a manual entry.
	doc.settlement_note = (
		_("{0} {1} settled invoice {2}.").format(payment_doctype, payment_document, invoice_name)
		if invoice_name
		else _("{0} {1} settled this proposal by hand.").format(payment_doctype, payment_document)
	)

	try:
		doc.flags.ignore_permissions = True
		doc.save()
	except Exception as error:
		result["outcome"] = "settlement_fields_failed"
		frappe.log_error(
			title="brchms: could not record settlement evidence",
			message="{0}\n{1}".format(error, frappe.get_traceback()),
		)
		return

	try:
		doc.flags.ignore_permissions = True
		doc.submit()
	except Exception as error:
		# The evidence is on the record, so this is recoverable: fix the cause and
		# submit by hand, or let `settle_proposal` retry it.
		result["outcome"] = "settlement_fields_failed"
		frappe.log_error(
			title="brchms: proposal {0} could not be submitted".format(proposal_name),
			message="{0}\n{1}".format(error, frappe.get_traceback()),
		)
		return

	result["outcome"] = "settled"
	result["settled"] = True

	# ── The two buyer-facing consequences of a settled payment ──────────────
	#
	# Both are best-effort by design: the money is settled, and a desk-visibility
	# flag or a receipt that fails must not turn a completed settlement into an
	# error the operator retries -- a retry here cannot double-settle (the
	# docstatus check above answers first) but it CAN bury the real outcome in a
	# second error. Each is logged if it cannot complete.
	_mark_intake_paid(proposal_name)
	_send_receipt(doc, invoice_name)


def _settling_user(payment):
	"""
	Who to credit with the settlement, as a `Link` to User.

	A gateway's own entry is owned by the integration user; a hand-made one by the
	operator. Either is a real User row, but the field is a Link, so an unknown
	value must not be written — a broken link would make every later read fail.
	"""
	for candidate in (
		cstr(payment.get("owner")).strip(),
		cstr(getattr(frappe.session, "user", "")).strip(),
	):
		if candidate and frappe.db.exists("User", candidate):
			return candidate
	return None


PURCHASE_DOCTYPE = "Rest Credit Purchase"


def _mark_intake_paid(proposal_name):
	"""
	The form's intake row moves to `Paid`, so the desk sees a paid request.

	Without this, a fully-paid form purchase reads "Invoiced" forever -- the row
	that told the operator "somebody is waiting on this" never stops telling
	them. Best-effort: the settlement stands regardless, and a failure here is
	logged rather than raised.
	"""
	try:
		for name in frappe.get_all(
			PURCHASE_DOCTYPE, filters={"proposal": proposal_name}, pluck="name"
		):
			frappe.db.set_value(PURCHASE_DOCTYPE, name, "status", "Paid")
		frappe.db.commit()
	except Exception:
		frappe.log_error(
			title="brchms: could not mark the purchase behind {0} as paid".format(proposal_name),
			message=frappe.get_traceback(),
		)


def _send_receipt(proposal, invoice_name):
	"""
	The buyer's receipt, sent when the payment settles.

	## Why here and not the gateway

	Paystack already mails the card path; this is for the paths with no system
	behind them -- a bank transfer entered at the desk, cash taken at a counter.
	For those, the settlement hook is the one moment the purchase is known to
	be complete and the buyer's address is on the record, so this is where the
	receipt exists or nowhere does.

	Best-effort, always: a mail failure is logged and the settlement stands --
	money that moved is a fact, and a missing mail server must not be able to
	present a completed settlement as a failure to retry.
	"""
	if getattr(frappe.flags, "in_test", False):
		return

	email = cstr(proposal.get("buyer_email")).strip()
	if not email or "@" not in email:
		return

	from brchms.brchms_credits import email_templates, invoicing

	currency = cstr(proposal.get("currency")).strip() or "GHS"
	amount = invoicing.format_minor(proposal.get("amount_minor") or 0, currency)
	channel = cstr(proposal.get("channel")).strip().upper()
	units = cint(proposal.get("units") or 0)
	reference = cstr(proposal.get("purchase_reference")).strip()

	subject = _("Receipt: {0} {1} credits ({2})").format(units, channel, amount)

	message = email_templates.render_receipt(
		units=units,
		channel=channel,
		amount=amount,
		reference=reference,
		invoice=invoice_name,
	)

	try:
		frappe.sendmail(recipients=[email], subject=subject, message=message)
	except Exception:
		frappe.log_error(
			title="brchms: could not send the credit purchase receipt to {0}".format(email),
			message=frappe.get_traceback(),
		)


def invoice_paid_amount(payment, invoice_name):
	"""
	How much of this payment went to that invoice, in invoice currency.

	Kept here rather than in the hook because the amount is what a reviewer looks
	at when a proposal's units and the money disagree, and because
	`quote_variance_minor` on the proposal exists to express exactly that.
	"""
	for reference in payment.get("references") or []:
		if (
			cstr(reference.get("reference_doctype")) == INVOICE_DOCTYPE
			and cstr(reference.get("reference_name")).strip() == cstr(invoice_name).strip()
		):
			return flt(reference.get("allocated_amount"))
	return 0.0
