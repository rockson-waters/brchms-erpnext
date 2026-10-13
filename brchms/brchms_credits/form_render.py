# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
Desk-side rendering for the funding record.

Registered as a `form_render` hook in `hooks.py`. Kept out of the controller so
the controller stays about invariants, and out of the doctype JSON because this is
markup, not a field.
"""

import frappe
from frappe import _
from frappe.utils import cstr, get_url_to_form


def render_funding_extras(doc, method=None):
	"""
	Render the two things a reviewer wants on a funding record and cannot get from
	a raw field.

	1. **A clickable link to the payment document.** The Dynamic Link stores a
	   doctype and a document name; on its own that is a string a reviewer has to
	   reassemble into a URL by hand. One click is the difference between auditing
	   a funding record and not bothering.

	2. **The instance's activity, inline.** The `instance_id` on the record is a
	   Link, so Desk already offers the reference — but the display name and
	   confirmed count are what tell a reviewer whether they are looking at a
	   deployment they recognise or one they should investigate.

	Every lookup is defensive: a Desk form must render even when the referenced
	document was deleted, and a broken link is not a reason to refuse to show a
	payment record.
	"""
	if not doc or doc.doctype != "Rest Communication Credit Funding":
		return

	html = []

	# ── the payment link ────────────────────────────────────────────────────
	payment_doctype = cstr(doc.get("erpnext_payment_doctype")).strip()
	payment_document = cstr(doc.get("erpnext_payment_document")).strip()

	if payment_doctype and payment_document:
		if frappe.db.exists(payment_doctype, payment_document):
			html.append(
				f'<p><a href="{get_url_to_form(payment_doctype, payment_document)}" '
				f'target="_blank" rel="noopener">'
				f'{_("Open")} {frappe.utils.escape_html(payment_doctype)} '
				f'{frappe.utils.escape_html(payment_document)}</a></p>'
			)
		else:
			# The document is gone. Say so rather than rendering a dead link: a
			# missing payment behind a funding record is something a reviewer
			# needs to see, not something to hide.
			html.append(
				'<p class="text-muted">'
				+ _("{0} {1} no longer exists — the link cannot be opened.").format(
					frappe.utils.escape_html(payment_doctype),
					frappe.utils.escape_html(payment_document),
				)
				+ "</p>"
			)
	else:
		html.append(
			'<p class="text-muted">'
			+ _(
				"No payment document is linked. Set the type and the document to make this "
				"funding auditable against the books."
			)
			+ "</p>"
		)

	# ── the sending instance, named ─────────────────────────────────────────
	instance_id = cstr(doc.get("instance_id")).strip()
	if instance_id and frappe.db.exists("Rest Instance", instance_id):
		instance = frappe.db.get_value(
			"Rest Instance",
			instance_id,
			["display_name", "confirmed_count", "is_active", "last_seen_at"],
			as_dict=True,
		)
		if instance:
			state = _("active") if instance.is_active else _("INACTIVE")
			html.append(
				'<p class="text-muted">'
				+ _("Confirmed by {0} ({1}) · {2} funding event(s) · last seen {3}").format(
					frappe.utils.escape_html(cstr(instance.display_name or instance_id)),
					state,
					instance.confirmed_count or 0,
					frappe.utils.escape_html(cstr(instance.last_seen_at or "—")),
				)
				+ "</p>"
			)

	if html:
		doc.set("payment_link", "".join(html))

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
