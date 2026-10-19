# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The public form's bridge from a buyer's request to a proposal they can pay.

## What changed, and why

The form used to raise its own Sales Invoice directly from the intake row.
That produced money on the books that could never become credits: the
settlement hook matches a payment to a PROPOSAL by its purchase reference,
and nothing ever carried one -- so a paid form invoice settled nothing, and
the app's purchase sat `requested` forever while ERPNext held the money.

`checkout` now bridges the intake row to an attributed PROPOSAL and lets the
one invoice path (`brchms_credits.invoicing`, shared with the app's door) do
the rest. The proposal carries the organization, the funding group and the
deployment's instance id, which is the whole identity the settlement and the
app's reconcile poller need.

## The identity requirement

`org` is the registry slug, and a submission without one is REFUSED. An
unattributed purchase is money on the books with nowhere to put the credits
it bought -- the app grants units to a funding GROUP, and a stranger typing
"buy credits" cannot name one. The buyer is told to open the form from their
church's own link, which is the only way a public submission can be honest
about whose balance it funds.

## Why this runs as Administrator

The buyer is a stranger with no account here, and creating a proposal and an
invoice is accounting work they have no rights to: ERPNext's own
`account_perm_check` refuses the Guest outright when an invoice picks its
receivable account, and no per-document flag reaches that check. Acting as
the system's identity is the honest expression of what is happening -- the
DEPLOYMENT is billing, and the buyer is only supplying what they want to buy.
Their identity is carried on the purchase record and the proposal, which is
why this is safe to expose rather than a hole.

## The two-step shape of the public entry point

`checkout` is whitelisted with `allow_guest`, because the buyer is a stranger
with no account here. It does the whole job in one call -- request in,
payment URL out -- but it is deliberately the ONLY public write in this app,
and it writes exactly the documents the purchase needs: a proposal, its
invoice, and a payment request. Everything else (marking the purchase,
settling the proposal, sending the receipt) runs server-side afterwards.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from brchms.brchms_credits import billing, invoicing, proposals, registry

PURCHASE_DOCTYPE = "Rest Credit Purchase"
PROPOSAL_DOCTYPE = "Rest Credit Proposal"


@frappe.whitelist(allow_guest=True)
def checkout(purchase=None, org=None):
	"""
	Turn a form submission into an attributed proposal, and return where to pay.

	Called by the purchase page after the request has been saved. Idempotent: a
	reloaded page or a double submit resolves to the SAME proposal and the SAME
	payment URL rather than raising a second one for the same money.

	Returns `{"purchase", "proposal", "invoice", "payment_url", "status"}`.
	"""
	name = cstr(purchase).strip()
	if not name:
		frappe.throw(_("No purchase was named, so there is nothing to pay for."), frappe.ValidationError)

	# ── The identity requirement ────────────────────────────────────────────
	slug = cstr(org).strip()
	if not slug:
		frappe.throw(
			_(
				"This form must be opened from your church's own purchase link. Ask your church "
				"administrator for the 'Buy credits' link, and open it from there -- a purchase "
				"has to say whose credits it is buying."
			),
			frappe.ValidationError,
		)

	registry_row = registry.resolve(slug)
	if not registry_row:
		frappe.throw(
			_(
				"This purchase link is no longer recognized. Ask your church administrator for a "
				"fresh 'Buy credits' link and try again from there."
			),
			frappe.DoesNotExistError,
		)

	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		doc = frappe.get_doc(PURCHASE_DOCTYPE, name)

		# Already bridged: answer with the page this purchase already has. A
		# reloaded form or a double submit must not buy credits twice.
		if doc.proposal:
			proposal_doc = frappe.get_doc(PROPOSAL_DOCTYPE, doc.proposal)
			page, page_error = _page_or_error(proposal_doc)
			doc.db_set(
				{
					"status": "Invoiced" if proposal_doc.invoice else doc.status,
					"company": proposal_doc.company,
					**({"payment_url": page["payment_url"]} if page and page["payment_url"] else {}),
				}
			)
			frappe.db.commit()

			if page_error is not None:
				_raise_page_refusal(doc.name, page_error)

			return {
				"purchase": doc.name,
				"proposal": doc.proposal,
				"invoice": proposal_doc.invoice,
				"payment_url": page["payment_url"] if page else None,
				"status": doc.status,
				"company": proposal_doc.company,
				"currency": proposal_doc.currency,
			}

		funding_group_id = cstr(registry_row.default_group_id).strip()
		if not funding_group_id:
			frappe.throw(
				_(
					"Your church's registration does not name a group to credit yet. Ask your "
					"church administrator to open the credits page in the app once (which "
					"completes the registration), then try again."
				),
				frappe.ValidationError,
			)

		# ── The pair must match the rate the form quoted ────────────────────
		#
		# The form's amount field is computed by `quote` and read-only, so a
		# buyer cannot type a disagreement -- but the page is public, so this
		# re-derives the amount server-side from the same rate and refuses any
		# mismatch. Defense in depth: this is what stops a crafted request
		# from naming its own price for a million credits.
		# The org slug is the whole region context: it resolves to ONE company
		# and currency, and a caller cannot name another. `quote` also refuses an
		# unregistered or region-less organization on a multi-company site.
		quoted = proposals.quote(
			channel=doc.channel,
			units=doc.units,
			org=slug,
			currency=cstr(registry_row.billing_currency).strip() or None,
		)
		amount_minor = invoicing.minor_amount_for(
			doc.units, quoted["unit_price_micros"], quoted["currency"]
		)

		if flt(doc.amount or 0) > 0:
			form_minor = _major_to_minor(doc.amount, quoted["currency"])
			if form_minor != amount_minor:
				frappe.throw(
					_(
						"The amount on this request ({0}) does not match the current price for "
						"{1} credits ({2}). Go back and let the form recompute the amount."
					).format(
						invoicing.format_minor(form_minor, quoted["currency"]),
						cint(doc.units),
						invoicing.format_minor(amount_minor, quoted["currency"]),
					),
					frappe.ValidationError,
				)

		# ── The proposal, carrying the identity the settlement needs ─────────
		result = proposals.create_proposal(
			instance_id=registry_row.instance_id,
			# The form's own row is the idempotency key: a re-submitted form
			# resolves to the SAME proposal rather than raising a second one
			# for the same money.
			request_key="form:{0}".format(doc.name),
			purchase_reference=doc.purchase_reference or None,
			organization_id=registry_row.organization_id,
			organization_name=registry_row.organization_name,
			funding_group_id=funding_group_id,
			group_name=registry_row.default_group_name,
			channel=doc.channel,
			units=doc.units,
			amount_minor=amount_minor,
			currency=quoted["currency"],
			unit_price_micros=quoted["unit_price_micros"],
			instance_name=registry_row.organization_name,
			buyer_email=doc.buyer_email,
			country_code=registry_row.billing_country,
		)

		proposal_name = result["proposal"]
		proposal_doc = frappe.get_doc(PROPOSAL_DOCTYPE, proposal_name)

		# The page, or a refusal the buyer reads. `create_proposal` already
		# tried quietly; this try is the one that must SUCCEED for the buyer to
		# pay -- but the intake row is linked FIRST, so a page that cannot be
		# built leaves a recoverable record (proposal + invoice + linkage) for
		# the desk rather than an orphaned request.
		page, page_error = _page_or_error(proposal_doc)

		doc.db_set(
			{
				"proposal": proposal_name,
				"company": proposal_doc.company,
				# Truthful either way: the invoice exists or it does not, and a
				# payment page is not the same fact as an invoice.
				"status": "Invoiced" if proposal_doc.invoice else doc.status,
				**({"payment_url": page["payment_url"]} if page and page["payment_url"] else {}),
			}
		)
		frappe.db.commit()

		if page_error is not None:
			_raise_page_refusal(doc.name, page_error)

		return {
			"purchase": doc.name,
			"proposal": proposal_name,
			"invoice": proposal_doc.invoice,
			"payment_url": page["payment_url"] if page else None,
			"status": doc.status,
			"company": proposal_doc.company,
			"currency": proposal_doc.currency,
		}
	finally:
		frappe.set_user(previous_user)


def _page_or_error(proposal_doc):
	"""
	`proposals.ensure_page`, with the failure handed back instead of raised.

	Checkout needs BOTH the answer and the failure: the intake row must be
	linked whatever happened, and the buyer must be told when there is no page.
	"""
	try:
		return proposals.ensure_page(proposal_doc), None
	except Exception as error:  # noqa: BLE001 - the refusal is re-raised by the caller
		frappe.log_error(
			title="brchms: the payment page for proposal {0} could not be opened".format(
				proposal_doc.name
			),
			message=frappe.get_traceback(),
		)
		return None, error


def _raise_page_refusal(purchase_name, cause):
	"""
	The buyer's message when the payment page could not be opened.

	The purchase is RECORDED and the intake row is linked, so the recoverable
	path exists: the desk retries this same call once the cause is fixed
	(usually a Payment Gateway Account that has not been configured). The buyer
	is told to quote the purchase's name, which is the one identifier they saw.
	"""
	frappe.throw(
		_(
			"Your purchase was recorded, but the payment page could not be opened. Please "
			"contact us and quote {0}."
		).format(frappe.utils.escape_html(cstr(purchase_name))),
		frappe.ValidationError,
	)


def _major_to_minor(amount_major, currency):
	"""A Currency-field value (major units) as an exact minor-unit integer."""
	fraction = invoicing.currency_fraction_units(currency)
	return int(round(flt(amount_major) * fraction))
