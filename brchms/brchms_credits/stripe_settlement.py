# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Settle completed Stripe charges that ERPNext's Payments app leaves open.

The Payments app charges the card and redirects to its success page, then calls
`on_payment_authorized` on the reference document. `Payment Request` does not
implement that hook, so the charge is captured while the invoice stays Unpaid and
the proposal never settles. Paystack's app carries an explicit fallback for the
same gap; this is that fallback for Stripe.

The trigger is the Payments app's own `Integration Request` row: it is marked
Completed only after Stripe reports the charge captured. We never infer payment
from a browser redirect, and we only touch credit invoices (the Sales Invoice must
carry a `purchase_reference`), so ordinary Stripe commerce on the same site is left
alone.

Idempotent: a paid Payment Request, or one that already has a Payment Entry, is
skipped. Failures are logged and retried by the next scheduler run.
"""

import frappe
from frappe.utils import add_to_date, cstr, now_datetime

INVOICE_DOCTYPE = "Sales Invoice"
PAYMENT_REQUEST_DOCTYPE = "Payment Request"


def reconcile_completed_stripe_payment_requests():
	"""Scheduler entry point: settle credit invoices paid through Stripe."""
	rows = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "Stripe",
			"status": "Completed",
			"reference_doctype": PAYMENT_REQUEST_DOCTYPE,
			"creation": (">", add_to_date(now_datetime(), minutes=-30)),
		},
		fields=["name", "reference_docname"],
		limit_page_length=100,
		order_by="creation asc",
	)

	for row in rows:
		try:
			_settle_request(cstr(row.reference_docname).strip())
		except Exception:
			frappe.log_error(
				title="brchms: Stripe credit settlement failed",
				message=frappe.get_traceback(),
			)


def _settle_request(payment_request_name):
	if not payment_request_name or not frappe.db.exists(PAYMENT_REQUEST_DOCTYPE, payment_request_name):
		return

	request = frappe.get_doc(PAYMENT_REQUEST_DOCTYPE, payment_request_name)
	if request.docstatus != 1 or request.status == "Paid":
		return
	if cstr(request.payment_gateway).strip().lower() == "paystack":
		return
	if frappe.db.exists(
		"Payment Entry",
		{"reference_no": payment_request_name, "docstatus": 1},
	):
		return

	# Only credit purchases: an ordinary Stripe invoice has no purchase reference.
	invoice = request.reference_name
	purchase_reference = (
		frappe.db.get_value(INVOICE_DOCTYPE, invoice, "purchase_reference") if invoice else None
	)
	if not cstr(purchase_reference).strip():
		return

	request.flags.ignore_permissions = True
	request.set_as_paid()
	frappe.db.commit()
