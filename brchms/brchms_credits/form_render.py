# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Desk-side rendering for the proposal.

Registered as a `form_render` hook in `hooks.py`. Kept out of the controller so
the controller stays about invariants, and out of the doctype JSON because this is
markup, not a field.

The funding record's renderer lived here too, until the funding doctype was
retired: the settled proposal is the instrument now, and that is what a reviewer
opens.
"""

import frappe
from frappe import _
from frappe.utils import cstr, get_url_to_form


def render_proposal_extras(doc, method=None):
	"""
	Render a proposal's payment evidence, and say plainly when there is none.

	A reviewer's first question on a settlement is "what was paid, and where is it".
	The Dynamic Link stores a doctype and a name, which on its own is a string
	somebody has to reassemble into a URL. One click is the difference between
	auditing a settlement and not bothering.
	"""
	if not doc or doc.doctype != "Rest Credit Proposal":
		return

	html = []

	payment_doctype = cstr(doc.get("payment_doctype")).strip()
	payment_document = cstr(doc.get("payment_document")).strip()

	if payment_doctype and payment_document:
		if frappe.db.exists(payment_doctype, payment_document):
			html.append(
				f'<p><a href="{get_url_to_form(payment_doctype, payment_document)}" '
				f'target="_blank" rel="noopener">'
				f'{_("Open")} {frappe.utils.escape_html(payment_doctype)} '
				f'{frappe.utils.escape_html(payment_document)}</a></p>'
			)
		else:
			html.append(
				'<p class="text-muted">'
				+ _("{0} {1} no longer exists - the payment cannot be opened.").format(
					frappe.utils.escape_html(payment_doctype),
					frappe.utils.escape_html(payment_document),
				)
				+ "</p>"
			)
	elif doc.docstatus == 1:
		# A submitted settlement with no payment behind it should be impossible,
		# because `before_submit` requires one. Saying so loudly here means that if it
		# ever does happen, it is visible rather than a blank field somebody scrolls past.
		html.append(
			'<p class="text-danger">'
			+ _("This settlement is submitted but records no payment document. That should not be possible.")
			+ "</p>"
		)
	else:
		html.append(
			'<p class="text-muted">'
			+ _(
				"No payment recorded yet. File the payment in the books, then set the document here "
				"and submit to make the credits claimable."
			)
			+ "</p>"
		)

	if doc.quote_variance_minor:
		html.append(
			'<p class="text-muted">'
			+ _("Settled for {0} minor units more/less than quoted.").format(doc.quote_variance_minor)
			+ "</p>"
		)

	if html:
		doc.set("payment_link", "".join(html))
