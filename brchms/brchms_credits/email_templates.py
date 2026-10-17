# Copyright (c) 2026, Breamal and contributors
# For license information, please see license.txt

"""
The receipt a buyer receives when their credit purchase settles.

Kept as a plain string rather than a Jinja file: `frappe.sendmail` takes the
rendered message, and a template file in this app would need the app to ship
a `templates/` directory it has no other use for. The content is rendered
with `frappe.render_template` on the STRING, which keeps the substitutions
testable without a site.
"""

import frappe


RECEIPT_TEMPLATE = """
<p>{{ _("Thank you — your payment has been received.") }}</p>

<p>
  {{ _("You bought") }} <strong>{{ units }} {{ channel }} {{ _("credits") }}</strong>
  {{ _("for") }} <strong>{{ amount }}</strong>.
</p>

<p>
  {{ _("The credits will appear on your church's balance automatically — this usually takes a few minutes.") }}
</p>

{% if reference %}
<p>{{ _("Your purchase reference") }}: <strong>{{ reference }}</strong></p>
{% endif %}

{% if invoice %}
<p>{{ _("Invoice") }}: {{ invoice }}</p>
{% endif %}

<p style="color: #888; font-size: 90%;">
  {{ _("Keep this receipt. If the credits have not appeared within the hour, reply to this email with your reference.") }}
</p>
"""


def render_receipt(units, channel, amount, reference=None, invoice=None):
	"""The receipt body, rendered. Kept here so the wording is one artefact."""
	return frappe.render_template(
		RECEIPT_TEMPLATE,
		{
			"units": units,
			"channel": channel,
			"amount": amount,
			"reference": reference,
			"invoice": invoice,
		},
	)
