# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Turning a purchase request into an invoice and a payment page.

## Why this step is server-side and not the Web Form's own mapping

A Web Form saves what it is given onto its target doctype. For a credit purchase
that would mean the buyer choosing the item, the quantity and the rate — and the
seller's accounts, company and taxes following from a public form. So the form
records only the *request* (`Rest Credit Purchase`) and this module raises the
invoice: the item comes from the channel, the rate is what the buyer agreed to pay,
and every accounting decision stays with the deployment.

## Why the invoice is raised BEFORE the customer pays

An invoice that does not exist cannot be paid. The customer is sent to a payment
page that names a real document, and the gateway's confirmation lands on that same
document, which is what `brchms_credits.settlement` watches. Raising the invoice
after payment would mean inventing a second reconciliation path for money that had
already arrived.

## The two-step shape of the public entry point

`checkout` is whitelisted with `allow_guest`, because the buyer is a stranger with
no account here. It does the whole job in one call — request in, payment URL out —
but it is deliberately the ONLY public write in this app, and it writes exactly two
documents: the purchase's invoice, and a payment request. Everything else (marking
the purchase, the settlement) runs server-side afterwards.
"""

import frappe
from frappe import _
from frappe.utils import cstr, flt

PURCHASE_DOCTYPE = "Rest Credit Purchase"
INVOICE_DOCTYPE = "Sales Invoice"

# One item per channel. The channel is the buyer's choice; WHICH ITEM represents it
# is not, which is why this mapping lives here and not in the form.
CHANNEL_ITEMS = {
	"sms": "REST-SMS-CREDIT",
	"email": "REST-EMAIL-CREDIT",
}


@frappe.whitelist(allow_guest=True)
def checkout(purchase=None):
	"""
	Raise the invoice for a purchase request and return where to pay.

	Called by the purchase page after the request has been saved. Idempotent: a
	reloaded page or a double submit returns the SAME payment URL rather than
	raising a second invoice for the same request.

	## Why this runs as Administrator

	The buyer is a stranger with no account here, and creating an invoice is
	accounting work they have no rights to: ERPNext's own `account_perm_check`
	refuses the Guest outright when the invoice picks its receivable account, and no
	per-document flag reaches that check. Acting as the system's identity is the
	honest expression of what is happening — the DEPLOYMENT is billing, and the buyer
	is only supplying what they want to buy. Their identity is carried on the purchase
	record, which is why this is safe to expose rather than a hole.

	Returns `{"invoice", "payment_url", "purchase", "status"}`.
	"""
	name = cstr(purchase).strip()
	if not name:
		frappe.throw(_("No purchase was named, so there is nothing to pay for."), frappe.ValidationError)

	previous_user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		doc = frappe.get_doc(PURCHASE_DOCTYPE, name)

		# A purchase already paid, or already invoiced, answers with what it has.
		# This is what makes a refresh safe: reloading the page must not buy credits
		# twice.
		if doc.invoice:
			return {
				"purchase": doc.name,
				"invoice": doc.invoice,
				"payment_url": doc.payment_url or _payment_url_for(doc.invoice),
				"status": doc.status,
			}

		invoice_name = _raise_invoice(doc)
		url = _payment_url_for(invoice_name)

		doc.db_set({"invoice": invoice_name, "status": "Invoiced", "payment_url": url})
		frappe.db.commit()

		return {
			"purchase": doc.name,
			"invoice": invoice_name,
			"payment_url": url,
			"status": "Invoiced",
		}
	finally:
		frappe.set_user(previous_user)


def _raise_invoice(doc):
	"""
	The Sales Invoice for this request, submitted so it can be paid.

	Submitted rather than draft because a Payment Request (and therefore a gateway
	page) is only valid against a submitted document, and because a draft invoice is
	not a receivable: nothing about the sale is real until it is submitted.
	"""
	item_code = CHANNEL_ITEMS.get(doc.channel)
	if not item_code:
		frappe.throw(
			_("This purchase names channel '{0}', which has no credit item to bill.").format(
				frappe.utils.escape_html(cstr(doc.channel))
			),
			frappe.ValidationError,
		)

	if not frappe.db.exists("Item", item_code):
		frappe.throw(
			_(
				"The credit item {0} does not exist, so this purchase cannot be billed. Run "
				"`bench migrate` to install it."
			).format(frappe.utils.escape_html(item_code)),
			frappe.DoesNotExistError,
		)

	company = frappe.defaults.get_defaults().company or frappe.db.get_value("Company", {}, "name")
	if not company:
		frappe.throw(
			_("This site has no Company, so no invoice can be raised."),
			frappe.DoesNotExistError,
		)

	customer = _customer_for(doc)
	currency = doc.currency or frappe.db.get_value("Company", company, "default_currency")

	invoice = frappe.get_doc(
		{
			"doctype": INVOICE_DOCTYPE,
			"company": company,
			"customer": customer,
			"posting_date": frappe.utils.nowdate(),
			"currency": currency,
			# The buyer agreed to THIS amount, so it is what they are billed. The
			# units travel beside it, and the settlement is where the two are checked
			# against the rate rather than silently reconciled here.
			"purchase_reference": doc.purchase_reference,
			"channel": doc.channel,
			"units": doc.units,
			"items": [
				{
					"item_code": item_code,
					"description": _("{0} credits for {1}").format(
						frappe.utils.escape_html(cstr(doc.channel).upper()), doc.units
					),
					"qty": 1,
					"rate": flt(doc.amount),
				}
			],
		}
	)
	invoice.flags.ignore_permissions = True
	invoice.insert()
	invoice.submit()
	frappe.db.commit()

	return invoice.name


def _customer_for(doc):
	"""
	The Customer this purchase is billed to, created once per email address.

	Matched on email rather than name because the same person typing "Rev. J. Mensah"
	and "John Mensah" is one customer, and two Customer records for one buyer would
	split their purchase history — which is exactly what an operator reconciling
	payments against credits would need.
	"""
	email = cstr(doc.buyer_email).strip().lower()
	existing = frappe.db.get_value("Customer", {"email_id": email}, "name")
	if existing:
		return existing

	# ERPNext may name customers by a series rather than by the entered name; the
	# getter is used so this does not depend on which naming rule the site runs.
	#
	# The fallbacks must be LEAF nodes. `Selling Settings.customer_group` is empty on
	# a site that never ran the setup wizard, and defaulting to the tree ROOT
	# ("All Customer Groups") is refused with "Cannot select a Group type Customer
	# Group" — the failure a buyer would see as a broken purchase page.
	customer_group = frappe.db.get_single_value("Selling Settings", "customer_group") or _leaf(
		"Customer Group"
	)
	territory = frappe.db.get_single_value("Selling Settings", "territory") or _leaf("Territory")

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": cstr(doc.buyer_name).strip(),
			"customer_type": "Individual",
			"email_id": email,
			"customer_group": customer_group,
			"territory": territory,
		}
	)
	customer.flags.ignore_permissions = True
	customer.insert()
	frappe.db.commit()

	return customer.name


def _leaf(doctype):
	"""
	A non-group row of a tree doctype, for use where a leaf is required.

	None when the tree has no leaves at all, so the doctype's own validation reports
	the site's problem rather than this module inventing a node to silence it.
	"""
	return frappe.db.get_value(doctype, {"is_group": 0}, "name")


def _payment_url_for(invoice_name):
	"""
	Where this invoice can be paid, creating the gateway request if needed.

	`make_payment_request` is ERPNext's own builder. It is reached only from
	`checkout`, which has already taken the administrator identity — the buyer is
	identified by their purchase record, and the gateway page they are sent to is the
	system's choice rather than theirs.
	"""
	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	existing = frappe.db.get_value(
		"Payment Request",
		{"reference_doctype": INVOICE_DOCTYPE, "reference_name": invoice_name},
		"name",
	)
	if existing:
		return frappe.get_doc("Payment Request", existing).get_payment_url()

	request = make_payment_request(
		dt=INVOICE_DOCTYPE,
		dn=invoice_name,
		submit_doc=True,
		return_doc=True,
		# The buyer is being redirected to the gateway in the same breath, so the
		# email ERPNext would otherwise send duplicates the page they are already
		# looking at — and it would make the whole purchase depend on a configured
		# outgoing Email Account, turning a missing mail server into a purchase page
		# that cannot be used at all.
		mute_email=1,
	)

	return request.get_payment_url()
